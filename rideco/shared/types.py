"""Wire-level types shared across RideCo services.

Inputs and outputs are plain JSON-friendly dicts on the wire so that the Restate
runtime's default serde handles them without ceremony. Dataclasses are used
inside handlers for clarity.
"""

from dataclasses import dataclass


TripStatus = str
TRIP_REQUESTED = "requested"
TRIP_QUOTED = "quoted"
TRIP_DISPATCHING = "dispatching"
TRIP_ASSIGNED = "assigned"
TRIP_IN_PROGRESS = "in_progress"
TRIP_COMPLETED = "completed"
TRIP_CANCELLED = "cancelled"


DriverStatus = str
DRIVER_OFFLINE = "offline"
DRIVER_IDLE = "idle"
DRIVER_EN_ROUTE = "en_route"
DRIVER_ON_TRIP = "on_trip"


EntityType = str
ENTITY_DRIVER = "driver"
ENTITY_RIDER = "rider"
ENTITY_REGION = "region"


@dataclass
class LatLng:
    lat: float
    lng: float

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng}

    @classmethod
    def from_dict(cls, d: dict) -> "LatLng":
        return cls(lat=float(d["lat"]), lng=float(d["lng"]))


def feature_key(entity_type: str, entity_id: str, feature_name: str) -> str:
    return f"{entity_type}:{entity_id}:{feature_name}"
