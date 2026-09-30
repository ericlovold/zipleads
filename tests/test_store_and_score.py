from zipleads.models import Lead
from zipleads.score import score_lead
from zipleads.store import Store


def _lead(**kw) -> Lead:
    base = dict(
        source="mpls_permits",
        signal="permit:x",
        company_name="Northstar Dental",
        address="1200 Yankee Doodle Rd",
        zip="55121",
        city="Eagan",
    )
    base.update(kw)
    return Lead(**base)


def test_upsert_creates_then_merges(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    key, created = store.upsert(_lead())
    assert created
    key2, created2 = store.upsert(
        _lead(source="google_news", signal="news:headline", phone="651-555-0100")
    )
    assert key2 == key and not created2
    row = store.get(key)
    assert row["sources"] == "mpls_permits,google_news"
    assert row["signals"] == "permit:x,news:headline"
    assert row["phone"] == "651-555-0100"


def test_merge_does_not_overwrite_existing_values(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    key, _ = store.upsert(_lead(phone="651-555-0100"))
    store.upsert(_lead(phone="000"))
    assert store.get(key)["phone"] == "651-555-0100"


def test_multi_site_counts_distinct_addresses(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.upsert(_lead(address="1200 Yankee Doodle Rd", zip="55121"))
    store.upsert(_lead(address="1200 Yankee Doodle Road", zip="55121"))
    assert store.distinct_addresses("northstar dental") == 1
    store.upsert(_lead(address="800 Grand Ave", zip="55105", city="St. Paul"))
    assert store.distinct_addresses("northstar dental") == 2


def test_mark_submitted_is_idempotent(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    key, _ = store.upsert(_lead())
    assert store.mark_submitted(key)
    assert not store.mark_submitted(key)
    assert not store.mark_submitted("nope|00000")
    assert store.unsubmitted() == []


def test_needs_enrichment_skips_complete_and_enriched(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    k1, _ = store.upsert(_lead(company_name="A"))
    store.upsert(_lead(company_name="B", phone="1", contact_email="b@x"))
    k3, _ = store.upsert(_lead(company_name="C"))
    store.mark_enriched(k3)
    assert [r["dedupe_key"] for r in store.needs_enrichment(10)] == [k1]


def test_score_ordering(profile):
    s = lambda src, sig="", n=1, ph="", em="": score_lead(profile, src, sig, n, ph, em)  # noqa: E731
    news_only = s("google_news", "news:headline")
    permit = s("mpls_permits")  # falls back to the "permits" weight
    places = s("places_future")
    assert news_only < permit < places
    assert s("mpls_permits", n=2) > permit
    assert s("mpls_permits,google_news") > permit
    assert s("places_future", n=2, ph="651", em="a@b") == 40 + 30 + 10 + 10
    assert s("", "") == 0
    assert s("google_news", "news:unparsed") < news_only
    assert s("google_news,mpls_permits", "news:unparsed,permit:x") > permit


def test_migration_adds_columns_to_old_database(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE leads (dedupe_key TEXT PRIMARY KEY, norm_name TEXT NOT NULL, "
        "company_name TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)"
    )
    con.commit()
    con.close()
    store = Store(db)
    cols = {row[1] for row in store.conn.execute("PRAGMA table_info(leads)")}
    assert {"applicant", "value", "submitted_ref"} <= cols


def test_reset_enrichment_only_touches_unsubmitted_without_phone(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    k1, _ = store.upsert(_lead(company_name="A"))
    k2, _ = store.upsert(_lead(company_name="B", phone="1"))
    k3, _ = store.upsert(_lead(company_name="C"))
    for k in (k1, k2, k3):
        store.mark_enriched(k)
    store.mark_submitted(k3)
    assert store.reset_enrichment() == 1
    assert [r["dedupe_key"] for r in store.needs_enrichment(10)] == [k1]
