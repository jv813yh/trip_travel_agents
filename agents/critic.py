"""Critic agent — independently evaluates the analyser's output.

Given the raw scraped data + the analyser's structured JSON + the schema, asks
Claude to verify that every claim in the analysis is supported by the raw data.
Catches hallucination: carriers fabricated, prices invented, properties not in
the source list, distance/coordinate mismatches.

Returns a verdict dict:
  {
    "valid": bool,
    "issues": [str, ...],          # human-readable problems
    "retry_hint": str | None,      # instruction for the analyser if retrying
  }

If ANTHROPIC_API_KEY is missing or the call fails, falls back to a
deterministic check (cross-references hotel_id / trip_id / price against raw).
"""

from __future__ import annotations

import json
import os
from typing import Any


SYSTEM_PROMPT = """\
You are a strict QA critic for a travel-planning agent. You receive:
- the RAW fetched data (accommodation, transport),
- the ANALYSIS the agent produced (top 2 stays + recommended transport),
- the OUTPUT SCHEMA the analysis was supposed to follow.

Your job: verify that every claim in ANALYSIS is supported by RAW data.
Specifically check:
  1. Every top_pick hotel_id MUST exist in raw accommodation.
  2. Every top_pick price / rating / distance / coordinates MUST match the raw row.
  3. The recommended transport carrier + price + duration MUST appear in raw transport.
  4. No fabricated carriers (e.g. claiming "Ryanair KSC-WAW" when no such row exists).
  5. All booking_link values must be non-null and look like real URLs.
  6. If raw transport is empty, recommendation must be null (don't invent one).

Reply with ONLY a JSON object:
{"valid": true|false, "issues": ["..."], "retry_hint": "..."}
- valid: true ONLY if zero issues found.
- issues: list each problem in one short sentence. Empty list if valid.
- retry_hint: if invalid, one short instruction to the analyser for the retry
  (e.g. "Do not recommend carriers absent from the raw transport list").
  null if valid.
"""


def critique(
    raw_accommodation: list[dict[str, Any]],
    raw_transport: list[dict[str, Any]],
    analysis: dict[str, Any],
    schema_hint: dict[str, Any],
) -> dict[str, Any]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _critique_with_claude(raw_accommodation, raw_transport, analysis, schema_hint)
        except Exception as exc:  # noqa: BLE001
            print(f"[critic] Claude call failed ({exc}); using deterministic fallback.")
    return _critique_deterministic(raw_accommodation, raw_transport, analysis)


def _critique_with_claude(
    raw_accommodation: list[dict[str, Any]],
    raw_transport: list[dict[str, Any]],
    analysis: dict[str, Any],
    schema_hint: dict[str, Any],
) -> dict[str, Any]:
    from anthropic import Anthropic

    client = Anthropic()
    user_msg = (
        f"OUTPUT SCHEMA:\n{json.dumps(schema_hint)}\n\n"
        f"RAW ACCOMMODATION ({len(raw_accommodation)} rows):\n"
        f"{json.dumps(raw_accommodation, ensure_ascii=False)}\n\n"
        f"RAW TRANSPORT ({len(raw_transport)} rows):\n"
        f"{json.dumps(raw_transport, ensure_ascii=False)}\n\n"
        f"ANALYSIS:\n{json.dumps(analysis, ensure_ascii=False)}\n\n"
        "Validate per the rules in the system prompt. Return JSON only."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in critic response")
    return json.loads(text[start : end + 1])


def _critique_deterministic(
    raw_accommodation: list[dict[str, Any]],
    raw_transport: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Mechanical cross-check against the raw input lists."""
    issues: list[str] = []

    hotel_ids = {a["hotel_id"] for a in raw_accommodation}
    for pick in analysis.get("accommodation", {}).get("top_picks", []) or []:
        hid = pick.get("hotel_id")
        if hid not in hotel_ids:
            issues.append(f"top_pick hotel_id '{hid}' not found in raw accommodation")
        if not pick.get("booking_link"):
            issues.append(f"top_pick '{pick.get('name')}' has no booking_link")

    trip_ids = {t["trip_id"] for t in raw_transport}
    carriers = {t.get("carrier", "").lower() for t in raw_transport}
    transport = analysis.get("transport", {}) or {}
    rec_type = transport.get("recommendation")
    if rec_type and not raw_transport:
        issues.append("transport recommendation provided but raw transport is empty")
    for opt in transport.get("options", []) or []:
        if opt.get("trip_id") not in trip_ids:
            issues.append(f"transport trip_id '{opt.get('trip_id')}' not in raw")
        if (opt.get("carrier") or "").lower() not in carriers and raw_transport:
            issues.append(f"transport carrier '{opt.get('carrier')}' not in raw")
        if not opt.get("booking_link"):
            issues.append(f"transport option '{opt.get('carrier')}' has no booking_link")

    return {
        "valid": not issues,
        "issues": issues,
        "retry_hint": (
            "Only use hotel_ids / trip_ids / carriers that appear in the raw data; "
            "do not fabricate options."
            if issues
            else None
        ),
    }
