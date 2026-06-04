"""Supported rental markets for the apartment search agent."""

from __future__ import annotations

from typing import Any


DEFAULT_CITY = "paris"

CITY_LABELS = {
    "paris": "Paris",
    "berlin": "Berlin",
}

CITY_COUNTRIES = {
    "paris": "France",
    "berlin": "Germany",
}

CITY_BIASES = {
    "paris": "Paris, France",
    "berlin": "Berlin, Germany",
}


def normalize_city(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in CITY_LABELS:
        return text
    return DEFAULT_CITY


def city_label(value: Any) -> str:
    return CITY_LABELS[normalize_city(value)]


def city_bias(value: Any) -> str:
    return CITY_BIASES[normalize_city(value)]
