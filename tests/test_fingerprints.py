from zaheendata.fingerprints import detect_tools, summarize_tools
from zaheendata.pipeline.crawl import extract_signals


def test_detects_shopify_quickbooks_jobber() -> None:
    html = """
    <html>
      <script src="https://cdn.shopify.com/s/files/x.js"></script>
      <script src="https://qbo.intuit.com/qbo/js"></script>
      <p>Pay invoices with QuickBooks.</p>
      <script src="https://cdn.getjobber.com/widget.js"></script>
    </html>
    """
    matches = detect_tools(html)
    names = {row["name"] for row in matches}
    assert names >= {"shopify", "quickbooks", "jobber"}
    summary = summarize_tools(matches)
    assert summary["store_platform"] == "shopify"
    assert summary["booking_provider"] == "jobber"
    assert "accounting" in summary["tool_categories"]
    assert "ecommerce" in summary["tool_categories"]


def test_extract_signals_uses_fingerprint_names() -> None:
    html = '<script src="https://js.stripe.com/v3"></script><script src="https://cdn.shopify.com/s.js"></script>'
    signals = extract_signals(html, "https://gift.example/")
    assert "shopify" in signals["tech_stack"]
    assert "stripe" in signals["tech_stack"]
    assert signals["store_platform"] == "shopify"
    assert signals["online_store"] is True
