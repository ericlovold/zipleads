from tests.conftest import FakeHttp
from zipleads.sources import arcgis_permits, google_news, places_future


def test_permits_keep_wanted_only(fixture_json, territory, profile):
    layer = territory.permit_layers[0]
    leads = arcgis_permits.parse_features(
        fixture_json("arcgis_query.json")["features"], layer, profile
    )
    assert [ld.company_name for ld in leads] == ["Greiner Construction", "Ryan Companies"]
    first = leads[0]
    assert first.source == "mpls_permits" and first.zip == "55401" and first.state == "MN"
    assert first.signal == "permit:remodel"
    assert first.signal_date == "2026-09-25"
    assert "Northstar Dental" in first.description
    assert first.description.startswith(
        "permit BLDG-2026-01234 | Commercial | Building | Remodel | Issued"
    )
    assert first.raw["value"] == 425000


def test_permits_paging_and_where_clause(fixture_json, territory, profile):
    http = FakeHttp({"/query": fixture_json("arcgis_query.json")})
    leads = arcgis_permits.fetch_permits(http, territory.permit_layers[0], profile, 7)
    assert len(leads) == 2
    _, url, params = http.calls[0]
    assert url.endswith("CCS_Permits/FeatureServer/0/query")
    assert params["where"].startswith("issueDate >= TIMESTAMP '")


def test_permits_skip_when_url_blank(territory, profile):
    assert arcgis_permits.fetch_permits(FakeHttp(), territory.permit_layers[1], profile, 7) == []


def test_news_company_extraction():
    ex = google_news.extract_company
    assert ex("Northstar Dental to open second clinic in Eagan - Star Tribune") == (
        "Northstar Dental"
    )
    assert ex("Minneapolis-based Lakes Physical Therapy relocating to Woodbury - BMTN") == (
        "Lakes Physical Therapy"
    )
    assert ex("Crumbl's new location in Blaine set for October - Patch") == "Crumbl"
    assert ex("Fall festival draws crowds to Eagan park - Sun Thisweek") == ""


def test_news_feed_to_leads(fixture_text, matcher):
    items = google_news.parse_feed(fixture_text("google_news.xml"))
    assert len(items) == 4 and items[0]["published"] == "2026-09-28"
    leads = google_news.items_to_leads(items, matcher, "Eagan", "MN")
    by_name = {ld.company_name: ld for ld in leads}
    assert by_name["Northstar Dental"].city == "Eagan"
    assert by_name["Lakes Physical Therapy"].city == "Woodbury"
    assert by_name["Crumbl"].city == "Blaine"
    assert [ld for ld in leads if ld.signal == "news:unparsed"][0].company_name.startswith("Fall")


def test_news_query_uses_full_state_name():
    q = google_news.build_query(('"grand opening"', "relocating"), "Eagan", "MN", 7)
    assert q == '("grand opening" OR relocating) "Eagan" Minnesota when:7d'
    assert google_news.feed_url(q).startswith("https://news.google.com/rss/search?q=%28")


def test_news_fetch_dedupes_links_across_cities(fixture_text, matcher):
    http = FakeHttp({"news.google.com": fixture_text("google_news.xml")})
    leads = google_news.fetch_news(http, matcher, ('"new location"',), 7)
    assert len(http.calls) == len(matcher.cities())
    assert len(leads) == 4


def test_places_future_filters_status_and_territory(fixture_json, matcher):
    leads = places_future.parse_places(fixture_json("places_search.json")["places"], matcher)
    assert [ld.company_name for ld in leads] == ["Northstar Dental Eagan"]
    assert leads[0].zip == "55121" and leads[0].raw["place_id"] == "ChIJfuture001"


def test_places_future_fetch_cost_shape(fixture_json, matcher):
    http = FakeHttp({"places:searchText": fixture_json("places_search.json")})
    leads = places_future.fetch_future_openings(
        http, "KEY", matcher, ("opening soon", "coming soon")
    )
    assert len(leads) == 1
    assert len(http.calls) == len(matcher.search_areas()) * 2  # one page per area per query
    assert "nationalPhoneNumber" not in places_future.FIELD_MASK
