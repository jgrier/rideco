"""Region definitions. Regions are the geographic unit for dispatch and pricing.

A real ride-hailing platform would slice into finer-grained subnetworks (geohashes,
hex bins, etc.) per the PTv3 dynamic pricing optimization. For the demo, four
city-sized regions are enough to make batched matching visible.
"""

REGIONS: dict[str, dict] = {
    "SF":  {"name": "San Francisco", "center": {"lat": 37.7749, "lng": -122.4194}, "base_fare_cents": 350},
    "NYC": {"name": "New York City", "center": {"lat": 40.7128, "lng": -74.0060},  "base_fare_cents": 425},
    "LA":  {"name": "Los Angeles",   "center": {"lat": 34.0522, "lng": -118.2437}, "base_fare_cents": 375},
    "SEA": {"name": "Seattle",       "center": {"lat": 47.6062, "lng": -122.3321}, "base_fare_cents": 325},
}


def all_regions() -> list[str]:
    return list(REGIONS.keys())


def region_base_fare_cents(region: str) -> int:
    return REGIONS.get(region, {}).get("base_fare_cents", 350)
