from zaheendata.models import empty_lead
from zaheendata.pipeline.crawl import apply_signals, extract_signals


HTML = """
<html>
  <head>
    <script src="https://cdn.shopify.com/s/files/x.js"></script>
    <script src="https://js.stripe.com/v3"></script>
  </head>
  <body>
    <p>Call for a quote. 24/7 emergency service. Ask about our maintenance plan.</p>
    <p>Owned by Jane Plumber</p>
    <a href="mailto:jane@coolair.example">email Jane</a>
    <a href="https://instagram.com/coolairatx">Instagram</a>
    <script src="https://cdn.getjobber.com/widget.js"></script>
    <a href="/contact">Contact</a>
  </body>
</html>
"""


def test_extract_hvac_signals() -> None:
    signals = extract_signals(HTML, "https://coolair.example/")
    assert signals["emergency_service"] is True
    assert signals["maintenance_plan"] is True
    assert signals["says_call_for_quote"] is True
    assert signals["booking_provider"] == "jobber"
    assert "jane@coolair.example" in signals["emails"]
    assert signals["owner_on_site"] == "Jane Plumber"
    assert "ecommerce" in signals["tool_categories"]
    assert "payments" in signals["tool_categories"]
    assert "field_service" in signals["tool_categories"]
    assert "shopify" in signals["tech_stack"]
    assert "jobber" in signals["tech_stack"]
    assert "stripe" in signals["tech_stack"]


def test_jsonld_and_site_extras() -> None:
    html = """
    <html><body>
      <script type="application/ld+json">
        {"@type":"HVACBusiness","email":"shop@coolair.example",
         "telephone":"+1-512-555-0199","foundingDate":"1998",
         "sameAs":["https://www.facebook.com/coolairatx","https://youtube.com/coolair"]}
      </script>
      <p>Family owned since 1998. Licensed and insured. Financing available.</p>
      <a href="tel:5125550100">call</a>
      <script src="https://www.greensky.com/widget.js"></script>
    </body></html>
    """
    signals = extract_signals(html, "https://coolair.example/")
    assert signals["founded_year"] == "1998"
    assert signals["family_owned"] is True
    assert signals["licensed_bonded_insured"] is True
    assert "greensky" in signals["financing"]
    assert "facebook.com/coolairatx" in signals["facebook"]
    assert "shop@coolair.example" in signals["emails"]
    assert "+15125550100" in signals["extra_phones"] or "+15125550199" in signals["extra_phones"]


def test_junk_owner_copy_is_ignored() -> None:
    html = "<p>Meet the owner At Brooks Custom. Support Filters Warranty.</p>"
    signals = extract_signals(html, "https://brookscustomcomfort.com/")
    assert signals["owner_on_site"] == ""


def test_apply_signals_sets_email_and_owner() -> None:
    lead = empty_lead(name="Cool Air")
    apply_signals(lead, extract_signals(HTML, "https://coolair.example/"))
    assert lead.owner_email == "jane@coolair.example"
    assert lead.email_is_role_account is False
    assert lead.owner_name == "Jane Plumber"
    assert lead.website_crawled is True
    assert lead.emergency_service is True
