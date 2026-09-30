"""Building permits from an ArcGIS FeatureServer layer.

Each territory lists its own permit layers with their field names. The
profile decides which permits count (include/exclude terms on permit type,
work type, and description).

A permit applicant is often the contractor, not the tenant. The lead still
carries the address and the work description, which is enough to look the
tenant up during enrichment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zipleads.config import PermitLayer, Profile
from zipleads.http import Http
from zipleads.models import Lead
from zipleads.normalize import extract_zip

PAGE_SIZE = 1000


def layer_fields(http: Http, layer: PermitLayer) -> list[dict]:
    """Field list from the layer's metadata, for the `probe` command."""
    meta = http.get_json(layer.url, params={"f": "pjson"})
    return meta.get("fields", [])


def _epoch_ms_to_iso(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _number(value) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def wanted(profile: Profile, *texts: str) -> bool:
    hay = " ".join(t.lower() for t in texts if t)
    if profile.permit_include_terms and not any(t in hay for t in profile.permit_include_terms):
        return False
    return not any(t in hay for t in profile.permit_exclude_terms)


def parse_features(features: list[dict], layer: PermitLayer, profile: Profile) -> list[Lead]:
    f = layer.fields
    leads: list[Lead] = []
    for feat in features:
        a = feat.get("attributes", {})
        permit_type = str(a.get(f.permit_type) or "")
        work_type = str(a.get(f.work_type) or "")
        description = str(a.get(f.description) or "")
        occupancy = str(a.get(f.occupancy) or "") if f.occupancy else ""
        if not wanted(profile, permit_type, work_type, description, occupancy):
            continue
        permit_number = str(a.get(f.permit_number) or "") if f.permit_number else ""
        status = str(a.get(f.status) or "") if f.status else ""
        applicant = str(a.get(f.applicant) or "").strip()
        address = str(a.get(f.address) or "").strip()
        if not applicant and not address:
            continue
        leads.append(
            Lead(
                source=layer.name,
                signal=f"permit:{(work_type or permit_type).strip().lower()}"[:60],
                company_name=applicant,
                address=address,
                city=layer.city,
                state=layer.state,
                zip=extract_zip(address),
                signal_date=_epoch_ms_to_iso(a.get(f.date)),
                evidence_url=layer.url,
                description=" | ".join(
                    x
                    for x in (
                        f"permit {permit_number}" if permit_number else "",
                        occupancy,
                        permit_type,
                        work_type,
                        status,
                        description,
                    )
                    if x
                )[:500],
                value=_number(a.get(f.value)),
                raw={"attributes": a, "value": a.get(f.value)},
            )
        )
    return leads


def fetch_permits(http: Http, layer: PermitLayer, profile: Profile, since_days: int) -> list[Lead]:
    """Pull permits issued in the last `since_days`, paging through the layer."""
    if not layer.url:
        return []
    since = datetime.now(UTC) - timedelta(days=since_days)
    where = f"{layer.fields.date} >= TIMESTAMP '{since.strftime('%Y-%m-%d %H:%M:%S')}'"
    features: list[dict] = []
    offset = 0
    while True:
        page = http.get_json(
            f"{layer.url}/query",
            params={
                "where": where,
                "outFields": "*",
                "orderByFields": f"{layer.fields.date} DESC",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
                "f": "json",
            },
        )
        if "error" in page:
            raise RuntimeError(f"{layer.name}: {page['error']}")
        batch = page.get("features", [])
        features.extend(batch)
        if not page.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
    return parse_features(features, layer, profile)
