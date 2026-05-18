"""SQLAlchemy ORM models for the Paris rental agent."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_room_requirements() -> dict:
    return {
        "living_room": {"must_have": [], "nice_to_have": []},
        "bedroom": {"must_have": [], "nice_to_have": []},
        "kitchen": {"must_have": [], "nice_to_have": []},
    }


def _default_nearby_requirements() -> dict:
    return {
        "supermarket_m": 500,
        "metro_m": 700,
        "hospital_m": 2000,
        "park_m": 1000,
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    renter_profile: Mapped[Optional["RenterProfile"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    search_profiles: Mapped[list["SearchProfile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RenterProfile(Base):
    __tablename__ = "renter_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(8), default="en")
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dossierfacile_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    work_location_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    work_location_address: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    work_lat: Mapped[Optional[float]] = mapped_column(nullable=True)
    work_lon: Mapped[Optional[float]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    user: Mapped["User"] = relationship(back_populates="renter_profile")


class SearchProfile(Base):
    __tablename__ = "search_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), default="Main Paris search")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmation_status: Mapped[str] = mapped_column(String(32), default="draft")
    last_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    max_rent_including_charges_eur: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    min_bedrooms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    min_rooms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    min_surface_m2: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    furnished_preference: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    commute_max_minutes: Mapped[int] = mapped_column(Integer, default=30)
    commute_modes: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["metro", "bike"]
    )
    commute_logic: Mapped[str] = mapped_column(String(32), default="metro_or_bike")
    preferred_arrondissements: Mapped[list[int]] = mapped_column(JSON, default=list)
    excluded_arrondissements: Mapped[list[int]] = mapped_column(JSON, default=list)
    room_requirements: Mapped[dict] = mapped_column(JSON, default=_default_room_requirements)
    nearby_requirements: Mapped[dict] = mapped_column(JSON, default=_default_nearby_requirements)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    user: Mapped["User"] = relationship(back_populates="search_profiles")


class ProfileIntakeSession(Base):
    __tablename__ = "profile_intake_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    search_profile_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("search_profiles.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="collecting")
    channel: Mapped[str] = mapped_column(String(32), default="voice_text")
    raw_transcript: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latest_user_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_profile_patch: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    ambiguous_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    field_confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    field_sources: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed_profile_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    canonical_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rent_eur: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    charges_eur: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_monthly_eur: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    surface_m2: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rooms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bedrooms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    furnished: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    address_text: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    arrondissement: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    available_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    features: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    search_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("search_profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="running")
    query_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ListingMatch(Base):
    __tablename__ = "listing_matches"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "listing_id", "search_run_id", name="uq_match_user_listing_run"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    search_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("search_profiles.id", ondelete="CASCADE"), nullable=False
    )
    search_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("search_runs.id", ondelete="SET NULL"), nullable=True
    )
    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    overall_score: Mapped[int] = mapped_column(Integer, default=0)
    passes_hard_filters: Mapped[bool] = mapped_column(Boolean, default=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    commute: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {"metro_min": None, "bike_min": None, "status": "unknown"},
    )
    amenity_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SavedListing(Base):
    __tablename__ = "saved_listings"
    __table_args__ = (
        UniqueConstraint("user_id", "listing_id", name="uq_saved_user_listing"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="saved")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), default="chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="user")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ViewingRequestDraft(Base):
    __tablename__ = "viewing_request_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(String(4), default="en")
    subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AlertPreference(Base):
    __tablename__ = "alert_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    search_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("search_profiles.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    min_score: Mapped[int] = mapped_column(Integer, default=70)
    frequency: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
