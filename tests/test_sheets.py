from tests.conftest import FakeHttp
from zipleads import sheets
from zipleads.models import Lead
from zipleads.store import Store


def _client(http):
    return sheets.SheetsClient(http, "SHEET123", token=lambda: "tok", tab="Leads")


def _lead(name, **kw):
    base = dict(
        source="mpls_permits",
        signal="permit:x",
        company_name=name,
        address="1 Main St",
        city="Minneapolis",
        zip="55401",
    )
    base.update(kw)
    return Lead(**base)


def test_push_writes_header_then_rows_on_empty_sheet(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.upsert(_lead("Alpha"))
    store.upsert(_lead("Beta"))
    http = FakeHttp(
        {
            "values/Leads!A:P:append": {},
            "values/Leads!A1:P1": {},
            "values/Leads!A:P": {"values": []},
        }
    )
    n = sheets.push(_client(http), store.unsubmitted())
    assert n == 2
    methods = [(m, u.rsplit("/", 1)[-1]) for m, u, _ in http.calls]
    assert methods[0][0] == "GET" and methods[1] == ("PUT", "Leads!A1:P1")
    appended = http.calls[2][2]["values"]
    assert appended[0][0] in ("alpha|minneapolis", "beta|minneapolis")
    assert len(appended[0]) == len(sheets.HEADER)


def test_push_skips_rows_already_in_sheet(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.upsert(_lead("Alpha"))
    store.upsert(_lead("Beta"))
    existing = {"values": [sheets.HEADER, ["alpha|minneapolis", "2026-10-01", "30", "Alpha"]]}
    http = FakeHttp({"values/Leads!A:P:append": {}, "values/Leads!A:P": existing})
    n = sheets.push(_client(http), store.unsubmitted())
    assert n == 1
    assert http.calls[-1][2]["values"][0][0] == "beta|minneapolis"
    assert not any(m == "PUT" for m, _, _ in http.calls)  # header already present


def test_pull_applies_statuses_and_hides_junk(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ka, _ = store.upsert(_lead("Alpha"))
    kb, _ = store.upsert(_lead("Beta"))
    kc, _ = store.upsert(_lead("Gamma"))
    kd, _ = store.upsert(_lead("Delta"))

    def row(key, status="", ref="", notes=""):
        r = [key] + [""] * (len(sheets.HEADER) - 1)
        r[sheets.STATUS_COL], r[sheets.REF_COL], r[sheets.NOTES_COL] = status, ref, notes
        return r

    values = [
        sheets.HEADER,
        row(ka, "Submitted", "REF-1"),
        row(kb, "junk"),
        row(kc, "maybe later"),
        row(kd, "", "", "call after 10am"),
        ["missing|key", "", "", "x"],
    ]
    http = FakeHttp({"values/Leads!A:P": {"values": values}})
    counts = sheets.pull(_client(http), store)
    assert counts == {"submitted": 1, "junk": 1, "noted": 1, "unknown_status": 1}
    assert store.get(ka)["submitted_at"] and store.get(ka)["submitted_ref"] == "REF-1"
    assert store.get(kb)["status"] == "junk"
    assert store.get(kd)["notes"] == "call after 10am"
    keys = {r["dedupe_key"] for r in store.unsubmitted()}
    assert keys == {kc, kd}  # submitted and junk are gone from the working list
    # Pulling again is idempotent.
    assert sheets.pull(_client(http), store) == {
        "submitted": 0,
        "junk": 0,
        "noted": 0,
        "unknown_status": 1,
    }
