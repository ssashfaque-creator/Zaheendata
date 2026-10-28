from zaheendata.models import empty_lead
from zaheendata.pipeline.match import attach_licenses


def test_matches_license_by_name_and_copies_person_owner() -> None:
    lead = empty_lead(name="Kindred Service", phone="+15123354004")
    licenses = [
        {
            "source": "tdlr:a/c_contractor",
            "business_name": "KINDRED SERVICES",
            "owner_name": "Alex Rivera",
            "owner_is_person": True,
            "license_number": "20445",
            "license_status": "active",
            "phone": "",
        }
    ]
    attach_licenses([lead], licenses)
    assert lead.license_number == "20445"
    assert lead.owner_name == "Alex Rivera"
    assert "tdlr:a/c_contractor" in lead.sources


def test_phone_match_wins() -> None:
    lead = empty_lead(name="Something Else", phone="512-555-0100")
    licenses = [
        {
            "source": "tsbpe_rmp",
            "business_name": "Unrelated",
            "owner_name": "Pat Smith",
            "owner_is_person": True,
            "license_number": "RMP1",
            "phone": "+1 (512) 555-0100",
        }
    ]
    attach_licenses([lead], licenses)
    assert lead.license_number == "RMP1"
    assert lead.owner_name == "Pat Smith"
