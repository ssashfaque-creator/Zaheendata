from zaheendata.normalize import (
    looks_like_company,
    looks_like_person,
    name_similarity,
    normalize_name,
    normalize_phone,
    person_display_name,
)


def test_normalize_strips_legal_suffixes() -> None:
    assert normalize_name("The Coolest LLC") == "coolest"
    assert normalize_name("Hot Rod Mechanical, Inc.") == "hot rod mechanical"


def test_person_vs_company() -> None:
    assert looks_like_person("SKOSKI, MICHAEL JOSEPH")
    assert looks_like_person("Akram A Saleh")
    assert not looks_like_person("Intelligent Air Services, Llc")
    assert looks_like_company("Intelligent Air Services, Llc")


def test_person_display_flips_last_first() -> None:
    assert person_display_name("BURGOS, DAVID") == "DAVID BURGOS"


def test_phone_and_similarity() -> None:
    assert normalize_phone("(512) 323-2500") == "+15123232500"
    assert name_similarity("Lake Travis Energy Doctor", "LAKE TRAVIS ENERGY DOCTOR") == 1.0
    assert name_similarity("Kindred Service LLC", "KINDRED SERVICES") >= 0.72
