"""Pydantic v2 models for Kiwi.com Flights API responses."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class KiwiLocation(BaseModel):
    model_config = ConfigDict(extra="allow")

    lat: Optional[float] = None
    lng: Optional[float] = None


class KiwiCountry(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    code: Optional[str] = None


class KiwiCity(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None


class KiwiPlace(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    name: str
    code: Optional[str] = None
    slug: str
    country: Optional[KiwiCountry] = None
    city: Optional[KiwiCity] = None
    location: Optional[KiwiLocation] = None


class KiwiAutocompleteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int = 0
    places: list[KiwiPlace] = Field(default_factory=list)


class KiwiPriceMapDestination(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    country: Optional[str] = None
    location: Optional[KiwiLocation] = None


class KiwiPriceMapEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    destination: KiwiPriceMapDestination
    price: float = Field(ge=0)
    currency: Optional[str] = None


class KiwiPriceMapResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    currency: Optional[str] = None
    count: int = 0
    entries: list[KiwiPriceMapEntry] = Field(default_factory=list)
