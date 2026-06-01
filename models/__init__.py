"""Pydantic models for the AI analyser agent's JSON output.

These mirror the "Agent output schema (JSON)" in CLAUDE.md and are used to
validate/normalise the analyser response before it is written to Sheets or the
Gmail digest. Import the top-level `AgentOutput` to validate a full run.
"""

from models.schemas import (
    AccommodationBlock,
    AgentOutput,
    Coordinates,
    PriceAlert,
    TopPick,
    TransportBlock,
    TransportOption,
)

__all__ = [
    "AgentOutput",
    "AccommodationBlock",
    "TransportBlock",
    "TopPick",
    "TransportOption",
    "PriceAlert",
    "Coordinates",
]
