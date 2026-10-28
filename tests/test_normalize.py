from zaheendata.normalize import (
    is_own_website,
    is_usable_email,
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
    assert looks_like_person("Jane Plumber")
    assert looks_like_person("CHARLES RAY WILKS")
    assert looks_like_person("CHRIS B NEMEC")
    assert not looks_like_person("Malco Electric")
    assert not looks_like_person("Lighting Installation")
    assert not looks_like_person("At Brooks Custom")
    assert not looks_like_person("Support Filters Warranty")
    assert not looks_like_person("Intelligent Air Services, Llc")
    assert looks_like_company("Intelligent Air Services, Llc")


def test_person_display_flips_last_first() -> None:
    assert person_display_name("BURGOS, DAVID") == "David Burgos"


def test_placeholder_emails_are_not_usable() -> None:
    assert is_usable_email("chuck@chucksac.com")
    assert not is_usable_email("user@domain.com")
    assert not is_usable_email("email@domain.com")
    assert not is_usable_email("you@email.com")


def test_directory_hosts_are_not_own_sites() -> None:
    assert is_own_website("https://coolair.example/")
    assert not is_own_website("https://www.yelp.com/biz/cool-air")
    assert not is_own_website("https://homeadvisor.com/rated.x")
    assert not is_own_website("https://facebook.com/coolairatx")


def test_phone_and_similarity() -> None:
    assert normalize_phone("(512) 323-2500") == "+15123232500"
    assert name_similarity("Lake Travis Energy Doctor", "LAKE TRAVIS ENERGY DOCTOR") == 1.0
    assert name_similarity("Kindred Service LLC", "KINDRED SERVICES") >= 0.72
