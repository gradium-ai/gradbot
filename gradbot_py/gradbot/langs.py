"""Language code mappings."""

from ._gradbot import Lang

_ALL = [Lang.En, Lang.Fr, Lang.De, Lang.Es, Lang.Pt]

# "en" -> Lang.En, etc.
LANGUAGES = {lang.code(): lang for lang in _ALL}

LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
}

COUNTRY_NAMES = {
    "us": "United States",
    "gb": "United Kingdom",
    "ie": "Ireland",
    "fr": "France",
    "ca": "Canada",
    "de": "Germany",
    "at": "Austria",
    "mx": "Mexico",
    "es": "Spain",
    "br": "Brazil",
    "pt": "Portugal",
}

GENDER_NAMES = {
    "male": "Masculine",
    "female": "Feminine",
}
