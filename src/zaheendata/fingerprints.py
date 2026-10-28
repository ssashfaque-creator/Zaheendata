from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fingerprint:
    name: str
    category: str
    needles: tuple[str, ...]


# SMB stack we actually care about: accounting, field service, commerce,
# booking, payments, marketing. Needles match script URLs, meta tags, and copy.
FINGERPRINTS: tuple[Fingerprint, ...] = (
    Fingerprint("quickbooks", "accounting", ("quickbooks.intuit.com", "qbo.intuit.com", "intuitcdn.net", "payments.intuit.com", "quickbooks")),
    Fingerprint("xero", "accounting", ("xero.com", "go.xero.com")),
    Fingerprint("freshbooks", "accounting", ("freshbooks.com",)),
    Fingerprint("sage", "accounting", ("sage.com", "sagesoftware")),
    Fingerprint("wave", "accounting", ("waveapps.com",)),
    Fingerprint("servicetitan", "field_service", ("servicetitan.com", "st.com/embed")),
    Fingerprint("housecall_pro", "field_service", ("housecallpro.com", "housecall.co")),
    Fingerprint("jobber", "field_service", ("getjobber.com", "cdn.getjobber.com")),
    Fingerprint("service_fusion", "field_service", ("servicefusion.com",)),
    Fingerprint("workiz", "field_service", ("workiz.com",)),
    Fingerprint("fieldedge", "field_service", ("fieldedge.com",)),
    Fingerprint("successware", "field_service", ("successware.com",)),
    Fingerprint("service_monster", "field_service", ("servicemonster.com",)),
    Fingerprint("kickserv", "field_service", ("kickserv.com",)),
    Fingerprint("mhelpdesk", "field_service", ("mhelpdesk.com",)),
    Fingerprint("fieldpulse", "field_service", ("fieldpulse.com",)),
    Fingerprint("gorilladesk", "field_service", ("gorilladesk.com",)),
    Fingerprint("jobnimbus", "field_service", ("jobnimbus.com",)),
    Fingerprint("acculynx", "field_service", ("acculynx.com",)),
    Fingerprint("buildertrend", "field_service", ("buildertrend.com", "buildertrend.net")),
    Fingerprint("companycam", "field_service", ("companycam.com",)),
    Fingerprint("shopify", "ecommerce", ("cdn.shopify.com", "myshopify.com", "shopify.com/s/")),
    Fingerprint("woocommerce", "ecommerce", ("woocommerce", "wp-content/plugins/woocommerce")),
    Fingerprint("bigcommerce", "ecommerce", ("bigcommerce.com", "cdn.bcapp")),
    Fingerprint("squarespace", "cms", ("squarespace.com", "sqsp.net", "squarespace-cdn.com")),
    Fingerprint("wix", "cms", ("wix.com", "wixstatic.com", "parastorage.com")),
    Fingerprint("wordpress", "cms", ("wp-content", "wp-includes", "wordpress.com")),
    Fingerprint("webflow", "cms", ("webflow.com", "uploads-ssl.webflow.com")),
    Fingerprint("weebly", "cms", ("weebly.com", "weeblycloud.com")),
    Fingerprint("godaddy", "cms", ("godaddy.com/websites", "godaddysites.com")),
    Fingerprint("duda", "cms", ("duda.co", "multiscreensite.com")),
    Fingerprint("square", "payments", ("squareup.com", "square.site", "js.squareup.com", "squareup.com/appointments")),
    Fingerprint("stripe", "payments", ("js.stripe.com", "checkout.stripe.com")),
    Fingerprint("clover", "payments", ("clover.com",)),
    Fingerprint("paypal", "payments", ("paypal.com/sdk", "paypalobjects.com")),
    Fingerprint("authorize_net", "payments", ("authorize.net",)),
    Fingerprint("mailchimp", "email", ("list-manage.com", "mailchimp.com", "chimpstatic.com")),
    Fingerprint("klaviyo", "email", ("klaviyo.com", "static.klaviyo.com")),
    Fingerprint("constant_contact", "email", ("constantcontact.com", "ctctcdn.com")),
    Fingerprint("hubspot", "crm", ("hs-scripts.com", "hubspot.com", "hsforms.com", "js.hs-analytics.net")),
    Fingerprint("salesforce", "crm", ("salesforce.com", "force.com")),
    Fingerprint("zoho", "crm", ("zoho.com", "zohocdn.com")),
    Fingerprint("gohighlevel", "crm", ("leadconnectorhq.com", "msgsndr.com", "gohighlevel.com")),
    Fingerprint("calendly", "booking", ("calendly.com",)),
    Fingerprint("acuity", "booking", ("acuityscheduling.com",)),
    Fingerprint("booksy", "booking", ("booksy.com",)),
    Fingerprint("vagaro", "booking", ("vagaro.com",)),
    Fingerprint("fresha", "booking", ("fresha.com",)),
    Fingerprint("styleseat", "booking", ("styleseat.com",)),
    Fingerprint("setmore", "booking", ("setmore.com",)),
    Fingerprint("launch27", "booking", ("launch27.com",)),
    Fingerprint("mindbody", "booking", ("mindbodyonline.com",)),
    Fingerprint("intercom", "chat", ("widget.intercom.io", "intercomcdn.com")),
    Fingerprint("tidio", "chat", ("tidio.co", "code.tidio.co")),
    Fingerprint("tawk", "chat", ("tawk.to", "embed.tawk.to")),
    Fingerprint("drift", "chat", ("js.driftt.com", "drift.com")),
    Fingerprint("zendesk", "chat", ("zdassets.com", "zendesk.com")),
    Fingerprint("callrail", "call_tracking", ("callrail.com", "cdn.callrail.com")),
    Fingerprint("calltrackingmetrics", "call_tracking", ("calltrackingmetrics.com",)),
    Fingerprint("invoca", "call_tracking", ("invoca.net",)),
    Fingerprint("birdeye", "reviews", ("birdeye.com",)),
    Fingerprint("podium", "reviews", ("podium.com", "cdn.podium.com")),
    Fingerprint("nicejob", "reviews", ("nicejob.com", "cdn.nicejob.co")),
    Fingerprint("google_analytics", "ads", ("googletagmanager.com", "google-analytics.com", "gtag/js")),
    Fingerprint("facebook_pixel", "ads", ("connect.facebook.net", "fbevents.js")),
    Fingerprint("google_ads", "ads", ("googleadservices.com", "doubleclick.net")),
    Fingerprint("etsy", "ecommerce", ("etsy.com",)),
    Fingerprint("angi", "marketplace", ("angi.com", "angieslist.com")),
    Fingerprint("thumbtack", "marketplace", ("thumbtack.com",)),
    Fingerprint("ringcentral", "phone", ("ringcentral.com",)),
    Fingerprint("nextiva", "phone", ("nextiva.com",)),
    Fingerprint("toast", "pos", ("toasttab.com",)),
    Fingerprint("shopify_inbox", "chat", ("shopifycloud.com/shopify-inbox",)),
    Fingerprint("greensky", "financing", ("greensky.com", "greenskycredit.com")),
    Fingerprint("synchrony", "financing", ("synchrony.com", "mysynchrony.com")),
    Fingerprint("wisetack", "financing", ("wisetack.com",)),
    Fingerprint("affirm", "financing", ("affirm.com", "cdn1.affirm.com")),
)

BOOKING_TOOLS = {
    "servicetitan",
    "housecall_pro",
    "jobber",
    "service_fusion",
    "workiz",
    "calendly",
    "acuity",
    "booksy",
    "vagaro",
    "fresha",
    "styleseat",
    "setmore",
    "launch27",
    "mindbody",
    "square",
    "fieldpulse",
    "gorilladesk",
    "jobnimbus",
}

STORE_TOOLS = {"shopify", "woocommerce", "bigcommerce", "etsy", "wix", "squarespace"}


def detect_tools(blob: str) -> list[dict[str, str]]:
    haystack = blob.lower()
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in FINGERPRINTS:
        for needle in item.needles:
            if needle.lower() in haystack and item.name not in seen:
                seen.add(item.name)
                found.append(
                    {
                        "name": item.name,
                        "category": item.category,
                        "evidence": needle,
                    }
                )
                break
    return found


def summarize_tools(matches: list[dict[str, str]]) -> dict[str, str]:
    names = [row["name"] for row in matches]
    categories = sorted({row["category"] for row in matches})
    booking = next((name for name in names if name in BOOKING_TOOLS), "")
    store = next((name for name in names if name in STORE_TOOLS), "")
    return {
        "tech_stack": ",".join(names),
        "tool_categories": ",".join(categories),
        "booking_provider": booking,
        "store_platform": store,
    }
