"""SimControl — single control plane for the entire sim fleet.

VirtualObject with a single conventional key (`"global"`). Holds the
configured drivers/riders count so fan-out operations (pause everyone,
change rider rate) don't need the caller to remember sizes. Calls into
RiderSim / DriverSim / MappingSim per-key VOs.

Same primitives, same ingress — operators (TUI, scripts, curl) drive
the sim fleet exactly the way they drive the app.
"""

import restate

from rideco.shared.log import log, log_in, log_out
from rideco.shared.regions import all_regions
from rideco.services import sim_driver as driver_svc
from rideco.services import sim_mapping as mapping_svc
from rideco.services import sim_rider as rider_svc


sim_control = restate.VirtualObject("SimControl")


@sim_control.handler("start_all")
async def start_all(ctx: restate.ObjectContext, payload: dict | None = None) -> dict:
    """Boot the whole sim fleet.

    Payload (all optional, sensible defaults):
      drivers: int = 16              # total, round-robin across regions
      riders:  int = 3               # total, round-robin across regions
      rider_rate: float = 0.1        # per-rider trips/sec (Poisson-ish)
      mapping_interval_s: float = 12 # seconds between mapping feed emits
    """
    p = payload or {}
    drivers = int(p.get("drivers", 16))
    riders = int(p.get("riders", 3))
    rider_rate = float(p.get("rider_rate", 0.1))
    mapping_interval_s = float(p.get("mapping_interval_s", 12.0))
    log_in("start_all", drivers=drivers, riders=riders,
           rider_rate=rider_rate, mapping_interval_s=mapping_interval_s)

    ctx.set("drivers", drivers)
    ctx.set("riders", riders)
    ctx.set("rider_rate", rider_rate)
    ctx.set("mapping_interval_s", mapping_interval_s)

    regions = all_regions()

    for region in regions:
        ctx.object_send(mapping_svc.start, key=region,
                        arg={"interval_s": mapping_interval_s})
    for i in range(drivers):
        ctx.object_send(driver_svc.start, key=f"driver-{i:03d}",
                        arg={"region": regions[i % len(regions)]})
    for i in range(riders):
        ctx.object_send(rider_svc.start, key=f"rider-{i:03d}",
                        arg={"rate": rider_rate})
    # Summary instead of one log per send: 12 hypercorns each tail this
    # log, and 23 individual send lines per start_all would just be noise.
    log_out("send", f"MappingSim.start × {len(regions)}")
    log_out("send", f"DriverSim.start × {drivers}")
    log_out("send", f"RiderSim.start × {riders}")

    return {
        "drivers": drivers, "riders": riders,
        "rider_rate": rider_rate, "mapping_interval_s": mapping_interval_s,
        "regions": regions,
    }


@sim_control.handler("stop_all")
async def stop_all(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Pause every rider, driver, and mapping feed."""
    drivers = (await ctx.get("drivers", type_hint=int)) or 0
    riders = (await ctx.get("riders", type_hint=int)) or 0
    log_in("stop_all", drivers=drivers, riders=riders)
    for i in range(drivers):
        ctx.object_send(driver_svc.pause, key=f"driver-{i:03d}", arg={})
    for i in range(riders):
        ctx.object_send(rider_svc.pause, key=f"rider-{i:03d}", arg={})
    for region in all_regions():
        ctx.object_send(mapping_svc.pause, key=region, arg={})
    log_out("send", f"DriverSim.pause × {drivers}")
    log_out("send", f"RiderSim.pause × {riders}")
    log_out("send", f"MappingSim.pause × {len(all_regions())}")
    return {"stopped": True, "drivers": drivers, "riders": riders}


@sim_control.handler("pause_riders")
async def pause_riders(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    riders = (await ctx.get("riders", type_hint=int)) or 0
    log_in("pause_riders", riders=riders)
    for i in range(riders):
        ctx.object_send(rider_svc.pause, key=f"rider-{i:03d}", arg={})
    log_out("send", f"RiderSim.pause × {riders}")
    return {"paused_riders": riders}


@sim_control.handler("resume_riders")
async def resume_riders(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    riders = (await ctx.get("riders", type_hint=int)) or 0
    log_in("resume_riders", riders=riders)
    for i in range(riders):
        ctx.object_send(rider_svc.resume, key=f"rider-{i:03d}", arg={})
    log_out("send", f"RiderSim.resume × {riders}")
    return {"resumed_riders": riders}


@sim_control.handler("set_rider_rate")
async def set_rider_rate(ctx: restate.ObjectContext, payload: dict) -> dict:
    rate = float(payload["rate"])
    riders = (await ctx.get("riders", type_hint=int)) or 0
    log_in("set_rider_rate", rate=rate, riders=riders)
    ctx.set("rider_rate", rate)
    for i in range(riders):
        ctx.object_send(rider_svc.set_rate, key=f"rider-{i:03d}", arg={"rate": rate})
    log_out("send", f"RiderSim.set_rate × {riders}", rate=rate)
    return {"rate": rate, "riders": riders}


@sim_control.handler("pause_drivers")
async def pause_drivers(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    drivers = (await ctx.get("drivers", type_hint=int)) or 0
    log_in("pause_drivers", drivers=drivers)
    for i in range(drivers):
        ctx.object_send(driver_svc.pause, key=f"driver-{i:03d}", arg={})
    log_out("send", f"DriverSim.pause × {drivers}")
    return {"paused_drivers": drivers}


@sim_control.handler("resume_drivers")
async def resume_drivers(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    drivers = (await ctx.get("drivers", type_hint=int)) or 0
    log_in("resume_drivers", drivers=drivers)
    for i in range(drivers):
        ctx.object_send(driver_svc.resume, key=f"driver-{i:03d}", arg={})
    log_out("send", f"DriverSim.resume × {drivers}")
    return {"resumed_drivers": drivers}


@sim_control.handler("pause_mapping")
async def pause_mapping(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    log_in("pause_mapping")
    for region in all_regions():
        ctx.object_send(mapping_svc.pause, key=region, arg={})
    log_out("send", f"MappingSim.pause × {len(all_regions())}")
    return {"paused_mapping": True}


@sim_control.handler("resume_mapping")
async def resume_mapping(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    log_in("resume_mapping")
    for region in all_regions():
        ctx.object_send(mapping_svc.resume, key=region, arg={})
    log_out("send", f"MappingSim.resume × {len(all_regions())}")
    return {"resumed_mapping": True}


@sim_control.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "drivers": (await ctx.get("drivers", type_hint=int)) or 0,
        "riders": (await ctx.get("riders", type_hint=int)) or 0,
        "rider_rate": await ctx.get("rider_rate", type_hint=float),
        "mapping_interval_s": await ctx.get("mapping_interval_s", type_hint=float),
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[sim_control])
