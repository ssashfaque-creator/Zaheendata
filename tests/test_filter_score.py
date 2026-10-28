from zaheendata.models import empty_lead
from zaheendata.pipeline.filter import classify
from zaheendata.pipeline.score import rank_leads, score_lead
from zaheendata.subsectors import get_subsector


def test_franchise_rejected() -> None:
    lead = empty_lead(name="Mr Rooter Plumbing of Greater Austin", website="https://example.com", google_reviews=80)
    classify(lead, get_subsector("plumbing_hvac"))
    assert lead.status == "rejected"
    assert "mr rooter" in lead.rejection_reason


def test_electrical_off_taxonomy_rejected() -> None:
    lead = empty_lead(name="NXP Semiconductors", website="https://nxp.com")
    classify(lead, get_subsector("electrical"), require_reviews=False)
    assert lead.status == "rejected"
    assert "off-taxonomy" in lead.rejection_reason


def test_directory_website_rejected() -> None:
    lead = empty_lead(
        name="Local HVAC",
        website="https://www.homeadvisor.com/rated.localhvac",
        google_reviews=80,
    )
    classify(lead, get_subsector("plumbing_hvac"))
    assert lead.status == "rejected"
    assert "directory" in lead.rejection_reason


def test_national_path_skips_review_floor() -> None:
    lead = empty_lead(name="Local HVAC", website="https://localhvac.example")
    classify(lead, get_subsector("plumbing_hvac"), require_reviews=False)
    assert lead.status == "enrich"


def test_no_website_rejected() -> None:
    lead = empty_lead(name="Local HVAC", google_reviews=40)
    classify(lead, get_subsector("plumbing_hvac"))
    assert lead.status == "rejected"
    assert lead.rejection_reason == "no website"


def test_low_reviews_rejected() -> None:
    lead = empty_lead(
        name="Tiny Shop",
        website="https://tiny.example",
        google_place_id="place123",
        google_reviews=3,
    )
    classify(lead, get_subsector("plumbing_hvac"))
    assert lead.status == "rejected"


def test_hvac_score_uses_site_and_reviews() -> None:
    lead = empty_lead(
        name="Austin Air Co",
        website="https://austinairexample.com",
        owner_name="Maria Lopez",
        owner_email="maria@austinairexample.com",
        owner_source="tdlr:a/c_contractor",
        google_reviews=120,
        recent_reviews_90d=4,
        emergency_service=True,
        maintenance_plan=True,
        says_call_for_quote=True,
        online_booking=False,
        quote_form=False,
        tool_categories="payments,email,booking",
        tech_stack="squareup.com,mailchimp.com,servicetitan.com",
    )
    score_lead(lead, get_subsector("plumbing_hvac"))
    assert lead.busy_and_growing == 2
    assert lead.admin_pain == 1
    assert lead.multi_tool == 2
    assert lead.owner_reachable == 2
    assert lead.subsector_signal == 2
    assert lead.score_total == 9


def test_llc_name_is_not_owner_reachable() -> None:
    lead = empty_lead(name="Air Co", owner_name="Air Co Llc", website="https://x.example")
    score_lead(lead, get_subsector("plumbing_hvac"))
    assert lead.owner_reachable == 0


def test_rank_keeps_top_n() -> None:
    leads = []
    for index in range(5):
        lead = empty_lead(name=f"Co {index}", website="https://x.example", google_reviews=10 + index)
        score_lead(lead, get_subsector("plumbing_hvac"))
        leads.append(lead)
    ranked = rank_leads(leads, keep=2)
    assert [lead.status for lead in ranked[:2]] == ["selected", "selected"]
    assert ranked[2].status == "scored"
