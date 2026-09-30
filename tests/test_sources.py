from tests.conftest import FakeHttp
from zipleads.sources import arcgis_permits, google_news, places_future


def test_permits_keep_wanted_only(fixture_json, territory, profile):
    layer = territory.permit_layers[0]
    leads = arcgis_permits.parse_features(
        fixture_json("arcgis_query.json")["features"], layer, profile
    )
    # Company is the tenant named in the comments, or blank; the applicant is the contractor.
    assert [ld.company_name for ld in leads] == ["Northstar Dental", ""]
    assert [ld.applicant for ld in leads] == ["Greiner Construction", "Ryan Companies"]
    first = leads[0]
    assert first.source == "mpls_permits" and first.zip == "55401" and first.state == "MN"
    assert first.signal == "permit:remodel"
    assert first.signal_date == "2026-09-25"
    assert "Northstar Dental" in first.description
    assert first.description.startswith(
        "permit BLDG-2026-01234 | Commercial | Building | Remodel | Issued"
    )
    assert first.raw["value"] == 425000


def test_tenant_extraction():
    ex = arcgis_permits.extract_tenant
    assert ex("Tenant improvement for Northstar Dental, suite 300") == "Northstar Dental"
    assert ex("TENANT FINISH FOR ACME LAW GROUP LLC AT SUITE 200") == "ACME LAW GROUP LLC"
    assert ex("Tenant: Lakes Physical Therapy - interior remodel") == "Lakes Physical Therapy"
    assert ex("new restaurant buildout for Crumbl Cookies located in suite 110") == "Crumbl Cookies"
    assert ex("scope includes plumbing for a kitchen remodel.") == ""
    assert ex("Interior remodel for existing tenant") == ""
    assert ex("remodel floors 2 through 5 - main west hospital building.") == ""
    assert ex("Plumbing for two (2) ADA restrooms, electric water cooler, and a mop sink") == ""
    assert ex("SEPARATE PERMITS ARE REQUIRED FOR ELECTRICAL, PLUMBING, AND HEATING") == ""
    assert (
        ex("Office tenant improvement on 7th floor. Tenant:  JE Dunn Constru") == "JE Dunn Constru"
    )
    assert ex("") == ""


def test_permit_exclude_types_are_whole_tokens(territory, profile):
    from zipleads.sources.arcgis_permits import wanted

    assert not wanted(
        profile, "MFD", "Remodel", "kitchen remodel", type_fields=("MFD", "Commercial")
    )
    assert not wanted(profile, "TFD", "Remodel", "deck", type_fields=("TFD", "Res"))
    # Trade permits: occupancy blank, housing code in the work type.
    assert not wanted(
        profile, "Plumbing", "Res", "basement bathroom", type_fields=("Plumbing", "Res", "")
    )
    assert wanted(
        profile, "Comm", "Remodel", "restaurant build-out", type_fields=("Comm", "Commercial")
    )


def test_permit_applicant_person_is_appended(fixture_json, territory, profile):
    layer = territory.permit_layers[0]
    feats = fixture_json("arcgis_query.json")["features"][:1]
    feats[0]["attributes"]["fullName"] = "Pat Builder"
    lead = arcgis_permits.parse_features(feats, layer, profile)[0]
    assert lead.applicant == "Greiner Construction / Pat Builder"


def test_permits_paging_and_where_clause(fixture_json, territory, profile):
    http = FakeHttp({"/query": fixture_json("arcgis_query.json")})
    leads = arcgis_permits.fetch_permits(http, territory.permit_layers[0], profile, 7)
    assert len(leads) == 2
    _, url, params = http.calls[0]
    assert url.endswith("CCS_Permits/FeatureServer/0/query")
    assert params["where"].startswith("issueDate >= TIMESTAMP '")


def test_probe_sample_returns_raw_attributes(fixture_json, territory):
    http = FakeHttp({"/query": fixture_json("arcgis_query.json")})
    rows = arcgis_permits.sample_features(http, territory.permit_layers[0], 2)
    assert rows[0]["applicantName"] == "Greiner Construction"
    assert http.calls[0][2]["resultRecordCount"] == 2


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
