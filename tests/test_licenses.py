import pytest

from tests.conftest import FakeHttp
from zipleads.config import LicenseFeed, LicenseFieldMap
from zipleads.sorter import NEW_OCCUPANT, TENANT_REFRESH
from zipleads.sources import licenses

STPAUL = LicenseFeed(
    name="stpaul_licenses", type="legistar", city="Saint Paul", state="MN", client="stpaul"
)

# Real Saint Paul titles (RES 24-1372, RES 25-702, RES 25-628 on stpaul.legistar.com).
GOPUFF = (
    "Approving the application for a license approval for GB License LLC d/b/a Gopuff for the "
    "Liquor Off Sale (License ID #20230002311) for the premises located at 129 State Street."
)
SHARRETT = (
    "Approving the application for change of ownership to the Liquor Off Sale and Tobacco Shop "
    "license now held by University Liquor LLC d/b/a Sharrett Liquor (License ID #20250000436) "
    "for the premises located at 2389 University Avenue West."
)
EARL = (
    "Approving the application for approval of amended license conditions for Earl Street Auto "
    "Sales and Repairs LLC d/b/a Earl Street Auto Sales and Repairs LLC for the Second Hand "
    "Dealer-Motor Vehicle and Auto Repair Garage (License ID #20250000006) for the premises "
    "located at 803 Earl St."
)


def test_parse_new_license_title():
    f = licenses.parse_legistar_title(GOPUFF)
    assert f == {
        "legal": "GB License LLC",
        "dba": "Gopuff",
        "license_types": "Liquor Off Sale",
        "action": "a license approval",
        "license_id": "20230002311",
        "address": "129 State Street",
    }


def test_parse_change_of_ownership_title():
    f = licenses.parse_legistar_title(SHARRETT)
    assert f["legal"] == "University Liquor LLC" and f["dba"] == "Sharrett Liquor"
    assert f["license_types"] == "Liquor Off Sale and Tobacco Shop"
    assert f["address"] == "2389 University Avenue West"


def test_parse_application_of_form_and_ward_suffix():
    f = licenses.parse_legistar_title(
        "Approving the application of Clairview Holdings LLC d/b/a Groveland Tap to add a "
        "Gambling Location license (License ID #20210001234) for the premises located at "
        "1834 St. Clair Avenue, in Ward 3."
    )
    assert f["legal"] == "Clairview Holdings LLC" and f["dba"] == "Groveland Tap"
    assert f["address"] == "1834 St. Clair Avenue" and f["action"] == ""


def test_parse_without_trade_name():
    f = licenses.parse_legistar_title(
        "Approving the application for a license approval for Northern Fuel Inc for the Gas "
        "Station (License ID #3) for the premises located at 900 Arcade Street."
    )
    assert f["legal"] == "Northern Fuel Inc" and f["dba"] == "Northern Fuel Inc"


@pytest.mark.parametrize(
    "title",
    [
        "Authorizing the Department of Parks and Recreation to accept a grant.",
        "Approving the application for a sound level variance at 5 Main St.",  # no license
        "Approving the license application for Foo LLC.",  # no premises
    ],
)
def test_non_license_titles_are_ignored(title):
    assert licenses.parse_legistar_title(title) is None


@pytest.mark.parametrize(
    ("text", "license_type", "status", "expect"),
    [
        (GOPUFF, "Liquor Off Sale", "", NEW_OCCUPANT),
        (SHARRETT, "", "", NEW_OCCUPANT),
        (EARL, "", "", TENANT_REFRESH),
        (
            "to add a Gambling Location license and upgrade to full Liquor On Sale",
            "",
            "",
            TENANT_REFRESH,
        ),
        ("Approving adverse action against the license held by Foo LLC", "", "", None),
        ("", "Liquor On Sale", "Withdrawn", None),
        ("", "Liquor On Sale", "Renewal", None),
        ("", "Mobile Food Vendor", "Pending", None),
        ("", "Peddler", "Pending", None),
        ("", "Food Manufacturer", "Pending", NEW_OCCUPANT),
        ("", "Fine Dining Restaurant", "Pending", NEW_OCCUPANT),  # "fine" is not a penalty
        ("Change of ownership", "Restaurant", "Pending", NEW_OCCUPANT),
    ],
)
def test_classify_license(text, license_type, status, expect):
    verdict = licenses.classify_license(text, license_type, status)
    assert (verdict.kind if verdict else None) == expect
    if verdict:
        assert verdict.reasons


def test_parse_matters_keeps_applications_with_evidence(fixture_json):
    leads = licenses.parse_matters(fixture_json("legistar_matters.json"), STPAUL)
    # Adverse action, mobile food vendor and the grant are dropped.
    assert [(ld.company_name, ld.kind) for ld in leads] == [
        ("Bao Bistro", NEW_OCCUPANT),
        ("Sharrett Liquor", NEW_OCCUPANT),
        ("Earl Street Auto Sales and Repairs LLC", TENANT_REFRESH),
    ]
    bao = leads[0]
    assert bao.applicant == "Bao Bistro LLC" and bao.address == "1600 Grand Avenue"
    assert bao.city == "Saint Paul" and bao.state == "MN" and bao.zip == ""
    assert bao.source == "stpaul_licenses" and bao.signal_date == "2026-09-30"
    assert bao.signal.startswith("license:liquor outdoor service area")
    assert (
        bao.evidence_url
        == "https://stpaul.legistar.com/LegislationDetail.aspx?ID=7900000&GUID=GUID-0"
    )
    assert bao.description == (
        "RES 26-1501 | Liquor Outdoor Service Area (Patio) and Wine On Sale | "
        "a license approval | Adopted"
    )
    assert bao.raw["title"].startswith("Approving the application")
    assert bao.raw["license_id"] == "20260000999"
    # Legal name equal to the trade name is not repeated as the applicant.
    assert leads[2].applicant == ""


def test_fetch_legistar_filters_by_date_and_pages():
    calls = []
    full_page = [{"MatterTitle": "unrelated"}] * licenses.LEGISTAR_PAGE

    class Paging(FakeHttp):
        def get_json(self, url, params=None, headers=None):
            calls.append((url, params))
            return full_page if params["$skip"] == 0 else [{"MatterTitle": GOPUFF}]

    leads = licenses.fetch_legistar(Paging(), STPAUL, 7)
    assert [ld.company_name for ld in leads] == ["Gopuff"]
    assert calls[0][0] == "https://webapi.legistar.com/v1/stpaul/matters"
    assert calls[0][1]["$filter"].startswith("MatterIntroDate ge datetime'")
    assert [p["$skip"] for _, p in calls] == [0, licenses.LEGISTAR_PAGE]


def test_fetch_legistar_rejects_error_body():
    with pytest.raises(RuntimeError):
        licenses.fetch_legistar(FakeHttp({"legistar": {"Message": "denied"}}), STPAUL, 7)


def test_fetch_skips_unconfigured_feeds():
    assert (
        licenses.fetch_licenses(
            FakeHttp(), STPAUL.__class__(name="x", type="legistar", city="", state="MN"), 7
        )
        == []
    )
    assert (
        licenses.fetch_licenses(
            FakeHttp(), LicenseFeed(name="y", type="arcgis", city="", state="MN"), 7
        )
        == []
    )


ARC = LicenseFeed(
    name="mpls_licenses",
    type="arcgis",
    city="Minneapolis",
    state="MN",
    url="https://example.test/FeatureServer/0",
    fields=LicenseFieldMap(
        business_name="dba",
        legal_name="legal",
        address="addr",
        license_type="ltype",
        date="appDate",
        status="status",
        license_number="caseNo",
        latitude="lat",
        longitude="lon",
    ),
)


def _feature(case, dba, ltype="Restaurant", status="Pending", addr="10 Main St, 55401"):
    return {
        "attributes": {
            "caseNo": case,
            "dba": dba,
            "legal": f"{dba} LLC",
            "addr": addr,
            "ltype": ltype,
            "status": status,
            "appDate": 1790208000000,
            "lat": 44.98,
            "lon": -93.27,
        }
    }


def test_parse_license_features_dedupes_and_classifies():
    feats = [
        _feature("LIC-1", "Bao Bistro"),
        _feature("LIC-1", "Bao Bistro"),  # repeated row for the same case
        _feature("LIC-2", "Old Bar", status="Withdrawn"),
        _feature("LIC-3", "Tacos Mobile", ltype="Mobile Food Vehicle"),
        _feature("LIC-4", "No Site", addr=""),
    ]
    leads = licenses.parse_license_features(feats, ARC)
    assert [ld.company_name for ld in leads] == ["Bao Bistro"]
    ld = leads[0]
    assert ld.applicant == "Bao Bistro LLC" and ld.zip == "55401" and ld.kind == NEW_OCCUPANT
    assert ld.signal_date == "2026-09-24" and ld.raw["lat"] == 44.98
    assert ld.description == "license LIC-1 | Restaurant | Pending"


def test_fetch_license_layer_where_clause():
    http = FakeHttp({"/query": {"features": [_feature("LIC-1", "Bao Bistro")]}})
    leads = licenses.fetch_license_layer(http, ARC, 7)
    assert len(leads) == 1
    _, url, params = http.calls[0]
    assert url.endswith("/FeatureServer/0/query")
    assert params["where"].startswith("appDate >= TIMESTAMP '")
