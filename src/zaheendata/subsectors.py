from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subsector:
    key: str
    name: str
    naics: str
    google_types: tuple[str, ...]
    search_queries: tuple[str, ...]
    license_source: str
    fit_signal: str
    overture_categories: tuple[str, ...] = ()
    signal_keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)
    wide_area: bool = False
    min_reviews_keep: int = 10


SUBSECTORS: dict[str, Subsector] = {
    "plumbing_hvac": Subsector(
        key="plumbing_hvac",
        name="Plumbing, heating and HVAC",
        naics="238220",
        google_types=("plumber", "hvac_contractor"),
        search_queries=("plumber", "HVAC contractor", "heating and air conditioning"),
        license_source="tdlr_ac_contractor+tsbpe_rmp",
        fit_signal="Offers emergency service and maintenance plans",
        overture_categories=("plumbing", "hvac_service", "heating_and_air_conditioning"),
        signal_keywords={
            "emergency_service": (
                "24/7",
                "24-7",
                "24 hour",
                "emergency",
                "after hours",
                "same day",
            ),
            "maintenance_plan": (
                "maintenance plan",
                "membership",
                "service agreement",
                "tune-up",
                "tune up",
                "comfort club",
                "maintenance club",
                "hvac protection",
            ),
        },
    ),
    "electrical": Subsector(
        key="electrical",
        name="Electrical contractors",
        naics="238210",
        google_types=("electrician",),
        search_queries=("electrician", "electrical contractor"),
        license_source="tdlr_electrical_contractor",
        fit_signal="Residential service work rather than commercial bids",
        overture_categories=("electrician", "electrician_service"),
        signal_keywords={
            "residential": ("residential", "home electrical", "panel upgrade", "homeowner"),
            "commercial_only": ("commercial only", "design-build", "plan spec", "bid only"),
        },
    ),
    "landscaping": Subsector(
        key="landscaping",
        name="Landscaping",
        naics="561730",
        google_types=("landscaper", "lawn_care_service"),
        search_queries=("landscaper", "lawn care", "landscape maintenance"),
        license_source="none",
        fit_signal="Recurring maintenance contracts and several crews",
        overture_categories=("landscaping", "lawn_service", "gardener", "tree_service", "irrigation_service"),
        signal_keywords={
            "recurring_service": (
                "weekly",
                "bi-weekly",
                "maintenance contract",
                "commercial mowing",
                "crew",
            ),
        },
    ),
    "remodelers": Subsector(
        key="remodelers",
        name="Residential remodelers",
        naics="236118",
        google_types=("general_contractor", "home_improvement_store"),
        search_queries=("home remodeler", "kitchen remodel", "bathroom remodel"),
        license_source="contractor_license_lists",
        fit_signal="Many estimates per month; project gallery online",
        overture_categories=("bathroom_remodeling", "kitchen_remodeling", "carpenter", "flooring_contractor"),
        signal_keywords={
            "gallery": ("gallery", "our work", "before and after", "portfolio"),
            "quote_form": ("free estimate", "request a quote", "get a quote"),
        },
    ),
    "roofing": Subsector(
        key="roofing",
        name="Roofing",
        naics="238160",
        google_types=("roofing_contractor",),
        search_queries=("roofer", "roofing contractor", "roof inspection"),
        license_source="contractor_license_lists",
        fit_signal="Inspection request form; insurance and storm work",
        overture_categories=("roofing",),
        signal_keywords={
            "inspection": ("roof inspection", "free inspection", "inspection request"),
            "storm": ("storm", "insurance claim", "hail"),
        },
    ),
    "pest_control": Subsector(
        key="pest_control",
        name="Pest control",
        naics="561710",
        google_types=("pest_control_service",),
        search_queries=("pest control", "exterminator"),
        license_source="state_pest_control_licenses",
        fit_signal="Quarterly service plans with recurring billing",
        overture_categories=("pest_control_service",),
        wide_area=True,
        signal_keywords={
            "recurring_service": ("quarterly", "monthly service", "pest-free plan", "recurring"),
        },
    ),
    "painting": Subsector(
        key="painting",
        name="Painting",
        naics="238320",
        google_types=("painter",),
        search_queries=("house painter", "residential painter"),
        license_source="none",
        fit_signal='Residential work with "free estimate" offers',
        overture_categories=("painting",),
        signal_keywords={
            "free_estimate": ("free estimate", "free quote"),
            "residential": ("interior", "exterior", "residential"),
        },
    ),
    "other_building": Subsector(
        key="other_building",
        name="Other building services",
        naics="561790",
        google_types=("plumber",),
        search_queries=(
            "pool cleaner",
            "gutter cleaning",
            "chimney sweep",
            "pressure washing",
        ),
        license_source="none",
        fit_signal="Recurring route-based service",
        overture_categories=("pool_cleaning", "gutter_service", "chimney_sweep", "pressure_washing"),
        wide_area=True,
        signal_keywords={
            "recurring_service": ("monthly", "route", "membership", "weekly service"),
        },
    ),
    "beauty_salons": Subsector(
        key="beauty_salons",
        name="Beauty salons",
        naics="812112",
        google_types=("beauty_salon", "hair_salon", "hair_care"),
        search_queries=("hair salon", "beauty salon"),
        license_source="tdlr_cosmetology_establishment",
        fit_signal="5+ chairs, sells retail products, phone-only or basic booking",
        overture_categories=("beauty_salon", "hair_salon"),
        signal_keywords={
            "retail": ("retail", "product bar", "salon professional"),
            "booking": ("book now", "vagaro", "fresha", "booksy", "styleseat"),
        },
    ),
    "nail_salons": Subsector(
        key="nail_salons",
        name="Nail salons",
        naics="812113",
        google_types=("nail_salon",),
        search_queries=("nail salon",),
        license_source="tdlr_cosmetology_establishment",
        fit_signal="Walk-in heavy, sells gift cards, no online booking",
        overture_categories=("nail_salon",),
        signal_keywords={
            "gift_cards": ("gift card", "gift certificate"),
            "walk_in": ("walk-in", "walk in", "no appointment"),
        },
    ),
    "janitorial": Subsector(
        key="janitorial",
        name="Janitorial",
        naics="561720",
        google_types=("cleaning_service",),
        search_queries=("janitorial", "commercial cleaning", "office cleaning"),
        license_source="state_business_registrations",
        fit_signal="Several commercial clients and staff schedules",
        overture_categories=("home_cleaning", "cleaning_service"),
        signal_keywords={
            "commercial": ("commercial cleaning", "office building", "nightly"),
        },
    ),
    "florists": Subsector(
        key="florists",
        name="Florists",
        naics="453110",
        google_types=("florist",),
        search_queries=("florist", "flower shop"),
        license_source="saf_ftd_teleflora",
        fit_signal="Wedding and event work; own online store, not wire orders only",
        overture_categories=("florist",),
        wide_area=True,
        signal_keywords={
            "events": ("wedding", "event", "bridal"),
            "online_store": ("shop now", "add to cart", "shopify"),
        },
    ),
    "pet_supply": Subsector(
        key="pet_supply",
        name="Pet-supply stores",
        naics="453910",
        google_types=("pet_store",),
        search_queries=("pet store", "pet supply"),
        license_source="none",
        fit_signal="In-store grooming (bookings plus retail)",
        overture_categories=("pet_store",),
        wide_area=True,
        signal_keywords={
            "grooming": ("grooming", "book a groom"),
        },
    ),
    "sporting_goods": Subsector(
        key="sporting_goods",
        name="Sporting-goods stores",
        naics="451110",
        google_types=("sporting_goods_store",),
        search_queries=("sporting goods", "team uniforms"),
        license_source="none",
        fit_signal="Team and uniform sales needing quotes and invoices",
        overture_categories=("sporting_goods_store",),
        wide_area=True,
        signal_keywords={
            "team": ("team sales", "uniforms", "custom jersey", "invoice"),
        },
    ),
    "gift_stores": Subsector(
        key="gift_stores",
        name="Gift and souvenir stores",
        naics="453220",
        google_types=("gift_shop",),
        search_queries=("gift shop", "souvenir shop"),
        license_source="none",
        fit_signal="Sells in-store and on Shopify or Etsy",
        overture_categories=("gift_shop",),
        wide_area=True,
        signal_keywords={
            "online_store": ("shopify", "etsy", "add to cart"),
        },
    ),
    "womens_clothing": Subsector(
        key="womens_clothing",
        name="Women's clothing stores",
        naics="448120",
        google_types=("clothing_store", "women's_clothing_store"),
        search_queries=("women's boutique", "women's clothing store"),
        license_source="none",
        fit_signal="Sells through Instagram or live sales as well as the shop",
        overture_categories=("womens_clothing_store", "fashion_boutique"),
        wide_area=True,
        signal_keywords={
            "instagram": ("instagram", "live sale", "facebook live"),
        },
    ),
    "hobby": Subsector(
        key="hobby",
        name="Hobby, toy and game stores",
        naics="451120",
        google_types=("toy_store",),
        search_queries=("hobby shop", "game store", "toy store"),
        license_source="astra_directory",
        fit_signal="Hosts events or tournaments; has an online store",
        overture_categories=("toy_store",),
        wide_area=True,
        signal_keywords={
            "hosts_events": ("tournament", "game night", "friday night magic", "event"),
            "online_store": ("shop now", "add to cart"),
        },
    ),
}


# Cities used for Places locationBias. Radius is metres (Places max 50_000).
CITY_CENTERS = {
    ("austin", "tx"): (30.2672, -97.7431, 45_000),
    ("dallas", "tx"): (32.7767, -96.7970, 50_000),
    ("houston", "tx"): (29.7604, -95.3698, 50_000),
    ("san antonio", "tx"): (29.4241, -98.4936, 50_000),
}


def get_subsector(key: str) -> Subsector:
    if key not in SUBSECTORS:
        known = ", ".join(sorted(SUBSECTORS))
        raise KeyError(f"Unknown subsector '{key}'. Choose one of: {known}")
    return SUBSECTORS[key]
