from __future__ import annotations

from zaheendata.models import (
    Business,
    BusinessStatus,
    OwnerContact,
    ReviewInsights,
    Technology,
    TechStack,
    WebsiteFeatures,
)
from zaheendata.registry import get_registry
from zaheendata.scoring import ScoringEngine, feature_resolver, rank_and_select

EXPECTED_NAICS = {
    "plumbing_hvac": "238220",
    "electrical": "238210",
    "landscaping": "561730",
    "residential_remodelers": "236118",
    "roofing": "238160",
    "pest_control": "561710",
    "painting": "238320",
    "other_building_services": "561790",
    "beauty_salons": "812112",
    "nail_salons": "812113",
    "janitorial": "561720",
    "florists": "453110",
    "pet_supply_stores": "453910",
    "sporting_goods_stores": "451110",
    "gift_souvenir_stores": "453220",
    "womens_clothing_stores": "448120",
    "hobby_toy_game_stores": "451120",
}


def test_registry_matches_brief():
    reg = get_registry()
    assert len(reg) == 17
    assert {k: s.naics for k, s in reg.subsectors.items()} == EXPECTED_NAICS
    assert reg.by_naics("812112").key == "beauty_salons"
    for s in reg:
        assert s.queries and s.cross_check and s.fit_signal.features
        assert 1 <= s.fit_signal.strong_threshold <= len(s.fit_signal.features)


def test_every_rule_feature_is_resolvable():
    """Every feature named in subsectors.yaml must map to a WebsiteFeatures field or a derived feature."""
    probe = Business(name="probe", website_features=WebsiteFeatures(crawled=True))
    resolve = feature_resolver(probe)
    known_fields = set(WebsiteFeatures.model_fields)
    derived = {
        "no_online_booking",
        "five_plus_chairs",
        "store_on_shopify_or_etsy",
        "instagram_present",
        "commercial_only",
        "wire_service_only",
        "busy_50_reviews",
    }
    for s in get_registry():
        for f in s.fit_signal.features + s.fit_signal.negative_features:
            assert f in known_fields or f in derived or f.split(":")[0] in {"tech", "social", "attr"}, (
                s.key,
                f,
            )
            assert resolve(f) in (True, False)


def _business(**kw) -> Business:
    base = dict(
        name="Test Co",
        website="https://test.com",
        domain="test.com",
        phone_e164="+15125550000",
        review_count=120,
    )
    base.update(kw)
    return Business(**base)


def test_busy_and_growing_levels():
    eng = ScoringEngine()
    sub = get_registry().get("plumbing_hvac")
    assert eng.score(_business(review_count=20), sub).by_name()["busy_and_growing"] == 0
    assert eng.score(_business(review_count=80), sub).by_name()["busy_and_growing"] == 1  # recency unknown
    stale = _business(review_count=80, review_insights=ReviewInsights(analysed_reviews=5, recent_reviews=0))
    assert eng.score(stale, sub).by_name()["busy_and_growing"] == 1
    fresh = _business(review_count=80, review_insights=ReviewInsights(analysed_reviews=5, recent_reviews=3))
    assert eng.score(fresh, sub).by_name()["busy_and_growing"] == 2


def test_admin_pain_levels():
    eng = ScoringEngine()
    sub = get_registry().get("plumbing_hvac")
    none = _business(website_features=WebsiteFeatures(crawled=True, has_online_booking=True))
    assert eng.score(none, sub).by_name()["admin_pain"] == 0
    call_only = _business(website_features=WebsiteFeatures(crawled=True, says_call_for_quote=True))
    assert eng.score(call_only, sub).by_name()["admin_pain"] == 1
    both = _business(
        website_features=WebsiteFeatures(crawled=True, says_call_for_quote=True),
        review_insights=ReviewInsights(analysed_reviews=5, pain_mentions=1, pain_categories={"billing": 1}),
    )
    assert eng.score(both, sub).by_name()["admin_pain"] == 2


def test_multi_tool_levels():
    eng = ScoringEngine()
    sub = get_registry().get("plumbing_hvac")
    one = _business(
        tech_stack=TechStack(
            technologies=[
                Technology(name="Square", category="payments"),
                Technology(name="WordPress", category="cms"),
            ]
        )
    )
    assert eng.score(one, sub).by_name()["multi_tool"] == 0
    two = _business(
        tech_stack=TechStack(
            technologies=[
                Technology(name="Square", category="payments"),
                Technology(name="Mailchimp", category="email_marketing"),
            ]
        )
    )
    assert eng.score(two, sub).by_name()["multi_tool"] == 1
    three = _business(
        tech_stack=TechStack(
            technologies=[
                Technology(name="Square", category="payments"),
                Technology(name="Shopify", category="ecommerce"),
                Technology(name="Mailchimp", category="email_marketing"),
            ]
        ),
        website_features=WebsiteFeatures(
            crawled=True, booking_provider="vagaro.com", has_online_booking=True
        ),
    )
    s = eng.score(three, sub)
    assert s.by_name()["multi_tool"] == 2
    assert "4 tool categories" in s.signals[2].evidence[0]


def test_owner_reachable_levels():
    eng = ScoringEngine()
    sub = get_registry().get("plumbing_hvac")
    assert eng.score(_business(), sub).by_name()["owner_reachable"] == 0
    role_only = _business(owner=OwnerContact(email="info@test.com"))
    assert eng.score(role_only, sub).by_name()["owner_reachable"] == 0
    named = _business(website_features=WebsiteFeatures(crawled=True, owner_name_candidates=["Jane Doe"]))
    assert eng.score(named, sub).by_name()["owner_reachable"] == 1
    named_role = _business(owner=OwnerContact(name="Jane Doe", email="info@test.com", phone="+15125550000"))
    assert eng.score(named_role, sub).by_name()["owner_reachable"] == 1
    direct = _business(owner=OwnerContact(name="Jane Doe", email="jane@test.com"))
    assert eng.score(direct, sub).by_name()["owner_reachable"] == 2
    linkedin = _business(owner=OwnerContact(name="Jane Doe", linkedin_url="https://linkedin.com/in/jane"))
    assert eng.score(linkedin, sub).by_name()["owner_reachable"] == 2


def test_subsector_signal_rules():
    eng = ScoringEngine()
    reg = get_registry()
    plumb = _business(
        website_features=WebsiteFeatures(crawled=True, emergency_service=True, maintenance_plan=True)
    )
    assert eng.score(plumb, reg.get("plumbing_hvac")).by_name()["subsector_signal"] == 2
    plumb1 = _business(website_features=WebsiteFeatures(crawled=True, emergency_service=True))
    assert eng.score(plumb1, reg.get("plumbing_hvac")).by_name()["subsector_signal"] == 1

    elec_commercial = _business(website_features=WebsiteFeatures(crawled=True, commercial_focus=True))
    assert eng.score(elec_commercial, reg.get("electrical")).by_name()["subsector_signal"] == 0

    salon = _business(
        website_features=WebsiteFeatures(crawled=True, chair_count_hint=8, retail_products=True)
    )
    assert (
        eng.score(salon, reg.get("beauty_salons")).by_name()["subsector_signal"] == 2
    )  # 3 of 3 (no booking)

    gift = _business(website_features=WebsiteFeatures(crawled=True, online_store=True, store_platform="etsy"))
    assert eng.score(gift, reg.get("gift_souvenir_stores")).by_name()["subsector_signal"] == 2

    florist_wire = _business(domain="www.ftd.com", website_features=WebsiteFeatures(crawled=True))
    assert eng.score(florist_wire, reg.get("florists")).by_name()["subsector_signal"] == 0

    boutique = _business(
        website_features=WebsiteFeatures(
            crawled=True, instagram_shop_signals=True, social_links={"instagram": "https://instagram.com/x"}
        )
    )
    assert eng.score(boutique, reg.get("womens_clothing_stores")).by_name()["subsector_signal"] == 2


def test_rank_and_select_marks_top_n():
    eng = ScoringEngine()
    sub = get_registry().get("plumbing_hvac")
    items = []
    for i, reviews in enumerate((300, 150, 60, 20)):
        b = _business(name=f"B{i}", review_count=reviews)
        b.score = eng.score(b, sub)
        items.append(b)
    rejected = _business(name="Rejected", status=BusinessStatus.REJECTED)
    selected = rank_and_select(items + [rejected], top_n=2)
    assert [b.name for b in selected] == ["B0", "B1"]
    assert items[0].status == BusinessStatus.SELECTED and items[0].score.rank == 1
    assert items[2].status == BusinessStatus.SCORED and items[2].score.rank == 3
    assert rejected.status == BusinessStatus.REJECTED
