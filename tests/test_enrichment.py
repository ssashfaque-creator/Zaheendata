from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from zaheendata.enrich.reviews import ReviewAnalyzer
from zaheendata.enrich.techstack import TechStackDetector
from zaheendata.enrich.website import extract_features, parse_page
from zaheendata.models import Review, utcnow


def _pages(fixtures_dir: Path, host: str, *names: str):
    pages = []
    for n in names:
        html = (fixtures_dir / "sites" / host / f"{n}.html").read_text()
        pages.append(
            parse_page(
                f"https://{host}/{'' if n == 'index' else n}", 200, html, {"content-type": "text/html"}
            )
        )
    return pages


def test_website_features_plumber(fixtures_dir: Path):
    pages = _pages(fixtures_dir, "hillcountryplumbingair.com", "index", "about", "contact")
    f = extract_features(pages, "hillcountryplumbingair.com")
    assert f.crawled and f.pages_crawled == 3
    assert f.emergency_service and f.maintenance_plan and f.residential_focus and f.financing
    assert f.says_call_for_quote and not f.has_online_booking and not f.has_quote_form
    assert f.free_estimate
    assert f.locally_owned_language and not f.franchise_language
    assert f.multiple_crews_or_staff
    assert f.owner_name_candidates[0] == "Marcus Reyes"
    assert "marcus@hillcountryplumbingair.com" in f.emails
    assert "5125550101" in f.phones
    assert set(f.social_links) >= {"facebook", "instagram"}


def test_website_features_booking_and_team(fixtures_dir: Path):
    f = extract_features(_pages(fixtures_dir, "lonestarcomfort.com", "index", "team"), "lonestarcomfort.com")
    assert f.has_online_booking and f.booking_provider == "servicetitan.com"
    assert not f.says_call_for_quote  # online alternative exists
    assert f.owner_name_candidates[0] == "Angela Whitfield"
    assert f.social_links.get("linkedin")


def test_website_features_salon(fixtures_dir: Path):
    f = extract_features(_pages(fixtures_dir, "luxelanesalon.com", "index"), "luxelanesalon.com")
    assert f.chair_count_hint == 10  # max of "8 chairs" / "10 stylists"
    assert f.retail_products and f.sells_gift_cards and f.walk_ins_welcome
    assert f.online_store and f.store_platform == "shopify"
    assert not f.has_online_booking
    assert "Jasmine Lee" in f.owner_name_candidates
    assert "instagram" in f.social_links


def test_owner_regex_does_not_duplicate_names(fixtures_dir: Path):
    f = extract_features(_pages(fixtures_dir, "eastsideplumbingatx.com", "index"), "eastsideplumbingatx.com")
    assert f.owner_name_candidates == ["Ray Delgado"]


def test_tech_detection(fixtures_dir: Path):
    det = TechStackDetector()
    stack = det.detect(
        _pages(fixtures_dir, "hillcountryplumbingair.com", "index"), "hillcountryplumbingair.com"
    )
    assert {"Square", "Mailchimp", "Google Analytics", "Wisetack"} <= stack.names
    assert {"payments", "email_marketing", "financing"} <= stack.categories
    stack2 = det.detect(_pages(fixtures_dir, "lonestarcomfort.com", "index"), "lonestarcomfort.com")
    assert {"ServiceTitan", "HubSpot", "Podium", "Affirm"} <= stack2.names
    stack3 = det.detect(_pages(fixtures_dir, "luxelanesalon.com", "index"), "luxelanesalon.com")
    assert {"Shopify", "Klaviyo", "Instagram"} <= stack3.names


def test_review_analyzer_pain_and_recency():
    now = utcnow()
    reviews = [
        Review(
            text="Great techs but the office never called me back about my invoice",
            published_at=now - timedelta(days=5),
        ),
        Review(
            text="I was double charged and nobody responds to email.", published_at=now - timedelta(days=20)
        ),
        Review(text="No one showed up for the appointment.", published_at=now - timedelta(days=200)),
        Review(text="Lovely people, perfect job.", published_at=now - timedelta(days=400)),
        Review(text="Still waiting on a quote two weeks later.", published_at=None),
    ]
    insights = ReviewAnalyzer().analyse(reviews)
    assert insights.analysed_reviews == 5
    assert insights.recent_reviews == 2
    assert insights.pain_mentions == 4
    assert insights.pain_categories["slow_callback"] >= 2
    assert insights.pain_categories["billing"] >= 1
    assert insights.pain_categories["scheduling"] == 1
    assert insights.pain_categories["quote_estimate"] == 1
    assert len(insights.pain_examples) == 3
    assert insights.last_review_at == now - timedelta(days=5)
