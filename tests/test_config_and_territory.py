import pytest

from zipleads.config import load_profile, load_territory
from zipleads.normalize import dedupe_key, extract_zip, normalize_address, normalize_name


def test_normalize_name_strips_suffix_and_punctuation():
    assert normalize_name("Northstar Dental, LLC") == "northstar dental"
    assert normalize_name("The Lakes P.T. Inc.") == "lakes p t"
    assert normalize_name("Smith & Jones PLLC") == "smith and jones"
    assert normalize_name("") == ""


def test_normalize_address_abbreviates():
    assert normalize_address("250 Marquette Avenue South, Suite 300") == (
        "250 marquette ave s ste 300"
    )


def test_extract_zip_and_dedupe_key():
    assert extract_zip("Eagan, MN 55121-1234") == "55121"
    assert extract_zip("11361 Fountains Dr, Maple Grove, MN 55369, USA") == "55369"
    assert extract_zip("PO Box 123456") == ""
    assert dedupe_key("Northstar Dental LLC", "55121") == "northstar dental|55121"
    assert dedupe_key("Northstar Dental", "", city="Eagan") == "northstar dental|eagan"
    # City wins over zip so news (no zip) and Places (zip) land on one key.
    assert dedupe_key("Northstar Dental", "55121", city="Eagan") == "northstar dental|eagan"
    assert dedupe_key("", "55401", address="250 Marquette Ave") == "@250 marquette ave|55401"
    assert dedupe_key("", "", city="Minneapolis", address="800 28th St E") == (
        "@800 28th st e|minneapolis"
    )


def test_territory_loads_zips_cities_and_layers(territory):
    assert territory.state == "MN"
    assert "55401" in territory.zips and territory.zips["55401"] == "Minneapolis"
    assert "56073" in territory.zips  # New Ulm
    assert "54016" in territory.zips  # Hudson WI, still in the rep's list
    assert "55044" not in territory.zips  # Lakeville is another carrier
    assert [ly.name for ly in territory.permit_layers] == ["mpls_permits", "stpaul_permits"]
    assert territory.permit_layers[0].fields.applicant == "applicantName"
    assert territory.permit_layers[0].fields.occupancy == "occupancyType"
    assert territory.permit_layers[1].url == ""


def test_territory_without_zips_is_rejected(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('name = "empty"\nstate = "TX"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_territory(p)


def test_territory_bare_zips_become_search_areas(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('name = "x"\nstate = "TX"\nzips = ["78701", "78702"]\n', encoding="utf-8")
    from zipleads.territory import TerritoryMatcher

    m = TerritoryMatcher(load_territory(p))
    assert m.cities() == []
    assert m.search_areas() == ["78701", "78702"]
    assert m.contains_zip("78701") and not m.contains_zip("78703")


def test_matcher_city_matching(matcher):
    assert matcher.match_city("Northstar Dental to open second clinic in Eagan") == "Eagan"
    assert matcher.match_city("New clinic coming to Saint Paul") == "St. Paul"
    assert matcher.match_city("Eagan clinic moving to Woodbury") == "Woodbury"
    assert matcher.match_city("Lakeville clinic opens") == ""
    assert "Minneapolis" in matcher.cities()
    assert matcher.search_areas()[0].endswith(", MN")


def test_territory_labels_are_real_cities(territory):
    for city in territory.cities():
        assert "/" not in city and "(" not in city, city
    assert "University of Minnesota" not in territory.cities()
    assert territory.zips["55455"] == "Minneapolis" and territory.zips["55155"] == "St. Paul"


def test_profile_loads(profile):
    assert profile.source_weights["places_future"] == 40
    assert profile.sorter_drop == ("residential", "building_systems")
    assert profile.sorter_hide == ("tenant_refresh",)
    assert profile.contact_titles[0] == "owner"
    assert "maxpreps" in profile.news_exclude_terms
    assert profile.export_columns[:4] == ("contact_name", "company_name", "contact_email", "phone")


def test_default_profile_has_defaults():
    p = load_profile("profiles/default.toml")
    assert p.export_columns[0] == "contact_name"
    assert p.places_queries == ("opening soon",)


def test_smtp_password_strips_all_whitespace(monkeypatch):
    from zipleads.config import load_settings

    monkeypatch.setenv("SMTP_PASSWORD", "abcd\u00a0efgh ijkl\tmnop ")
    assert load_settings().smtp_password == "abcdefghijklmnop"


def test_profile_rejects_old_permits_section_and_unknown_kinds(tmp_path):
    old = tmp_path / "old.toml"
    old.write_text('name = "x"\n[permits]\ninclude_terms = ["commercial"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="permit sorter"):
        load_profile(old)
    bad = tmp_path / "bad.toml"
    bad.write_text('name = "x"\n[sorter]\ndrop = ["condos"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kinds"):
        load_profile(bad)


def test_territory_loads_license_feeds(territory):
    feeds = {fd.name: fd for fd in territory.license_feeds}
    assert set(feeds) == {"stpaul_licenses", "mpls_licenses"}
    sp = feeds["stpaul_licenses"]
    assert sp.type == "legistar" and sp.client == "stpaul" and sp.state == "MN"
    assert sp.configured
    mpls = feeds["mpls_licenses"]
    assert mpls.type == "arcgis" and mpls.url == "" and not mpls.configured
    assert mpls.fields.date == "APPLICATION_DATE"


def test_license_feed_type_is_checked(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text(
        'name = "x"\nstate = "TX"\nzips = ["78701"]\n'
        '[[license_feeds]]\nname = "austin"\ntype = "socrata"\ncity = "Austin"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="legistar, arcgis"):
        load_territory(p)


def test_territory_without_license_feeds_still_loads(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('name = "x"\nstate = "TX"\nzips = ["78701"]\n', encoding="utf-8")
    assert load_territory(p).license_feeds == ()
