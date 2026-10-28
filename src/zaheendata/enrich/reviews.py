"""Review analysis: recency ("busy and growing") and admin-pain mentions (slow callbacks,
billing/invoicing problems, scheduling chaos). Rule based and deterministic; an LLM classifier
can be plugged in through ``ReviewAnalyzer.classifier``."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

from ..models import Review, ReviewInsights, utcnow
from ..settings import Settings, get_settings

PAIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "slow_callback": re.compile(
        r"(never|didn'?t|did not|no one|nobody|still haven'?t|took (days|weeks|forever) to) (call(ed)? (me )?back|return(ed)? (my|our) call|get back|respond|responded|reply|replied|hear(d)? back)"
        r"|(no|zero|lack of) (response|communication|follow.?up|call ?back)"
        r"|(hard|impossible|difficult) to (reach|get (a ?)?hold of|get in touch)"
        r"|(left|leave) (several|multiple|\d+|a) (voicemails?|messages?)"
        r"|(unanswered|ignored) (calls?|emails?|messages?)"
        r"|(waited|waiting) (\d+|two|three|several) (days|weeks) (for|to)",
        re.IGNORECASE,
    ),
    "billing": re.compile(
        r"(over|double|wrongly|incorrectly|mis)[- ]?(charged|billed)|billing (issue|problem|error|mistake|nightmare)"
        r"|(wrong|incorrect|inaccurate|confusing|surprise) (invoice|bill|charge|price|amount)"
        r"|invoice (was|is) (wrong|incorrect|late|never)|never (received|got) (an |the |my )?(invoice|receipt|bill)"
        r"|charged (me )?(for|twice|more than)|hidden fees|refund (took|never|still)|credit card (was )?charged",
        re.IGNORECASE,
    ),
    "scheduling": re.compile(
        r"(no.?show|no one showed|never showed|didn'?t show)|(missed|forgot|lost) (my|our|the) (appointment|booking|order)"
        r"|(rescheduled|cancelled|canceled) (on (me|us)|last minute|\d+ times|multiple times)"
        r"|(showed up|arrived) (\d+ )?(hours?|days?) late|double.?booked|(wrong|mixed up) (date|time|order)",
        re.IGNORECASE,
    ),
    "quote_estimate": re.compile(
        r"(never|still) (got|received|sent) (the |a |my )?(quote|estimate)|(quote|estimate) (took|never came)"
        r"|waiting (on|for) (a |the |my )?(quote|estimate)|final (bill|price) (was )?(way )?(higher|more) than (the )?(quote|estimate)",
        re.IGNORECASE,
    ),
}


class ReviewAnalyzer:
    def __init__(self, settings: Settings | None = None, classifier: Callable[[str], set[str]] | None = None):
        self.settings = settings or get_settings()
        self.classifier = classifier

    def analyse(self, reviews: list[Review]) -> ReviewInsights:
        insights = ReviewInsights(analysed_reviews=len(reviews))
        cutoff = utcnow() - timedelta(days=self.settings.recent_review_window_days)
        for r in reviews:
            if r.published_at:
                if r.published_at >= cutoff:
                    insights.recent_reviews += 1
                if insights.last_review_at is None or r.published_at > insights.last_review_at:
                    insights.last_review_at = r.published_at
            text = r.text or ""
            if not text:
                continue
            hits = {cat for cat, rx in PAIN_PATTERNS.items() if rx.search(text)}
            if self.classifier:
                hits |= set(self.classifier(text))
            if hits:
                insights.pain_mentions += 1
                for h in hits:
                    insights.pain_categories[h] = insights.pain_categories.get(h, 0) + 1
                if len(insights.pain_examples) < 3:
                    insights.pain_examples.append(text[:220])
        return insights
