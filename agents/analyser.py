"""AI analyser agent — selects the top picks and transport recommendation.

Sends today's scored data to Claude with the system prompt from CLAUDE.md and
asks for the structured JSON in the documented output schema. If
ANTHROPIC_API_KEY is unavailable or the call fails, falls back to a
deterministic selection (top-2 by composite score; transport by total group
cost vs. travel time) so the pipeline always produces a result.
"""

from __future__ import annotations

import json
import os
from typing import Any

MODEL = "claude-sonnet-4-6"


def _system_prompt(config: dict[str, Any]) -> str:
    """Build the analyser prompt from config.yaml so trip facts stay current."""
    trip = config["trip"]
    acc = config["accommodation"]
    transport = config["transport"]
    return f"""\
You are a travel assistant helping plan a group trip to {trip['destination_city']}, Poland.

GROUP CONTEXT:
- {trip['group_size']} people travelling from {trip['origin_city']} ({trip['origin_airport']})
- Target accommodation area: {acc['target_address']}
- Travel dates: {trip['dates']['outbound']} to {trip['dates']['return']} ({acc.get('nights')} nights)
- Budget: max EUR {acc['max_price_per_night_eur']}/person/night for accommodation, max EUR {transport['budget_flight_eur']}/person for flights (one-way), max EUR {transport['budget_bus_eur']}/person for FlixBus (one-way)

YOUR TASK:
Given today's scraped data, select the TOP 2 accommodation options and TOP 1 transport option.
Accommodation is pre-scored (composite_score, 0-100, higher = better). Prefer higher scores but
apply judgement. For transport, compare available flights vs FlixBus considering total door-to-door
travel time for the group, not just ticket price, and recommend the better option with clear reasoning.

Output ONLY valid JSON matching the schema provided in the user message. Do not fabricate data.
If a field is unavailable set it to null. Be concise.
"""

OUTPUT_SCHEMA_HINT = {
    "run_date": "YYYY-MM-DD",
    "accommodation": {
        "top_picks": [
            {
                "rank": 1,
                "hotel_id": "str",
                "source": "str",
                "name": "str",
                "price_eur_per_night": 0,
                "rating": 0,
                "distance_km": 0,
                "composite_score": 0,
                "vs_yesterday_pct": None,
                "vs_7d_avg_pct": None,
                "alert_triggered": False,
                "booking_link": "str",
                "coordinates": {"lat": 0, "lng": 0},
                "rooms": 1,
                "total_group_cost_eur": 0,
            }
        ],
        "alerts": [],
    },
    "transport": {
        "recommendation": "flight|flixbus",
        "reasoning": "str",
        "options": [
            {
                "trip_id": "str",
                "type": "flight|flixbus",
                "carrier": "str",
                "price_eur_per_person": 0,
                "duration_min": 0,
                "departure": "str",
                "arrival": "str",
                "stops": None,
                "booking_link": "str",
                "total_group_cost_eur": 0,
                "date": "YYYY-MM-DD",
                "date_offset_days": 0,
            }
        ],
    },
}


def analyse(
    run_date: str,
    accommodation: list[dict[str, Any]],
    transport: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    config: dict[str, Any],
    critic_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the structured analysis JSON (Claude if available, else fallback)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            result = _analyse_with_claude(
                run_date, accommodation, transport, alerts, config, critic_feedback
            )
            # Alerts come from price_tracker (structured); don't let Claude rewrite them as prose.
            result.setdefault("accommodation", {})["alerts"] = alerts
            return result
        except Exception as exc:  # noqa: BLE001 — never let analysis break the run
            print(f"[analyser] Claude call failed ({exc}); using deterministic fallback.")
    return _analyse_deterministic(run_date, accommodation, transport, alerts, config)


def _analyse_with_claude(
    run_date: str,
    accommodation: list[dict[str, Any]],
    transport: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    config: dict[str, Any],
    critic_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from anthropic import Anthropic

    client = Anthropic()
    feedback_block = ""
    if critic_feedback and not critic_feedback.get("valid"):
        feedback_block = (
            "\n\nA prior attempt was REJECTED by the critic. Issues to fix:\n- "
            + "\n- ".join(critic_feedback.get("issues") or [])
            + f"\nHint: {critic_feedback.get('retry_hint') or ''}\n"
            + "Only use hotel_ids, trip_ids and carriers that appear in the data below. "
            "Never invent options. If transport data is empty, set recommendation to null."
        )
    user_msg = (
        "Today's scored data follows. Select top 2 accommodation and best transport.\n\n"
        f"OUTPUT SCHEMA (return JSON exactly like this shape):\n{json.dumps(OUTPUT_SCHEMA_HINT)}\n\n"
        f"run_date: {run_date}\n"
        f"group_size: {config['trip']['group_size']}\n\n"
        f"ACCOMMODATION:\n{json.dumps(accommodation, ensure_ascii=False)}\n\n"
        f"TRANSPORT:\n{json.dumps(transport, ensure_ascii=False)}\n\n"
        f"ALERTS:\n{json.dumps(alerts, ensure_ascii=False)}"
        f"{feedback_block}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=_system_prompt(config),
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(text)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    return json.loads(text[start : end + 1])


def _analyse_deterministic(
    run_date: str,
    accommodation: list[dict[str, Any]],
    transport: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    group = config["trip"]["group_size"]

    scored = [a for a in accommodation if a.get("composite_score") is not None]
    scored.sort(key=lambda a: a["composite_score"], reverse=True)
    top_picks = []
    for rank, a in enumerate(scored[:2], start=1):
        top_picks.append(
            {
                "rank": rank,
                "hotel_id": a["hotel_id"],
                "source": a["source"],
                "name": a["name"],
                "price_eur_per_night": a["price_eur"],
                "rating": a["rating"],
                "distance_km": a["distance_km"],
                "composite_score": a["composite_score"],
                "vs_yesterday_pct": a.get("vs_yesterday_pct"),
                "vs_7d_avg_pct": a.get("vs_7d_avg_pct"),
                "alert_triggered": a.get("alert_triggered", False),
                "booking_link": a["booking_link"],
                "coordinates": {"lat": a.get("lat"), "lng": a.get("lng")},
                "rooms": a.get("rooms"),
                "total_group_cost_eur": a.get("total_group_cost_eur"),
            }
        )

    options = []
    for t in transport:
        pp = t.get("price_eur_per_person")
        options.append(
            {
                **{k: t.get(k) for k in (
                    "trip_id", "type", "carrier", "price_eur_per_person",
                    "duration_min", "departure", "arrival", "stops", "booking_link",
                    "date", "date_offset_days",
                )},
                "total_group_cost_eur": round(pp * group, 2) if pp is not None else None,
            }
        )

    recommendation, reasoning = _recommend_transport(options)

    return {
        "run_date": run_date,
        "accommodation": {"top_picks": top_picks, "alerts": alerts},
        "transport": {
            "recommendation": recommendation,
            "reasoning": reasoning,
            "options": options,
        },
    }


def _recommend_transport(options: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Pick flight vs flixbus: favour the cheapest flight unless the time
    saved isn't worth the extra group cost vs the bus."""
    flights = [o for o in options if o["type"] == "flight" and o.get("price_eur_per_person")]
    buses = [o for o in options if o["type"] == "flixbus" and o.get("price_eur_per_person")]
    if not flights and not buses:
        return None, "No transport options available today."
    if flights and not buses:
        f = min(flights, key=lambda o: o["price_eur_per_person"])
        return "flight", f"Only flights available. Cheapest: {f['carrier']} €{f['price_eur_per_person']}/person."
    if buses and not flights:
        b = min(buses, key=lambda o: o["price_eur_per_person"])
        return "flixbus", f"Only FlixBus available at €{b['price_eur_per_person']}/person."

    f = min(flights, key=lambda o: o["price_eur_per_person"])
    b = min(buses, key=lambda o: o["price_eur_per_person"])
    extra_cost = (f["total_group_cost_eur"] or 0) - (b["total_group_cost_eur"] or 0)
    hours_saved = ((b["duration_min"] or 0) - (f["duration_min"] or 0)) / 60
    reasoning = (
        f"{f['carrier']} is €{f['price_eur_per_person']}/person ({f['duration_min']}m). "
        f"FlixBus is €{b['price_eur_per_person']}/person but {b['duration_min']}m. "
        f"The flight saves ~{hours_saved * 4:.0f}h of combined travel time for €{extra_cost:.0f} extra."
    )
    # Heuristic: take the flight when the per-person premium is modest.
    if (f["price_eur_per_person"] - b["price_eur_per_person"]) <= 40:
        return "flight", reasoning + " Recommend the flight."
    return "flixbus", reasoning + " The flight premium is steep — recommend FlixBus."
