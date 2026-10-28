from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from zaheendata.normalize import looks_like_company, looks_like_person
from zaheendata.pipeline.export import read_csv


def _filled(rows: list[dict[str, str]], column: str) -> int:
    return sum(1 for row in rows if (row.get(column) or "").strip() not in {"", "0", "False", "false"})


def diagnose_csv(path: Path) -> dict[str, Any]:
    rows = read_csv(path)
    sources: Counter[str] = Counter()
    for row in rows:
        for part in (row.get("sources") or "").split(";"):
            part = part.strip()
            if part:
                sources[part] += 1
    owners = [row.get("owner_name") or "" for row in rows]
    return {
        "path": str(path),
        "rows": len(rows),
        "status": dict(Counter(row.get("status") or "" for row in rows)),
        "sources": dict(sources),
        "website": _filled(rows, "website"),
        "website_crawled": sum(1 for row in rows if str(row.get("website_crawled")).lower() == "true"),
        "phone": _filled(rows, "phone"),
        "owner_email": _filled(rows, "owner_email"),
        "google_place_id": _filled(rows, "google_place_id"),
        "google_reviews": _filled(rows, "google_reviews"),
        "license_number": _filled(rows, "license_number"),
        "employee_count": _filled(rows, "employee_count"),
        "owner_person": sum(1 for name in owners if looks_like_person(name)),
        "owner_company": sum(1 for name in owners if looks_like_company(name)),
        "scores": dict(Counter(row.get("score_total") or "" for row in rows)),
        "selected_with_website": sum(
            1
            for row in rows
            if row.get("status") == "selected" and (row.get("website") or "").strip()
        ),
    }


def render_report(stats: dict[str, Any]) -> str:
    rows = stats["rows"] or 1
    def pct(count: int) -> str:
        return f"{count} ({100 * count / rows:.1f}%)"

    problems = []
    if stats["website"] / rows < 0.2:
        problems.append("Almost no websites. Discovery is not Google Places / Maps.")
    if stats["google_place_id"] == 0:
        problems.append("Zero Google Place IDs, so review counts and maps profiles were never attached.")
    if stats["website_crawled"] == 0:
        problems.append("No pages were crawled, so booking/tech-stack/email fields stay empty.")
    if stats["owner_company"] > stats["owner_person"]:
        problems.append("Owner names are legal-entity names, not people. Do not score that as owner_reachable.")
    if stats["license_number"] == 0:
        problems.append("State contractor licenses were not joined, despite being the PDF cross-check source.")
    if set(stats.get("scores", {})) <= {"", "0", "1"}:
        problems.append("Every score is 0 or 1, so the top-100 cut is arbitrary.")

    lines = [
        f"File: {stats['path']}",
        f"Rows: {stats['rows']}",
        f"Status: {stats['status']}",
        f"Sources: {stats['sources']}",
        f"Website: {pct(stats['website'])}",
        f"Website crawled: {pct(stats['website_crawled'])}",
        f"Phone: {pct(stats['phone'])}",
        f"Owner email: {pct(stats['owner_email'])}",
        f"Google place id: {pct(stats['google_place_id'])}",
        f"Google reviews: {pct(stats['google_reviews'])}",
        f"License number: {pct(stats['license_number'])}",
        f"Employee count: {pct(stats['employee_count'])}",
        f"Owner looks like a person: {stats['owner_person']}",
        f"Owner looks like a company: {stats['owner_company']}",
        f"Scores: {stats['scores']}",
        f"Selected with a website: {stats['selected_with_website']}",
        "",
        "Diagnosis:",
    ]
    lines.extend(f"- {item}" for item in problems or ["Coverage looks usable."])
    return "\n".join(lines)
