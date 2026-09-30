from pathlib import Path

from tests.conftest import FakeHttp
from zipleads import mail
from zipleads.cli import main
from zipleads.models import Lead
from zipleads.pipeline import Context, ingest
from zipleads.score import score_lead
from zipleads.segments import excluded_by, flags_for
from zipleads.store import Store


def _lead(name, description="", primary_type="", **kw) -> Lead:
    return Lead(
        source="places_future",
        signal="places:future_opening",
        company_name=name,
        description=description,
        zip="55401",
        raw={"primaryType": primary_type},
        **kw,
    )


def test_segment_exclude_and_flags(profile):
    assert excluded_by(profile, _lead("Bright Minds Preschool")) == "preschool"
    assert excluded_by(profile, _lead("Acme", primary_type="university")) == "university"
    assert excluded_by(profile, _lead("Northstar Dental")) == ""
    assert flags_for(profile, _lead("Northstar Dental")) == ["flag:deprioritized:dental"]
    assert flags_for(profile, _lead("Hilton Garden Inn", primary_type="hotel")) == [
        "flag:deprioritized:hotel"
    ]
    assert flags_for(profile, _lead("Smith Law Office")) == ["flag:boost:office"]
    assert flags_for(profile, _lead("Joe's Coffee")) == []


def test_score_flags_and_value_tiers(profile):
    base = score_lead(profile, "mpls_permits", "permit:x", 1, "", "")
    assert score_lead(profile, "mpls_permits", "permit:x,flag:deprioritized:dental", 1, "", "") == (
        base - 20
    )
    assert score_lead(profile, "mpls_permits", "permit:x,flag:boost:office", 1, "", "") == base + 10
    assert score_lead(profile, "mpls_permits", "permit:x", 1, "", "", value=99_999) == base
    assert score_lead(profile, "mpls_permits", "permit:x", 1, "", "", value=100_000) == base + 5
    assert score_lead(profile, "mpls_permits", "permit:x", 1, "", "", value=5_000_000) == base + 20
    # A flag alone never triggers the "unparsed" penalty logic.
    assert score_lead(profile, "google_news", "news:headline,flag:boost:clinic", 1, "", "") == (
        20 - 5 + 10
    )


def test_ingest_drops_excluded_and_flags_rest(make_settings, territory, profile):
    places = {
        "places": [
            {
                "id": "a",
                "displayName": {"text": "Bright Minds Preschool"},
                "businessStatus": "FUTURE_OPENING",
                "primaryType": "preschool",
                "formattedAddress": "1 A St, Minneapolis, MN 55401",
                "addressComponents": [{"shortText": "55401", "types": ["postal_code"]}],
            },
            {
                "id": "b",
                "displayName": {"text": "Northstar Dental"},
                "businessStatus": "FUTURE_OPENING",
                "primaryType": "dentist",
                "formattedAddress": "2 B St, Minneapolis, MN 55401",
                "addressComponents": [{"shortText": "55401", "types": ["postal_code"]}],
            },
            {
                "id": "c",
                "displayName": {"text": "Riverside Law Office"},
                "businessStatus": "FUTURE_OPENING",
                "primaryType": "lawyer",
                "formattedAddress": "3 C St, Minneapolis, MN 55401",
                "addressComponents": [{"shortText": "55401", "types": ["postal_code"]}],
            },
        ]
    }
    settings = make_settings(places_key="PK")
    ctx = Context(
        settings,
        territory,
        profile,
        FakeHttp({"places:searchText": places}),
        Store(settings.db_path),
    )
    report = ingest(ctx, ("places",))
    assert report.dropped_by_segment == {"preschool": 1}
    rows = {r["company_name"]: r for r in ctx.store.all_leads()}
    assert "Bright Minds Preschool" not in rows
    assert "flag:deprioritized:" in rows["Northstar Dental"]["signals"]
    assert rows["Riverside Law Office"]["score"] > rows["Northstar Dental"]["score"]


def test_permit_value_is_stored_and_kept_max(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    key, _ = store.upsert(
        Lead(
            source="mpls_permits",
            signal="p",
            company_name="X",
            address="1 A St",
            zip="55401",
            value=50_000,
        )
    )
    store.upsert(
        Lead(
            source="stpaul_permits",
            signal="p",
            company_name="X",
            address="1 A St",
            zip="55401",
            value=400_000,
        )
    )
    store.upsert(
        Lead(
            source="google_news",
            signal="n",
            company_name="X",
            address="1 A St",
            zip="55401",
            value=0,
        )
    )
    assert store.get(key)["value"] == 400_000


def test_mark_submitted_records_ref(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    key, _ = store.upsert(Lead(source="s", signal="x", company_name="X", zip="55401"))
    assert store.mark_submitted(key, ref="REF-123")
    assert store.get(key)["submitted_ref"] == "REF-123"


class FakeSMTP:
    sent: list = []
    logins: list = []

    def __init__(self, host, port):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_mail_build_and_send(make_settings, tmp_path):
    settings = make_settings()
    settings = settings.__class__(
        **{
            **settings.__dict__,
            "smtp_host": "smtp.example",
            "smtp_user": "u",
            "smtp_password": "p",
            "mail_from": "me@example",
            "mail_to": ("ken@example", "me@example"),
        }
    )
    store = Store(settings.db_path)
    for i, name in enumerate(["Alpha Clinic", "Beta Dental", "Gamma Shop"]):
        key, _ = store.upsert(
            Lead(
                source="places_future",
                signal="p",
                company_name=name,
                address=f"{i} Main St",
                zip="55401",
                phone="651-555-0100",
            )
        )
        store.set_score(key, 50 - i)
    store.add_signal("beta dental|55401", "flag:deprioritized:dental")
    csv_path = tmp_path / "leads.csv"
    csv_path.write_text("h\n1\n")
    rows = store.unsubmitted()
    msg = mail.build_message(settings, rows, csv_path, "Test Territory")
    assert msg["To"] == "ken@example, me@example"
    assert msg["Subject"] == "zipleads: 3 new leads, Test Territory"
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert body.index("Alpha Clinic") < body.index("Gamma Shop")
    assert "[deprioritized:dental]" in body
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_filename() == "leads.csv"
    mail.send(settings, msg, smtp_factory=FakeSMTP)
    assert FakeSMTP.sent[-1] is msg and FakeSMTP.logins[-1] == ("u", "p")


def test_send_dry_run_and_missing_smtp(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZIPLEADS_DB", str(tmp_path / "x.sqlite"))
    monkeypatch.delenv("SMTP_HOST", raising=False)
    out = tmp_path / "leads.csv"
    assert main(["send", "--out", str(out), "--dry-run"]) == 0
    assert "Subject: zipleads: 0 new leads" in capsys.readouterr().out
    assert Path(out).exists()
    assert main(["send", "--out", str(out)]) == 2
