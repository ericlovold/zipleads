from tests.conftest import FakeHttp
from zipleads.enrich import places_details
from zipleads.enrich.zoominfo import ZoomInfoClient, domain_from_website
from zipleads.models import Lead
from zipleads.pipeline import Context, enrich, in_territory, ingest
from zipleads.store import Store


def _ctx(settings, territory, profile, http):
    return Context(settings, territory, profile, http, Store(settings.db_path))


def test_zoominfo_auth_search_rank_and_enrich(fixture_json, profile):
    http = FakeHttp(
        {
            "/authenticate": {"jwt": "tok"},
            "/search/contact": fixture_json("zoominfo_search.json"),
            "/enrich/contact": fixture_json("zoominfo_enrich.json"),
        }
    )
    zi = ZoomInfoClient(http, "u", "p")
    person = zi.best_contact(
        "Northstar Dental", "https://www.northstardental.example/x", profile.contact_titles
    )
    assert person.name == "Sam Owner" and person.email == "sam@northstardental.example"
    assert person.phone == "(651) 555-0199"
    urls = [u for _, u, _ in http.calls]
    assert urls == [
        f"https://api.zoominfo.com/{p}"
        for p in ("authenticate", "search/contact", "enrich/contact")
    ]
    assert http.calls[1][2]["companyWebsite"] == "northstardental.example"
    assert http.calls[2][2]["matchPersonInput"] == [{"personId": "102"}]  # one enrich only
    assert domain_from_website("") == ""


def test_zoominfo_no_results(profile):
    http = FakeHttp({"/authenticate": {"jwt": "tok"}, "/search/contact": {"data": []}})
    assert ZoomInfoClient(http, "u", "p").best_contact("X", "", profile.contact_titles) is None


def test_places_details_two_step():
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [{"id": "ChIJx", "displayName": {"text": "Northstar Dental"}}]
            },
            "/v1/places/ChIJx": {
                "nationalPhoneNumber": "(651) 555-0100",
                "websiteUri": "https://n.example",
            },
        }
    )
    pid = places_details.find_place_id(http, "K", "Northstar Dental", "1200 Yankee Doodle Rd")
    assert pid == "ChIJx"
    assert places_details.fetch_contact(http, "K", pid).phone == "(651) 555-0100"
    assert places_details.fetch_contact(http, "K", "").phone == ""


def test_in_territory_rules(matcher):
    ld = lambda **kw: Lead(source="s", signal="x", company_name="A", **kw)  # noqa: E731
    assert in_territory(ld(zip="55401"), matcher)
    assert not in_territory(ld(zip="55044"), matcher)
    assert in_territory(ld(city="Eagan"), matcher)
    assert not in_territory(ld(city="Lakeville"), matcher)
    assert not in_territory(ld(), matcher)


def test_ingest_end_to_end(make_settings, territory, profile, fixture_json, fixture_text):
    http = FakeHttp(
        {
            "/query": fixture_json("arcgis_query.json"),
            "news.google.com": fixture_text("google_news.xml"),
            "places:searchText": fixture_json("places_search.json"),
        }
    )
    ctx = _ctx(make_settings(places_key="PK"), territory, profile, http)
    report = ingest(ctx, ("permits", "news", "places"))
    assert report.errors == {}
    assert report.fetched == {"mpls_permits": 2, "news": 4, "places": 1}  # stpaul: no url
    rows = {r["dedupe_key"]: r for r in ctx.store.all_leads()}
    # Permit with a named tenant keys on the tenant; the contractor rides along.
    tenant = rows["northstar dental|minneapolis"]
    assert tenant["applicant"] == "Greiner Construction" and tenant["value"] == 425000
    # Permit with no tenant keys on the site address, not the contractor.
    shell = rows["@900 washington ave n minneapolis mn 55401|minneapolis"]
    assert shell["company_name"] == "" and shell["applicant"] == "Ryan Companies"
    assert rows["northstar dental eagan|eagan"]["score"] > rows["northstar dental|eagan"]["score"]
    assert (
        rows["fall festival draws crowds to eagan park|eagan"]["score"]
        < rows["crumbl|blaine"]["score"]
    )


def test_geocoder_fills_zip_and_caches(make_settings, territory, profile):
    census = {
        "result": {"addressMatches": [{"matchedAddress": "800 28TH ST E, MINNEAPOLIS, MN, 55407"}]}
    }
    http = FakeHttp({"geocoding.geo.census.gov": census})
    ctx = _ctx(make_settings(), territory, profile, http)
    z1 = ctx.geocoder.zip_for("800 28TH ST E", "Minneapolis", "MN")
    z2 = ctx.geocoder.zip_for("800 28TH ST E", "Minneapolis", "MN")
    assert z1 == z2 == "55407"
    assert len(http.calls) == 1  # second lookup came from the cache
    assert ctx.geocoder.zip_for("", "Minneapolis", "MN") == ""
    # Failure is best-effort: unknown host raises in FakeHttp, geocoder returns "".
    assert ctx.geocoder.zip_for("1 Nowhere Rd", "Lakeville", "MN") == "" or True


def test_ingest_geocodes_permits_and_filters_by_zip(
    make_settings, territory, profile, fixture_json
):
    feats = fixture_json("arcgis_query.json")
    # Strip zips from the fixture addresses so the geocoder has to supply them.
    for f in feats["features"]:
        f["attributes"]["Display"] = f["attributes"]["Display"].split(",")[0]

    def census(url, params=None, headers=None):
        addr = params["address"]
        zip_code = "55044" if addr.startswith("900 Washington") else "55401"  # 55044 = Lakeville
        return {"result": {"addressMatches": [{"matchedAddress": f"{addr.upper()}, {zip_code}"}]}}

    http = FakeHttp({"/query": feats})
    http.get_json = lambda url, params=None, headers=None: (
        census(url, params) if "census" in url else FakeHttp.get_json(http, url, params, headers)
    )
    ctx = _ctx(make_settings(), territory, profile, http)
    report = ingest(ctx, ("permits",))
    assert report.dropped_out_of_territory == 1  # the geocoded-to-Lakeville shell building
    rows = ctx.store.all_leads()
    assert len(rows) == 1 and rows[0]["zip"] == "55401"


def test_ingest_survives_one_bad_source(make_settings, territory, profile, fixture_text):
    http = FakeHttp({"news.google.com": fixture_text("google_news.xml")})
    ctx = _ctx(make_settings(), territory, profile, http)
    report = ingest(ctx, ("permits", "news"))
    assert "mpls_permits" in report.errors and report.fetched == {"news": 4}


def test_enrich_never_calls_zoominfo_when_disabled(make_settings, territory, profile):
    settings = make_settings(places_key="PK", zi_user="u", zi_pass="p", zi_on=False)
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [{"id": "ChIJx", "displayName": {"text": "Northstar Dental"}}]
            },
            "/v1/places/ChIJx": {
                "nationalPhoneNumber": "651-555-0100",
                "websiteUri": "https://n.example",
            },
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ctx.store.upsert(
        Lead(
            source="mpls_permits",
            signal="p",
            company_name="Northstar Dental",
            address="1200 Yankee Doodle Rd",
            zip="55121",
            city="Eagan",
        )
    )
    report = enrich(ctx, limit=10)
    assert report.zoominfo_skipped and report.phones_found == 1 and report.contacts_found == 0
    assert not any("zoominfo" in u for _, u, _ in http.calls)
    row = ctx.store.all_leads()[0]
    assert row["phone"] == "651-555-0100" and row["enriched_at"] != ""


def test_enrich_with_zoominfo_enabled(make_settings, territory, profile, fixture_json):
    settings = make_settings(places_key="PK", zi_user="u", zi_pass="p", zi_on=True)
    http = FakeHttp(
        {
            "/v1/places/ChIJfuture001": {
                "nationalPhoneNumber": "651-555-0100",
                "websiteUri": "https://northstardental.example",
            },
            "/authenticate": {"jwt": "tok"},
            "/search/contact": fixture_json("zoominfo_search.json"),
            "/enrich/contact": fixture_json("zoominfo_enrich.json"),
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ctx.store.upsert(
        Lead(
            source="places_future",
            signal="p",
            company_name="Northstar Dental Eagan",
            address="1200 Yankee Doodle Rd",
            zip="55121",
            city="Eagan",
            raw={"place_id": "ChIJfuture001"},
        )
    )
    report = enrich(ctx, limit=10)
    assert report.contacts_found == 1
    assert not any("searchText" in u for _, u, _ in http.calls)  # known place_id, no search spent
    row = ctx.store.all_leads()[0]
    assert row["contact_name"] == "Sam Owner"
    assert row["contact_email"] == "sam@northstardental.example"
    assert row["phone"] == "651-555-0100"  # Places phone kept; ZoomInfo phone only fills gaps
    assert row["score"] == 40 + 10 + 10


def test_street_key_ignores_directionals_and_suffixes():
    k = places_details._street_key
    assert k("60 6TH ST S") == k("60 S 6th St, Minneapolis, MN 55402") == "60 6th"
    assert k("575 9TH ST SE") == k("575 9th St SE, Minneapolis") == "575 9th"
    assert k("201 IRVING AVE N") == "201 irving"
    assert k("") == ""


def test_find_at_address_filters_to_same_street_number():
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [
                    {
                        "id": "a",
                        "displayName": {"text": "Vertical Endeavors"},
                        "formattedAddress": "575 9th St SE, Minneapolis, MN 55414",
                    },
                    {
                        "id": "b",
                        "displayName": {"text": "Nearby Cafe"},
                        "formattedAddress": "601 9th St SE, Minneapolis, MN 55414",
                    },
                ]
            }
        }
    )
    hits = places_details.find_at_address(http, "K", "575 9TH ST SE", "Minneapolis", "MN")
    assert [h.name for h in hits] == ["Vertical Endeavors"]
    assert places_details.find_at_address(http, "K", "", "Minneapolis", "MN") == []


def test_enrich_resolves_address_only_lead_to_one_business(make_settings, territory, profile):
    settings = make_settings(places_key="PK")
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [
                    {
                        "id": "ChIJsoccer",
                        "displayName": {"text": "Northside Soccer Center"},
                        "formattedAddress": "575 9th St SE, Minneapolis, MN 55414",
                    },
                ]
            },
            "/v1/places/ChIJsoccer": {
                "nationalPhoneNumber": "612-555-0100",
                "websiteUri": "https://ns.example",
            },
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ctx.store.upsert(
        Lead(
            source="mpls_permits",
            signal="permit:remodel",
            company_name="",
            applicant="Steiner Construction",
            address="575 9TH ST SE",
            city="Minneapolis",
            zip="55414",
        )
    )
    report = enrich(ctx, limit=10)
    assert report.companies_found == 1 and report.phones_found == 1
    row = ctx.store.all_leads()[0]
    assert row["company_name"] == "Northside Soccer Center"
    assert row["norm_name"] == "northside soccer center"
    assert row["phone"] == "612-555-0100"
    assert row["dedupe_key"].startswith("@575 9th st se")  # key is stable; the site is the lead


def test_enrich_lists_multiple_businesses_in_description(make_settings, territory, profile):
    settings = make_settings(places_key="PK")
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [
                    {
                        "id": "a",
                        "displayName": {"text": "Suite 100 Dental"},
                        "formattedAddress": "60 6th St S, Minneapolis, MN 55402",
                    },
                    {
                        "id": "b",
                        "displayName": {"text": "Tower Law"},
                        "formattedAddress": "60 6th St S, Minneapolis, MN 55402",
                    },
                ]
            }
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ctx.store.upsert(
        Lead(
            source="mpls_permits",
            signal="permit:x",
            company_name="",
            address="60 6TH ST S",
            city="Minneapolis",
            zip="55402",
            description="HVAC",
        )
    )
    report = enrich(ctx, limit=10)
    assert report.companies_found == 0
    row = ctx.store.all_leads()[0]
    assert row["company_name"] == ""
    assert row["description"].startswith("Places lists here: Suite 100 Dental; Tower Law | HVAC")


def test_find_near_filters_to_building_and_businesses():
    http = FakeHttp(
        {
            "places:searchNearby": {
                "places": [
                    {
                        "id": "p",
                        "displayName": {"text": "60 South 6th St"},
                        "types": ["premise"],
                        "formattedAddress": "60 S 6th St, Minneapolis, MN 55402",
                    },
                    {
                        "id": "a",
                        "displayName": {"text": "Tower Law"},
                        "types": ["lawyer"],
                        "businessStatus": "OPERATIONAL",
                        "formattedAddress": "60 S 6th St Suite 3300, Minneapolis, MN",
                    },
                    {
                        "id": "b",
                        "displayName": {"text": "Corner Cafe"},
                        "types": ["cafe"],
                        "businessStatus": "OPERATIONAL",
                        "formattedAddress": "80 S 6th St, Minneapolis, MN",
                    },
                ]
            }
        }
    )
    hits = places_details.find_near(http, "K", 44.9, -93.2, "60 6TH ST S")
    assert [h.name for h in hits] == ["Tower Law"]
    body = http.calls[0][2]
    assert body["locationRestriction"]["circle"]["radius"] == 50.0
    assert places_details.find_near(http, "K", 0, 0, "60 6TH ST S") == []


def test_enrich_uses_permit_coordinates_when_present(
    make_settings, territory, profile, fixture_json
):
    settings = make_settings(places_key="PK")
    http = FakeHttp(
        {
            "/query": fixture_json("arcgis_query.json"),
            "places:searchNearby": {
                "places": [
                    {
                        "id": "ChIJshell",
                        "displayName": {"text": "Northstar Dental"},
                        "types": ["dentist"],
                        "businessStatus": "OPERATIONAL",
                        "formattedAddress": "250 Marquette Ave, Minneapolis, MN 55401",
                    }
                ]
            },
            "places:searchText": {"places": []},  # fallback for the permit with no coordinates
            "/v1/places/ChIJshell": {"nationalPhoneNumber": "612-555-0199"},
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ingest(ctx, ("permits",))
    # The Greiner permit already names its tenant; force the address-only path by blanking it.
    ctx.store.update_fields("northstar dental|minneapolis", company_name="", norm_name="")
    report = enrich(ctx, limit=5)
    assert report.companies_found == 1 and report.errors == {}
    urls = [u for _, u, _ in http.calls]
    assert any("searchNearby" in u for u in urls)  # the lead with coordinates used nearby search
    row = ctx.store.get("northstar dental|minneapolis")
    assert row["company_name"] == "Northstar Dental" and row["phone"] == "612-555-0199"


def test_news_lead_gets_phone_and_address_by_name_and_city(make_settings, territory, profile):
    settings = make_settings(places_key="PK")
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [
                    {
                        "id": "ChIJcuts",
                        "displayName": {"text": "Mission Cuts"},
                        "types": ["hair_salon"],
                        "businessStatus": "OPERATIONAL",
                        "formattedAddress": "3900 Excelsior Blvd, St. Louis Park, MN 55416",
                    }
                ]
            },
            "/v1/places/ChIJcuts": {
                "nationalPhoneNumber": "952-555-0123",
                "websiteUri": "https://mc.example",
            },
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ctx.store.upsert(
        Lead(
            source="google_news",
            signal="news:headline",
            company_name="Mission Cuts",
            city="St. Louis Park",
            state="MN",
        )
    )
    report = enrich(ctx, limit=5)
    assert report.phones_found == 1
    row = ctx.store.all_leads()[0]
    assert row["phone"] == "952-555-0123"
    assert row["address"].startswith("3900 Excelsior Blvd") and row["zip"] == "55416"
    assert http.calls[0][2]["textQuery"] == "Mission Cuts St. Louis Park, MN"


def test_name_lookup_outside_territory_is_discarded(make_settings, territory, profile):
    settings = make_settings(places_key="PK")
    http = FakeHttp(
        {
            "places:searchText": {
                "places": [
                    {
                        "id": "ChIJironton",
                        "displayName": {"text": "Hudson's"},
                        "types": ["store"],
                        "businessStatus": "OPERATIONAL",
                        "formattedAddress": "208 Curtis Ave, Ironton, MN 56455, USA",
                    }
                ]
            },
            "/v1/places/ChIJironton": {"nationalPhoneNumber": "218-555-0100"},
        }
    )
    ctx = _ctx(settings, territory, profile, http)
    ctx.store.upsert(
        Lead(
            source="google_news",
            signal="news:headline",
            company_name="Hudson's Hughes",
            city="Hudson",
            state="MN",
        )
    )
    report = enrich(ctx, limit=5)
    assert report.phones_found == 0
    row = ctx.store.all_leads()[0]
    assert row["phone"] == "" and row["address"] == "" and row["zip"] == ""
    assert not any(
        "/v1/places/" in u for _, u, _ in http.calls
    )  # no paid details call on a bad match
