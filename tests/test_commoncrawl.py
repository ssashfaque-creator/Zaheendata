import gzip

from zaheendata.fingerprints import detect_tools
from zaheendata.sources.commoncrawl import parse_warc_payload


def test_parse_warc_payload_extracts_html() -> None:
    inner = (
        b"WARC/1.0\r\nWARC-Type: response\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
        b"<html><script src='https://cdn.shopify.com/s.js'></script>"
        b"<script src='https://qbo.intuit.com/x.js'></script></html>"
    )
    html = parse_warc_payload(gzip.compress(inner))
    assert "cdn.shopify.com" in html
    names = {row["name"] for row in detect_tools(html)}
    assert names >= {"shopify", "quickbooks"}
