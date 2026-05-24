"""ASGI entrypoint for all RideCo services on one Restate endpoint.

Run with:
    python -m hypercorn --config hypercorn-config.toml rideco.services.app:app

Then register the deployment (restate-server is in Docker, listens at :9070,
talks back to the host on host.docker.internal:9080):
    restate deployments register http://host.docker.internal:9080 --force
"""

import restate

from rideco.services.dispatch import dispatch
from rideco.services.eta import eta
from rideco.services.features import features
from rideco.services.locations import locations
from rideco.services.offers import offers
from rideco.services.pricing import pricing
from rideco.services.region_safety_agent import region_safety_agent
from rideco.services.sim_control import sim_control
from rideco.services.sim_driver import driver_sim
from rideco.services.sim_mapping import mapping_sim
from rideco.services.sim_rider import rider_sim
from rideco.services.trip import trip


app = restate.app(services=[
    # App
    trip,
    offers,
    dispatch,
    locations,
    pricing,
    eta,
    features,
    region_safety_agent,
    # Sims (load generators, also implemented on Restate)
    rider_sim,
    driver_sim,
    mapping_sim,
    sim_control,
])
