"""Export the directory as CSV / XLSX / JSON with one flat row per business."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .compliance import evaluate
from .models import Business

COLUMNS = [
    "rank",
    "score_total",
    "busy_and_growing",
    "admin_pain",
    "multi_tool",
    "owner_reachable",
    "subsector_signal",
    "status",
    "rejection_reason",
    "name",
    "phone",
    "website",
    "domain",
    "address",
    "city",
    "state",
    "postal_code",
    "lat",
    "lng",
    "google_place_id",
    "google_rating",
    "google_reviews",
    "recent_reviews_90d",
    "last_review_at",
    "naics",
    "employee_count",
    "categories",
    "owner_name",
    "owner_title",
    "owner_email",
    "owner_phone",
    "owner_linkedin",
    "owner_source",
    "owner_confidence",
    "email_is_role_account",
    "suppressed",
    "suppression_reasons",
    "online_booking",
    "booking_provider",
    "quote_form",
    "says_call_for_quote",
    "emergency_service",
    "maintenance_plan",
    "recurring_service",
    "free_estimate",
    "online_store",
    "store_platform",
    "sells_gift_cards",
    "hosts_events",
    "instagram",
    "tech_stack",
    "tool_categories",
    "pain_mentions",
    "pain_examples",
    "score_evidence",
    "sources",
    "license_number",
    "license_status",
    "business_id",
]


def flatten(b: Business, suppressions: dict[str, set[str]] | None = None) -> dict[str, Any]:
    s = b.score.by_name() if b.score else {}
    wf = b.website_features
    ri = b.review_insights
    flags = evaluate(b, suppressions)
    addr = b.address
    tools = sorted(t.name for t in b.tech_stack.technologies)
    evidence = " | ".join(
        f"{sig.name}={sig.points}: " + "; ".join(sig.evidence) for sig in (b.score.signals if b.score else [])
    )
    return {
        "rank": b.score.rank if b.score else None,
        "score_total": b.score.total if b.score else None,
        "busy_and_growing": s.get("busy_and_growing"),
        "admin_pain": s.get("admin_pain"),
        "multi_tool": s.get("multi_tool"),
        "owner_reachable": s.get("owner_reachable"),
        "subsector_signal": s.get("subsector_signal"),
        "status": b.status.value,
        "rejection_reason": b.rejection_reason,
        "name": b.name,
        "phone": b.phone_e164,
        "website": b.website,
        "domain": b.domain,
        "address": (addr.formatted or addr.line1) if addr else None,
        "city": addr.city if addr else None,
        "state": addr.region if addr else None,
        "postal_code": addr.postal_code if addr else None,
        "lat": b.location.lat if b.location else None,
        "lng": b.location.lng if b.location else None,
        "google_place_id": b.google_place_id,
        "google_rating": b.rating,
        "google_reviews": b.review_count,
        "recent_reviews_90d": ri.recent_reviews if ri.analysed_reviews else None,
        "last_review_at": ri.last_review_at.date().isoformat() if ri.last_review_at else None,
        "naics": b.naics,
        "employee_count": b.employee_count or b.employee_range,
        "categories": "; ".join(b.categories[:6]),
        "owner_name": b.owner.name,
        "owner_title": b.owner.title,
        "owner_email": b.owner.email,
        "owner_phone": b.owner.phone,
        "owner_linkedin": b.owner.linkedin_url,
        "owner_source": b.owner.source,
        "owner_confidence": b.owner.confidence or None,
        "email_is_role_account": flags.email_is_role_account,
        "suppressed": flags.suppressed,
        "suppression_reasons": "; ".join(flags.suppression_reasons),
        "online_booking": wf.has_online_booking,
        "booking_provider": wf.booking_provider,
        "quote_form": wf.has_quote_form,
        "says_call_for_quote": wf.says_call_for_quote,
        "emergency_service": wf.emergency_service,
        "maintenance_plan": wf.maintenance_plan,
        "recurring_service": wf.recurring_service,
        "free_estimate": wf.free_estimate,
        "online_store": wf.online_store,
        "store_platform": wf.store_platform,
        "sells_gift_cards": wf.sells_gift_cards,
        "hosts_events": wf.hosts_events,
        "instagram": wf.social_links.get("instagram"),
        "tech_stack": "; ".join(tools),
        "tool_categories": "; ".join(sorted(b.tech_stack.categories)),
        "pain_mentions": ri.pain_mentions,
        "pain_examples": " || ".join(ri.pain_examples),
        "score_evidence": evidence,
        "sources": "; ".join(b.sources),
        "license_number": b.license_number,
        "license_status": b.license_status,
        "business_id": b.id,
    }


def to_csv(businesses: Iterable[Business], suppressions: dict[str, set[str]] | None = None) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for b in businesses:
        writer.writerow(flatten(b, suppressions))
    return buf.getvalue()


def to_xlsx(businesses: Iterable[Business], suppressions: dict[str, set[str]] | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Directory"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for b in businesses:
        row = flatten(b, suppressions)
        ws.append([row.get(c) for c in COLUMNS])
    for idx, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = min(max(12, len(col) + 2), 40)
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_json(businesses: Iterable[Business]) -> str:
    return json.dumps([json.loads(b.model_dump_json()) for b in businesses], indent=2)


def write(path: Path, businesses: list[Business], suppressions: dict[str, set[str]] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        path.write_bytes(to_xlsx(businesses, suppressions))
    elif suffix == ".json":
        path.write_text(to_json(businesses), encoding="utf-8")
    else:
        path.write_text(to_csv(businesses, suppressions), encoding="utf-8")
    return path
