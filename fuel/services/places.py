"""US place-name normalisation and the local gazetteer.

The fuel price file has no coordinates, and its "Address" column holds
highway/exit descriptions ("I-44, EXIT 283 & US-69") that cannot be geocoded.
Stations are therefore located by (city, state). The same normalisation is
used offline (``build_geocodes``) and at request time to resolve user input
such as "Chicago, IL" without calling an external geocoding API.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {name.upper(): code for code, name in US_STATES.items()}

_WORD_SUBSTITUTIONS = [
    (re.compile(r"\bSAINTE\b"), "STE"),
    (re.compile(r"\bSAINT\b"), "ST"),
    (re.compile(r"\bMOUNT\b"), "MT"),
    (re.compile(r"\bFORT\b"), "FT"),
]
_DIRECTION_ABBREVIATIONS = {"N": "North", "S": "South", "E": "East", "W": "West"}


def name_key(name: str) -> str:
    """Normalise a place name for matching.

    Accents and punctuation are dropped, common words are abbreviated and all
    whitespace is removed, so "Mc Calla" == "McCalla", "Saint Johns" ==
    "St. Johns" and "Cañon City" == "Canon City".
    """
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.upper().replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    for pattern, replacement in _WORD_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", "", text)


def name_variants(city: str):
    """Yield the name itself, then progressively looser alternatives."""
    city = " ".join(city.split())
    yield city
    first, _, rest = city.partition(" ")
    if first.upper() in _DIRECTION_ABBREVIATIONS and rest:
        yield f"{_DIRECTION_ABBREVIATIONS[first.upper()]} {rest}"  # "S Coffeyville"
    for pattern in (r"\s+National Park$", r"\s+City$", r"\s+(Township|Twp)$", r"\s+Center$"):
        variant = re.sub(pattern, "", city, flags=re.IGNORECASE)
        if variant != city:
            yield variant


def normalize_state(value: str) -> str | None:
    value = " ".join(value.split()).upper().replace(".", "")
    if value in US_STATES:
        return value
    return _STATE_BY_NAME.get(value)


# ---------------------------------------------------------------------------
# Runtime gazetteer (data/us_places.csv, produced by `build_geocodes`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


def gazetteer_path() -> Path:
    return Path(settings.BASE_DIR) / "data" / "us_places.csv"


@lru_cache(maxsize=1)
def load_gazetteer() -> dict[tuple[str, str], Place]:
    index: dict[tuple[str, str], Place] = {}
    with gazetteer_path().open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            place = Place(row["name"], row["state"], float(row["latitude"]), float(row["longitude"]))
            # The file is written in priority order, so the first entry wins.
            index.setdefault((place.state, name_key(place.name)), place)
    return index


def lookup_place(city: str, state: str) -> Place | None:
    state_code = normalize_state(state)
    if not state_code:
        return None
    index = load_gazetteer()
    for variant in name_variants(city):
        place = index.get((state_code, name_key(variant)))
        if place:
            return place
    return None
