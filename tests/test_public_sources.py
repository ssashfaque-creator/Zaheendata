"""Free / government data sources: Socrata registries, SAM.gov, SBA PPP, Census CBP, owner waterfall."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from zaheendata.census import CensusCbpClient, CensusKeyRequired, assess_scope, parse_cbp_rows, state_fips
from zaheendata.enrich.owner import OwnerEnricher
from zaheendata.http import HttpClient, ResponseCache
from zaheendata.models import Address, Business, OwnerContact, SourceKind, SourceSearchSpec, WebsiteFeatures
from zaheendata.settings import get_settings
from zaheendata.sources.base import SourceContext
from zaheendata.sources.ppp_loans import PppLoansSource, build_state_index, ppp_distributions
from zaheendata.sources.ppp_loans import row_to_record as ppp_row_to_record
from zaheendata.sources.public_datasets import (
    PublicDataset,
    PublicDatasetSource,
    _naics_clause,
    _naics_in_row,
    _person_name,
    _regex_to_like,
    load_public_datasets,
    socrata_metadata_url,
)
from zaheendata.sources.sam_gov import ENTITIES_URL, SamGovSource, parse_entity


def _ctx(tmp_path: Path) -> SourceContext:
    settings = get_settings()
    return SourceContext(
        http=HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 10)), settings=settings
    )


def _spec(**kw) -> SourceSearchSpec:
    base = dict(
        subsector_key="plumbing_hvac",
        naics="238220",
        queries=["plumber", "HVAC contractor"],
        region="TX",
        location_text="Austin, TX",
        employee_min=5,
        employee_max=19,
        limit=100,
    )
    base.update(kw)
    return SourceSearchSpec(**base)


# ---------------------------------------------------------------------------- public datasets
def test_public_datasets_config_loads_and_has_all_roles():
    datasets = load_public_datasets()
    keys = {d.key for d in datasets}
    assert {
        "tx_sales_tax_permits",
        "tx_tdlr_licenses",
        "ny_dos_corporations",
        "sf_registered_businesses",
    } <= keys
    assert any(d.long_list and d.naics_column for d in datasets)
    assert any(d.owner_lookup and d.lookup_name_column for d in datasets)
    assert any(d.tags for d in datasets)


def test_regex_to_like_skips_regex_alternatives():
    assert _regex_to_like("license_type", "electric") == "(upper(license_type) like '%ELECTRIC%')"
    like = _regex_to_like("license_type", "air condition|refrigeration|B\\b")
    assert like is not None and "REFRIGERATION" in like and "\\b" not in like


def test_naics_clause_modes():
    base = dict(
        key="x", name="x", type="socrata", url="https://h/resource/aaaa-1111.json", columns={"name": "n"}
    )
    text = PublicDataset(**base, naics_column="naics")
    assert _naics_clause(text, "238220") == "starts_with(naics, '238220')"
    two = PublicDataset(**base, naics_column="naics", naics_prefix_length=2)
    assert _naics_clause(two, "238220") == "starts_with(naics, '23')"
    number = PublicDataset(**base, naics_column="code", naics_match="number")
    assert _naics_clause(number, "238220") == "code = 238220"
    number4 = PublicDataset(**base, naics_column="code", naics_match="number", naics_prefix_length=4)
    assert _naics_clause(number4, "238220") == "code >= 238200 AND code < 238300"
    contains = PublicDataset(**base, naics_column="naics_code", naics_match="contains")
    assert _naics_clause(contains, "238220") == "naics_code like '%(238220)%'"
    assert _naics_in_row(contains, {"naics_code": "Plumbing Contractors (238220)"}, "238220") is True
    assert _naics_in_row(contains, {"naics_code": "Limousine Service (485320)"}, "238220") is False
    assert socrata_metadata_url("https://data.texas.gov/resource/jrea-zgmq.json") == (
        "https://data.texas.gov/api/views/jrea-zgmq.json"
    )
    assert socrata_metadata_url("https://example.com/file.csv") is None


def test_person_name_formats():
    assert _person_name("OCHOA, ERIC JAMES", last_first=True) == "Eric James Ochoa"
    assert _person_name("Ochoa, Eric James", last_first=True) == "Eric James Ochoa"
    assert _person_name("JANE DOE", last_first=False) == "Jane Doe"
    assert _person_name("Jane Doe", last_first=True) == "Jane Doe"


@respx.mock
async def test_public_datasets_dba_and_last_first_principal(tmp_path: Path):
    def seattle(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        assert "starts_with(naics_code, '238220')" in qs["$where"][0]
        return httpx.Response(
            200,
            json=[
                {
                    "business_legal_name": "EMERALD CITY MECHANICAL LLC",
                    "trade_name": "EMERALD CITY HEATING",
                    "naics_code": "238220",
                    "street_address": "1 PIKE ST",
                    "city": "SEATTLE",
                    "state": "WA",
                    "zip": "98101",
                    "business_phone": "2065550100",
                }
            ],
        )

    def wa_li(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        where = qs["$where"][0]
        assert "contractorlicensestatus = 'ACTIVE'" in where
        assert "upper(specialtycode1desc) like '%PLUMBING%'" in where
        return httpx.Response(
            200,
            json=[
                {
                    "businessname": "EMERALD CITY MECHANICAL LLC",
                    "contractorlicensenumber": "EMERACM123",
                    "contractorlicensestatus": "ACTIVE",
                    "specialtycode1desc": "PLUMBING",
                    "primaryprincipalname": "NGUYEN, LINH T",
                    "city": "SEATTLE",
                    "state": "WA",
                }
            ],
        )

    respx.get("https://cos-data.seattle.gov/resource/wnbq-64tb.json").mock(side_effect=seattle)
    respx.get("https://data.wa.gov/resource/m8qx-ubtq.json").mock(side_effect=wa_li)
    ctx = _ctx(tmp_path)
    recs = [r async for r in PublicDatasetSource(ctx).search(_spec(region="WA", location_text="Seattle, WA"))]
    await ctx.http.aclose()
    by_source = {r.source: r for r in recs}
    sea = by_source["business_license:seattle_business_licenses"]
    assert (
        sea.name == "EMERALD CITY HEATING" and sea.attributes["legal_name"] == "EMERALD CITY MECHANICAL LLC"
    )
    li = by_source["license:wa_li_contractors"]
    assert li.owner_name == "Linh T Nguyen" and li.owner_title == "Principal (L&I registration)"


def test_ppp_distributions_from_dkan_search():
    payload = {
        "results": {
            "dkan_dataset/SBA-OCA-2022-07-001": {
                "title": "Paycheck Protection Program (PPP) FOIA",
                "distribution": [
                    {
                        "title": "PPP Data Dictionary.xlsx",
                        "format": "xlsx",
                        "downloadURL": "https://x/d.xlsx",
                    },
                    {
                        "title": "public_150k_plus_240930.csv",
                        "format": "csv",
                        "downloadURL": "https://x/a.csv",
                    },
                    {
                        "title": "public_up_to_150k_1_240930.csv",
                        "format": "csv",
                        "downloadURL": "https://x/b.csv",
                    },
                ],
            },
            "dkan_dataset/other": {
                "title": "7(a) loans",
                "distribution": [
                    {"title": "public_x.csv", "format": "csv", "downloadURL": "https://x/no.csv"}
                ],
            },
        }
    }
    assert [d["url"] for d in ppp_distributions(payload)] == ["https://x/a.csv", "https://x/b.csv"]


@respx.mock
async def test_public_datasets_naics_long_list_and_licence_cross_check(tmp_path: Path):
    seen_where: dict[str, str] = {}

    def tax_permits(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        seen_where["tx_sales_tax_permits"] = qs["$where"][0]
        return httpx.Response(
            200,
            json=[
                {
                    "outlet_name": "ACME PLUMBING LLC",
                    "outlet_address": "1 MAIN ST",
                    "outlet_city": "AUSTIN",
                    "outlet_state": "TX",
                    "outlet_zip_code": "78701",
                    "outlet_naics_code": "238220",
                    "taxpayer_name": "JANE DOE",
                    "taxpayer_organization_type": "CL",
                }
            ],
        )

    def tdlr(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        seen_where["tx_tdlr_licenses"] = qs["$where"][0]
        return httpx.Response(
            200,
            json=[
                {
                    "business_name": "ACME PLUMBING",
                    "license_number": "TACLA1",
                    "license_subtype": "ACR",
                    "license_type": "A/C Contractor",
                    "owner_name": "DOE, JANE",
                },
                {  # keyword regex is also enforced client-side
                    "business_name": "BARBER BOB",
                    "license_number": "B1",
                    "license_type": "Barber Shop",
                },
            ],
        )

    respx.get("https://data.texas.gov/resource/jrea-zgmq.json").mock(side_effect=tax_permits)
    respx.get("https://data.texas.gov/resource/7358-krk7.json").mock(side_effect=tdlr)
    ctx = _ctx(tmp_path)
    src = PublicDatasetSource(ctx)
    recs = [r async for r in src.search(_spec())]
    await ctx.http.aclose()

    by_source = {r.source: r for r in recs}
    assert set(by_source) == {"tax_registry:tx_sales_tax_permits", "license:tx_tdlr_licenses"}
    assert "outlet_naics_code = 238220" in seen_where["tx_sales_tax_permits"]  # numeric column
    assert "upper(outlet_state) = 'TX'" in seen_where["tx_sales_tax_permits"]
    assert "upper(outlet_city) = 'AUSTIN'" in seen_where["tx_sales_tax_permits"]
    assert "upper(license_type) like '%A/C CONTRACTOR%'" in seen_where["tx_tdlr_licenses"]
    permit = by_source["tax_registry:tx_sales_tax_permits"]
    assert permit.source_kind == SourceKind.REGISTRY and permit.naics == "238220"
    assert permit.owner_name == "Jane Doe" and permit.attributes["dataset_role"] == "long_list"
    assert permit.address is not None and permit.address.postal_code == "78701"
    licence = by_source["license:tx_tdlr_licenses"]
    assert licence.source_kind == SourceKind.LICENSE and licence.license_number == "TACLA1"
    assert licence.owner_name == "Doe, Jane"  # TDLR owner_name is not flagged last-first (mixed formats)
    assert licence.attributes["dataset_role"] == "cross_check"
    # datasets for other states / cities are not queried at all
    assert len(respx.calls) == 2


@respx.mock
async def test_registry_owner_lookup_fuzzy_match(tmp_path: Path):
    def ny_dos(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        assert "upper(current_entity_name) like 'BROOKLYN%'" in qs["$where"][0]
        return httpx.Response(
            200,
            json=[
                {"current_entity_name": "BROOKLYN BAGELS INC", "chairman_name": "SOMEONE ELSE"},
                {"current_entity_name": "BROOKLYN PLUMBING & HEATING, INC.", "chairman_name": "MARIA LOPEZ"},
            ],
        )

    respx.get("https://data.ny.gov/resource/n9v6-gdp6.json").mock(side_effect=ny_dos)
    ctx = _ctx(tmp_path)
    src = PublicDatasetSource(ctx)
    owner = await src.lookup_owner("Brooklyn Plumbing & Heating", "NY")
    assert owner is not None
    assert owner.name == "Maria Lopez" and owner.title == "Chairman (NY DOS filing)"
    assert owner.source == "registry:ny_dos_corporations" and owner.confidence == pytest.approx(0.55)
    assert await src.lookup_owner("Brooklyn Plumbing & Heating", "WY") is None  # no registry configured
    await ctx.http.aclose()


# ---------------------------------------------------------------------------- SAM.gov
SAM_ENTITY = {
    "entityRegistration": {
        "ueiSAM": "ABC123",
        "cageCode": "1AB23",
        "legalBusinessName": "LONE STAR MECHANICAL SERVICES LLC",
        "dbaName": "Lone Star Comfort",
        "registrationStatus": "Active",
        "registrationDate": "2021-03-01",
    },
    "coreData": {
        "entityInformation": {"entityURL": "https://lonestarcomfort.com", "entityStartDate": "2015-01-01"},
        "generalInformation": {"entityStructureDesc": "Limited Liability Company"},
        "physicalAddress": {
            "addressLine1": "500 Congress Ave",
            "city": "Austin",
            "stateOrProvinceCode": "TX",
            "zipCode": "78701",
            "countryCode": "USA",
        },
    },
    "assertions": {
        "goodsAndServices": {
            "naicsList": [
                {
                    "naicsCode": "238220",
                    "naicsDescription": "Plumbing, Heating, and Air-Conditioning Contractors",
                    "sbaSmallBusiness": "Y",
                },
                {"naicsCode": "561720", "naicsDescription": "Janitorial Services"},
            ]
        }
    },
    "pointsOfContact": {
        "governmentBusinessPOC": {"firstName": "RAY", "lastName": "DELGADO", "title": "Owner"},
        "electronicBusinessPOC": {"firstName": "PAT", "lastName": "SMITH", "title": "Office Manager"},
    },
}


def test_parse_sam_entity_prefers_owner_poc_and_dba():
    rec = parse_entity(SAM_ENTITY, "238220")
    assert rec is not None
    assert rec.source == "sam_gov" and rec.source_kind == SourceKind.REGISTRY
    assert rec.name == "Lone Star Comfort" and rec.website == "https://lonestarcomfort.com"
    assert rec.address is not None and rec.address.region == "TX" and rec.address.postal_code == "78701"
    assert rec.naics == "238220"
    assert rec.owner_name == "Ray Delgado" and rec.owner_title == "Owner"
    assert rec.attributes["legal_name"] == "LONE STAR MECHANICAL SERVICES LLC"
    assert rec.attributes["small_business"] is True


def test_sam_gov_unavailable_without_key():
    assert SamGovSource.available(get_settings()) is False
    assert "ZAHEEN_SAM_GOV_API_KEY" in (SamGovSource.unavailable_reason(get_settings()) or "")


@respx.mock
async def test_sam_gov_search_pages_and_find_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZAHEEN_SAM_GOV_API_KEY", "sam-key")
    get_settings.cache_clear()

    def responder(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        assert qs["api_key"] == ["sam-key"] and qs["registrationStatus"] == ["A"]
        if "legalBusinessName" in qs or "dbaName" in qs:
            return httpx.Response(200, json={"totalRecords": 1, "entityData": [SAM_ENTITY]})
        assert qs["naicsCode"] == ["238220"] and qs["physicalAddressProvinceOrStateCode"] == ["TX"]
        assert qs["physicalAddressCity"] == ["AUSTIN"]
        page = int(qs.get("page", ["0"])[0])
        second = {
            **SAM_ENTITY,
            "entityRegistration": {
                **SAM_ENTITY["entityRegistration"],
                "ueiSAM": "DEF456",
                "dbaName": None,
                "legalBusinessName": "OTHER LLC",
            },
        }
        data = [SAM_ENTITY] * 10 if page == 0 else [second]
        return httpx.Response(200, json={"totalRecords": 11, "entityData": data})

    respx.get(ENTITIES_URL).mock(side_effect=responder)
    ctx = _ctx(tmp_path)
    src = SamGovSource(ctx)
    recs = [r async for r in src.search(_spec())]
    assert len(recs) == 11 and recs[-1].name == "OTHER LLC" and recs[-1].source_id == "DEF456"
    assert ctx.http.ledger.calls["sam_gov.entities"] == 2

    owner = await src.find_owner("Lone Star Comfort", "TX")
    assert owner is not None and owner.name == "Ray Delgado" and owner.source == "sam_gov"
    assert await src.find_owner("Completely Different Co", "TX") is None
    await ctx.http.aclose()


# ---------------------------------------------------------------------------- SBA PPP
PPP_HEADER = (
    "LoanNumber,DateApproved,BorrowerName,BorrowerAddress,BorrowerCity,BorrowerState,BorrowerZip,"
    "FranchiseName,JobsReported,NAICSCode,BusinessType,BusinessAgeDescription,NonProfit,"
    "InitialApprovalAmount,ForgivenessAmount,ProcessingMethod,Gender\n"
)
PPP_ROWS = [
    "1,05/01/2020,ACME PLUMBING LLC,1 MAIN ST,AUSTIN,TX,78701-1234,,12,238220,Limited  Liability Company(LLC),Existing or more than 2 years old,,100000,100500,PPP,Male Owned",
    "2,02/01/2021,ACME PLUMBING LLC,1 MAIN ST,AUSTIN,TX,78701-1234,,12,238220,Limited  Liability Company(LLC),Existing or more than 2 years old,,90000,90000,PPS,Male Owned",
    "3,05/01/2020,MR. ROOTER OF AUSTIN,2 OAK ST,AUSTIN,TX,78702,Mr. Rooter,9,238220,Corporation,Existing or more than 2 years old,,50000,50000,PPP,Unanswered",
    "4,05/01/2020,BIG CORP HVAC,3 ELM ST,AUSTIN,TX,78703,,120,238220,Corporation,Existing or more than 2 years old,,2000000,0,PPP,Unanswered",
    "5,05/01/2020,DALLAS HEATING CO,4 PINE ST,DALLAS,TX,75201,,7,238220,Sole Proprietorship,New Business or 2 years or less,,20000,20000,PPP,Unanswered",
    "6,05/01/2020,CHURCH PLUMBING MINISTRY,5 ASH ST,AUSTIN,TX,78704,,8,238220,Non-Profit Organization,Existing or more than 2 years old,Y,20000,20000,PPP,Unanswered",
    "7,05/01/2020,SOME SALON,6 FIR ST,AUSTIN,TX,78705,,6,812112,Corporation,Existing or more than 2 years old,,20000,20000,PPP,Unanswered",
    "8,05/01/2020,DENVER PLUMBING,7 ASPEN ST,DENVER,CO,80202,,10,238220,Corporation,Existing or more than 2 years old,,20000,20000,PPP,Unanswered",
]


def _write_ppp(dir_: Path) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "public_up_to_150k_1.csv").write_text(PPP_HEADER + "\n".join(PPP_ROWS[:4]) + "\n")
    (dir_ / "public_150k_plus.csv").write_text(PPP_HEADER + "\n".join(PPP_ROWS[4:]) + "\n")


def test_ppp_row_to_record():
    row = dict(zip(PPP_HEADER.strip().split(","), PPP_ROWS[2].split(","), strict=True))
    rec = ppp_row_to_record(row)
    assert rec is not None and rec.source == "sba_ppp" and rec.source_kind == SourceKind.REGISTRY
    assert rec.name == "Mr. Rooter Of Austin" and rec.employee_count == 9 and rec.naics == "238220"
    assert rec.attributes["franchise_flag"] == "Mr. Rooter"
    assert rec.address is not None and rec.address.postal_code == "78702"


def test_ppp_state_index(tmp_path: Path):
    _write_ppp(tmp_path / "ppp")
    counts = build_state_index(sorted((tmp_path / "ppp").glob("*.csv")), tmp_path / "ppp" / "_by_state")
    assert counts == {"TX": 7, "CO": 1}
    assert (tmp_path / "ppp" / "_by_state" / "TX.csv").exists()
    assert (tmp_path / "ppp" / "_by_state" / "_complete").exists()


async def test_ppp_source_filters_by_naics_city_jobs_and_dedupes(tmp_path: Path):
    settings = get_settings()
    _write_ppp(settings.ppp_data_dir)
    assert PppLoansSource.available(settings) is True
    ctx = _ctx(tmp_path)
    src = PppLoansSource(ctx)

    austin = [r async for r in src.search(_spec())]
    names = sorted(r.name for r in austin)
    # dedupes first/second draw, drops >19 jobs, non-profits, other NAICS and other cities
    assert names == ["Acme Plumbing Llc", "Mr. Rooter Of Austin"]
    acme = next(r for r in austin if r.name.startswith("Acme"))
    assert acme.employee_count == 12 and acme.attributes["business_type"].startswith("Limited")

    statewide = [r async for r in src.search(_spec(location_text="Texas", region="TX"))]
    assert sorted(r.name for r in statewide) == [
        "Acme Plumbing Llc",
        "Dallas Heating Co",
        "Mr. Rooter Of Austin",
    ]
    assert (
        next(r for r in statewide if r.name.startswith("Dallas")).attributes["individual_structure"] is True
    )

    assert [r async for r in src.search(_spec(region=None))] == []
    await ctx.http.aclose()


# ---------------------------------------------------------------------------- Census CBP
def test_parse_cbp_rows_and_scope_recommendation():
    rows = [
        ["NAME", "ESTAB", "EMPSZES", "NAICS2017", "state", "county"],
        ["Travis County, Texas", "900", "001", "238220", "48", "453"],
        ["Travis County, Texas", "400", "220", "238220", "48", "453"],
        ["Travis County, Texas", "300", "230", "238220", "48", "453"],
        ["Bastrop County, Texas", "40", "001", "238220", "48", "021"],
        ["Bastrop County, Texas", "12", "220", "238220", "48", "021"],
        ["Bastrop County, Texas", "5", "230", "238220", "48", "021"],
    ]
    counts = parse_cbp_rows(rows)
    assert [c.geo_name for c in counts] == ["Travis County, Texas", "Bastrop County, Texas"]
    assert counts[0].estab_5_19 == 700 and counts[0].estab_all == 900 and counts[0].geo_code == "48453"
    plan = assess_scope(counts, 400)
    assert plan["recommendation"] == "local" and plan["total_estab_5_19"] == 717
    assert assess_scope(counts[1:], 400)["recommendation"] == "national"
    assert assess_scope([counts[1]] * 40, 400)["recommendation"] == "state"
    assert state_fips("tx") == "48" and state_fips("6") == "06"
    with pytest.raises(ValueError):
        state_fips("ZZ")


@respx.mock
async def test_census_client_builds_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    ctx = _ctx(tmp_path)
    with pytest.raises(CensusKeyRequired):
        await CensusCbpClient(ctx.http, ctx.settings).establishments("238220", "TX", "county")
    await ctx.http.aclose()
    monkeypatch.setenv("ZAHEEN_CENSUS_API_KEY", "census-key")
    get_settings.cache_clear()

    def responder(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlsplit(str(request.url)).query)
        assert qs["for"] == ["county:*"] and qs["in"] == ["state:48"]
        assert qs["NAICS2017"] == ["238220"] and qs["EMPSZES"] == ["001,220,230"]
        assert qs["key"] == ["census-key"]
        return httpx.Response(
            200,
            json=[
                ["NAME", "ESTAB", "EMPSZES", "NAICS2017", "state", "county"],
                ["Travis County, Texas", "400", "220", "238220", "48", "453"],
            ],
        )

    respx.get(url__regex=r"https://api\.census\.gov/data/\d+/cbp.*").mock(side_effect=responder)
    ctx = _ctx(tmp_path)
    counts = await CensusCbpClient(ctx.http, ctx.settings).establishments("238220", "TX", "county")
    assert counts[0].estab_5_9 == 400
    assert ctx.http.ledger.calls["census.cbp"] == 1
    await ctx.http.aclose()


# ---------------------------------------------------------------------------- owner waterfall
async def test_owner_enricher_waterfall(tmp_path: Path):
    calls: list[str] = []

    async def sam(name: str, region: str | None) -> OwnerContact | None:
        calls.append(f"sam:{name}:{region}")
        return None

    async def registry(name: str, region: str | None) -> OwnerContact | None:
        calls.append(f"registry:{name}:{region}")
        return OwnerContact(
            name="Maria Lopez", title="Chief executive officer", source="registry:ny", confidence=0.55
        )

    ctx = _ctx(tmp_path)
    enricher = OwnerEnricher(ctx.http, ctx.settings, [("sam_gov", sam), ("state_registry", registry)])
    b = Business(
        campaign_id="c",
        name="Brooklyn Plumbing & Heating",
        normalized_name="brooklyn plumbing heating",
        phone_e164="+17185550100",
        address=Address(city="Brooklyn", region="NY"),
        website_features=WebsiteFeatures(
            crawled=True,
            emails=["info@brooklynplumbing.com", "maria@brooklynplumbing.com"],
            social_links={"linkedin": "https://www.linkedin.com/company/brooklyn-plumbing"},
        ),
    )
    owner = await enricher.enrich(b)
    assert calls == ["sam:Brooklyn Plumbing & Heating:NY", "registry:Brooklyn Plumbing & Heating:NY"]
    assert owner.name == "Maria Lopez" and owner.title == "Chief executive officer"
    assert owner.email == "maria@brooklynplumbing.com"  # non-role mailbox preferred
    assert owner.phone == "+17185550100"
    assert owner.linkedin_url is None  # company page is not a personal contact

    # a record that already names the owner with an email skips the lookups entirely
    calls.clear()
    b.owner = OwnerContact(name="Jane Doe", email="jane@x.com", source="license:tx")
    owner = await enricher.enrich(b)
    assert calls == [] and owner.name == "Jane Doe"
    await ctx.http.aclose()
