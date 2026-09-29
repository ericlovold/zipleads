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
    rows = {r["company_name"]: r for r in ctx.store.all_leads()}
    assert rows["Northstar Dental Eagan"]["score"] > rows["Northstar Dental"]["score"]
    assert rows["Fall festival draws crowds to Eagan park"]["score"] < rows["Crumbl"]["score"]


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
