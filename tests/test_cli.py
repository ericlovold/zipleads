import tomllib

from zipleads.cli import main


def test_new_territory_writes_valid_toml(tmp_path):
    out = tmp_path / "austin.toml"
    rc = main(
        [
            "new-territory",
            "--name",
            "Austin",
            "--state",
            "tx",
            "--zips",
            "78701, 78702,78701",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    doc = tomllib.loads(out.read_text())
    assert doc["state"] == "TX" and doc["zips"] == ["78701", "78702"]


def test_stats_and_export_on_empty_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZIPLEADS_DB", str(tmp_path / "x.sqlite"))
    assert main(["stats"]) == 0
    assert "leads=0" in capsys.readouterr().out
    out = tmp_path / "leads.csv"
    assert main(["export", "--out", str(out)]) == 0
    assert out.read_text().startswith("contact_name,company_name,contact_email,phone")
    assert main(["mark-submitted", "nope"]) == 1


def test_probe_places_requires_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.setenv("ZIPLEADS_DB", str(tmp_path / "x.sqlite"))
    assert main(["probe-places", "Eagan, MN"]) == 2


def test_default_ingest_sources_exclude_places():
    from zipleads import pipeline

    assert pipeline.DEFAULT_SOURCES == ("permits", "news")
    assert "places" in pipeline.BUILTIN_SOURCES
