"""Pydantic schemas for API I/O."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ─────────── Auth ───────────
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    full_name: Optional[str] = None
    created_at: datetime
    last_login_at: Optional[datetime] = None


# ─────────── Profiles ───────────
class RenterProfileIn(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    preferred_language: Optional[str] = Field(default=None, max_length=8)
    phone: Optional[str] = Field(default=None, max_length=64)
    dossierfacile_url: Optional[str] = Field(default=None, max_length=512)
    work_location_label: Optional[str] = Field(default=None, max_length=255)
    work_location_address: Optional[str] = Field(default=None, max_length=512)
    work_lat: Optional[float] = None
    work_lon: Optional[float] = None


class RenterProfileOut(RenterProfileIn):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    created_at: datetime
    updated_at: datetime


class SearchProfileBase(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    is_active: Optional[bool] = None
    max_rent_including_charges_eur: Optional[int] = Field(default=None, ge=1, le=50000)
    min_bedrooms: Optional[int] = Field(default=None, ge=0, le=10)
    min_rooms: Optional[int] = Field(default=None, ge=1, le=20)
    min_surface_m2: Optional[int] = Field(default=None, ge=1, le=500)
    furnished_preference: Optional[Literal["required", "prefer", "any"]] = None
    commute_max_minutes: Optional[int] = Field(default=None, ge=1, le=180)
    commute_modes: Optional[list[str]] = Field(default=None, max_length=4)
    commute_logic: Optional[str] = Field(default=None, max_length=32)
    preferred_arrondissements: Optional[list[int]] = Field(default=None, max_length=20)
    excluded_arrondissements: Optional[list[int]] = Field(default=None, max_length=20)
    room_requirements: Optional[dict[str, Any]] = None
    nearby_requirements: Optional[dict[str, Any]] = None

    @field_validator("preferred_arrondissements", "excluded_arrondissements", mode="before")
    @classmethod
    def coerce_arrondissements(cls, value: Any) -> list[int] | None:
        if value is None:
            return None

        items = value if isinstance(value, (list, tuple, set)) else [value]
        out: list[int] = []

        def add(candidate: int) -> None:
            if 1 <= candidate <= 20 and candidate not in out:
                out.append(candidate)

        for item in items:
            if isinstance(item, int):
                add(item - 75000 if 75001 <= item <= 75020 else item)
                continue

            text = str(item or "").lower()
            if not text:
                continue
            for postal in re.findall(r"\b750(0[1-9]|1[0-9]|20)\b", text):
                add(int(postal))
            for ordinal in re.findall(r"\b([1-9]|1[0-9]|20)(?:st|nd|rd|th|e|er|eme|ème)?\b", text):
                add(int(ordinal))

        return out

    @field_validator("nearby_requirements", mode="before")
    @classmethod
    def coerce_nearby_requirements(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, list):
            return {}

        defaults = {
            "supermarket": ("supermarket_m", 500),
            "grocery": ("supermarket_m", 500),
            "metro": ("metro_m", 700),
            "station": ("metro_m", 700),
            "park": ("park_m", 1000),
            "hospital": ("hospital_m", 2000),
            "gym": ("gym_m", 1500),
            "school": ("school_m", 1500),
            "pharmacy": ("pharmacy_m", 700),
            "bakery": ("bakery_m", 500),
        }
        out: dict[str, Any] = {}
        for item in value:
            mapped = defaults.get(str(item).lower().strip())
            if mapped:
                out[mapped[0]] = mapped[1]
        return out


class SearchProfileOut(SearchProfileBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    confirmation_status: str
    last_confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ─────────── Intake ───────────
class TranscriptIn(BaseModel):
    transcript: str = Field(min_length=1, max_length=8000)


class TextUpdateIn(BaseModel):
    patch: dict[str, Any]


class IntakeStateOut(BaseModel):
    intake_session_id: str
    status: str
    raw_transcript: Optional[str] = None
    latest_user_text: Optional[str] = None
    draft_profile: Optional[dict[str, Any]] = None
    missing_fields: list[str] = Field(default_factory=list)
    ambiguous_fields: list[str] = Field(default_factory=list)
    field_confidence: dict[str, Any] = Field(default_factory=dict)
    field_sources: dict[str, Any] = Field(default_factory=dict)
    confirmation_status: str = "draft"
    summary: Optional[str] = None


# ─────────── Listings & matches ───────────
class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    canonical_url: Optional[str] = None
    source: Optional[str] = None
    title: str
    description: Optional[str] = None
    rent_eur: Optional[int] = None
    charges_eur: Optional[int] = None
    total_monthly_eur: Optional[int] = None
    surface_m2: Optional[int] = None
    rooms: Optional[int] = None
    bedrooms: Optional[int] = None
    furnished: Optional[bool] = None
    address_text: Optional[str] = None
    arrondissement: Optional[int] = None
    features: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    is_mock: bool = False


class MatchOut(BaseModel):
    id: str
    listing: ListingOut
    overall_score: int
    passes_hard_filters: bool
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    commute: dict[str, Any] = Field(default_factory=dict)


class SavedListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    listing_id: str
    status: str
    notes: Optional[str] = None
    created_at: datetime
    listing: Optional[ListingOut] = None


class SavedListingPatch(BaseModel):
    status: Optional[Literal["saved", "rejected"]] = None
    notes: Optional[str] = Field(default=None, max_length=4000)


# ─────────── Search ───────────
class SearchRunIn(BaseModel):
    max_results: int = Field(default=20, ge=1, le=50)
    refresh: bool = True
    allow_unconfirmed_profile: bool = False


class SearchRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: str
    result_count: int
    error_message: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None


# ─────────── Assistant / drafts ───────────
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_session_id: Optional[str] = None


class ChatOut(BaseModel):
    conversation_session_id: str
    reply: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class DraftRequestIn(BaseModel):
    language: Literal["en", "fr"] = "en"


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    listing_id: str
    language: str
    subject: Optional[str] = None
    body: str
    status: str
    created_at: datetime


# ─────────── Errors ───────────
class ErrorOut(BaseModel):
    error: str
    message: str
    details: Optional[dict[str, Any]] = None
