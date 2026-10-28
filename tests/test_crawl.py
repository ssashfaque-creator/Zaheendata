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
    assert "booking" in signals["tool_categories"]


def test_apply_signals_sets_email_and_owner() -> None:
    lead = empty_lead(name="Cool Air")
    apply_signals(lead, extract_signals(HTML, "https://coolair.example/"))
    assert lead.owner_email == "jane@coolair.example"
    assert lead.email_is_role_account is False
    assert lead.owner_name == "Jane Plumber"
    assert lead.website_crawled is True
    assert lead.emergency_service is True
