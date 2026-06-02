"""Pydantic v2 models for the agent output schema.

Mirrors CLAUDE.md "Agent output schema (JSON)". Validating the analyser's
response through `AgentOutput` guarantees downstream code (Sheets, Gmail) sees
a consistent shape and that unavailable fields are explicit `None` rather than
missing keys.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

TransportType = Literal["flight", "flixbus"]
AccommodationSource = Literal["booking_com", "airbnb", "skyscanner"]


class Coordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: Optional[float] = None
    lng: Optional[float] = None


class TopPick(BaseModel):
    """One ranked accommodation pick."""

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    hotel_id: str
    source: AccommodationSource
    name: str
    price_eur_per_night: Optional[float] = Field(default=None, ge=0)
    rating: Optional[float] = Field(default=None, ge=0)
    distance_km: Optional[float] = Field(default=None, ge=0)
    composite_score: Optional[float] = Field(default=None, ge=0, le=100)
    vs_yesterday_pct: Optional[float] = None
    vs_7d_avg_pct: Optional[float] = None
    alert_triggered: bool = False
    booking_link: Optional[str] = None
    coordinates: Coordinates = Field(default_factory=Coordinates)
    rooms: Optional[int] = Field(default=None, ge=1)
    total_group_cost_eur: Optional[float] = Field(default=None, ge=0)
    price_basis: Optional[str] = None


class PriceAlert(BaseModel):
    """A triggered price-drop / budget-breach alert."""

    model_config = ConfigDict(extra="forbid")

    property: str
    hotel_id: str
    prev_price: Optional[float] = None
    new_price: float
    change_pct: Optional[float] = None
    link: Optional[str] = None


class AccommodationBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_picks: list[TopPick] = Field(default_factory=list, max_length=2)
    alerts: list[PriceAlert] = Field(default_factory=list)


class TransportOption(BaseModel):
    """A single flight or bus option for the outbound date."""

    model_config = ConfigDict(extra="forbid")

    trip_id: str
    type: TransportType
    carrier: str
    price_eur_per_person: Optional[float] = Field(default=None, ge=0)
    duration_min: Optional[int] = Field(default=None, ge=0)
    departure: Optional[str] = None
    arrival: Optional[str] = None
    stops: int = Field(default=0, ge=0)
    booking_link: Optional[str] = None
    total_group_cost_eur: Optional[float] = Field(default=None, ge=0)
    date: Optional[str] = None                   # ISO date this option actually covers
    date_offset_days: Optional[int] = 0          # 0 = configured date, ±1 = alternative


class TransportBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation: Optional[TransportType] = None
    reasoning: str = ""
    options: list[TransportOption] = Field(default_factory=list)


class AgentOutput(BaseModel):
    """Top-level structured output for one daily run."""

    model_config = ConfigDict(extra="forbid")

    run_date: date
    accommodation: AccommodationBlock
    transport: TransportBlock


if __name__ == "__main__":
    # Smoke-test against the example payload in CLAUDE.md.
    import json

    example = {
        "run_date": "2026-06-01",
        "accommodation": {
            "top_picks": [
                {
                    "rank": 1, "hotel_id": "bk_123456", "source": "booking_com",
                    "name": "Apartmán Centrum Warsaw", "price_eur_per_night": 72,
                    "rating": 9.1, "distance_km": 0.8, "composite_score": 81.4,
                    "vs_yesterday_pct": -11.2, "vs_7d_avg_pct": -8.7,
                    "alert_triggered": False,
                    "booking_link": "https://www.booking.com/hotel/pl/apartman-centrum.html",
                    "coordinates": {"lat": 52.229, "lng": 21.012},
                }
            ],
            "alerts": [],
        },
        "transport": {
            "recommendation": "flight",
            "reasoning": "Flight saves time.",
            "options": [],
        },
    }
    parsed = AgentOutput.model_validate(example)
    print(json.dumps(parsed.model_dump(mode="json"), indent=2, ensure_ascii=False))
    print("OK — schema validates the CLAUDE.md example.")
