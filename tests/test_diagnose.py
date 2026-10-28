import csv
from pathlib import Path

from zaheendata.models import EXPORT_COLUMNS
from zaheendata.pipeline.diagnose import diagnose_csv


def test_diagnose_flags_empty_enrichment(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerow(
            {
                "name": "AIR CO LLC",
                "website": "",
                "website_crawled": "False",
                "owner_name": "Air Co Llc",
                "status": "selected",
                "sources": "tax_registry:tx_sales_tax_permits",
                "score_total": "1",
                "google_reviews": "0",
            }
        )
    stats = diagnose_csv(path)
    assert stats["rows"] == 1
    assert stats["website"] == 0
    assert stats["owner_company"] == 1
    assert stats["selected_with_website"] == 0
