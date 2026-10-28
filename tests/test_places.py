from zaheendata.sources.google_places import place_to_lead
from zaheendata.subsectors import get_subsector


def test_place_to_lead_maps_core_fields() -> None:
    place = {
        "id": "places/abc123",
        "displayName": {"text": "Intelligent Air Services"},
        "websiteUri": "http://www.iairservices.com",
        "nationalPhoneNumber": "(512) 323-2500",
        "rating": 4.8,
        "userRatingCount": 187,
        "types": ["hvac_contractor", "plumber"],
        "formattedAddress": "305 Victor St, Austin, TX 78753",
        "location": {"latitude": 30.33, "longitude": -97.73},
        "addressComponents": [
            {"shortText": "305", "types": ["street_number"]},
            {"shortText": "Victor St", "types": ["route"]},
            {"shortText": "Austin", "types": ["locality"]},
            {"shortText": "TX", "types": ["administrative_area_level_1"]},
            {"shortText": "78753", "types": ["postal_code"]},
        ],
        "reviews": [{"publishTime": "2026-10-01T00:00:00Z"}],
        "businessStatus": "OPERATIONAL",
    }
    lead = place_to_lead(place, get_subsector("plumbing_hvac"))
    assert lead.website == "https://www.iairservices.com/"
    assert lead.phone == "+15123232500"
    assert lead.google_reviews == 187
    assert lead.city == "Austin"
    assert lead.google_place_id == "abc123"
    assert "google_places" in lead.sources
    assert lead.recent_reviews_90d == 1
