"""Country / city catalog for the "Change Metadata" wizard.

Picking a real place beats asking for raw coordinates: the timezone, altitude
and GPS accuracy all follow from the city, so the written metadata stays
internally consistent instead of looking synthesised.

This is the single place where places are defined - keyboards and handlers
build themselves from ``COUNTRIES``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    key: str
    label: str
    latitude: float
    longitude: float
    # Standard UTC offset in hours. Written into EXIF OffsetTime and used to
    # place the capture time in local wall-clock terms.
    utc_offset: int
    # Rough elevation in metres, written as GPSAltitude.
    altitude: float

    def jittered(self, rng: random.Random | None = None) -> tuple[float, float]:
        """Coordinates nudged a few hundred metres, as a real shot would be.

        Every photo landing on the exact same city-centre point is the giveaway
        that the location was generated rather than recorded.
        """
        rng = rng or random.Random()
        return (
            round(self.latitude + rng.uniform(-0.02, 0.02), 6),
            round(self.longitude + rng.uniform(-0.02, 0.02), 6),
        )


@dataclass(frozen=True)
class Country:
    key: str
    label: str
    cities: tuple[City, ...]


COUNTRIES: tuple[Country, ...] = (
    Country(
        key="usa",
        label="🇺🇸 USA",
        cities=(
            City("new_york", "New York", 40.712776, -74.005974, -5, 10.0),
            City("los_angeles", "Los Angeles", 34.052235, -118.243683, -8, 87.0),
            City("miami", "Miami", 25.761681, -80.191788, -5, 2.0),
            City("las_vegas", "Las Vegas", 36.169941, -115.139832, -8, 610.0),
            City("chicago", "Chicago", 41.878113, -87.629799, -6, 181.0),
        ),
    ),
    Country(
        key="spain",
        label="🇪🇸 Spain",
        cities=(
            City("barcelona", "Barcelona", 41.385063, 2.173404, 1, 12.0),
            City("madrid", "Madrid", 40.416775, -3.703790, 1, 667.0),
            City("valencia", "Valencia", 39.469907, -0.376288, 1, 15.0),
            City("seville", "Seville", 37.389092, -5.984459, 1, 11.0),
            City("ibiza", "Ibiza", 38.906662, 1.421420, 1, 5.0),
        ),
    ),
    Country(
        key="italy",
        label="🇮🇹 Italy",
        cities=(
            City("rome", "Rome", 41.902782, 12.496366, 1, 21.0),
            City("milan", "Milan", 45.464203, 9.189982, 1, 120.0),
            City("venice", "Venice", 45.440847, 12.315515, 1, 2.0),
            City("florence", "Florence", 43.769562, 11.255814, 1, 50.0),
            City("naples", "Naples", 40.851775, 14.268124, 1, 17.0),
        ),
    ),
    Country(
        key="england",
        label="🏴󠁧󠁢󠁥󠁮󠁧󠁿 England",
        cities=(
            City("london", "London", 51.507351, -0.127758, 0, 11.0),
            City("manchester", "Manchester", 53.483959, -2.244644, 0, 38.0),
            City("liverpool", "Liverpool", 53.408371, -2.991573, 0, 70.0),
            City("birmingham", "Birmingham", 52.486243, -1.890401, 0, 140.0),
            City("brighton", "Brighton", 50.822530, -0.137163, 0, 8.0),
        ),
    ),
    Country(
        key="japan",
        label="🇯🇵 Japan",
        cities=(
            City("tokyo", "Tokyo", 35.689487, 139.691711, 9, 40.0),
            City("kyoto", "Kyoto", 35.011635, 135.768036, 9, 56.0),
            City("osaka", "Osaka", 34.693737, 135.502167, 9, 24.0),
            City("sapporo", "Sapporo", 43.061936, 141.354292, 9, 26.0),
            City("hiroshima", "Hiroshima", 34.385204, 132.455292, 9, 8.0),
        ),
    ),
)


def get_country(key: str) -> Country | None:
    for country in COUNTRIES:
        if country.key == key:
            return country
    return None


def get_city(key: str) -> tuple[Country, City] | None:
    for country in COUNTRIES:
        for city in country.cities:
            if city.key == key:
                return country, city
    return None


def validate_catalog(countries: tuple[Country, ...] = COUNTRIES) -> None:
    """Fail fast on a malformed catalog (called at import time and in tests)."""
    if not countries:
        raise ValueError("at least one country must be configured")

    seen_countries: set[str] = set()
    seen_cities: set[str] = set()
    for country in countries:
        if not country.key or country.key in seen_countries:
            raise ValueError(f"invalid or duplicate country key: {country.key!r}")
        seen_countries.add(country.key)
        if not country.cities:
            raise ValueError(f"{country.key}: at least one city is required")
        for city in country.cities:
            # City keys travel in callback data (64 byte budget).
            if not city.key or len(city.key) > 32:
                raise ValueError(f"invalid city key: {city.key!r}")
            if city.key in seen_cities:
                raise ValueError(f"duplicate city key: {city.key!r}")
            seen_cities.add(city.key)
            if not -90 <= city.latitude <= 90 or not -180 <= city.longitude <= 180:
                raise ValueError(f"{city.key}: coordinates out of range")
            if not -12 <= city.utc_offset <= 14:
                raise ValueError(f"{city.key}: implausible UTC offset")


validate_catalog()
