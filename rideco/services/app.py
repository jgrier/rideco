"""ASGI entrypoint for all eight RideCo services on one Restate endpoint.

In a real deployment each service would likely run as its own process behind
a service mesh. They live in one process here so the dev loop is fast and the
demo's "look how compact this is" point lands. Restate doesn't care — each
service is registered independently with the runtime regardless of how
they're packaged.

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
from rideco.services.safety_agent import safety_agent
from rideco.services.trip import trip


app = restate.app(services=[
    trip,
    offers,
    dispatch,
    locations,
    pricing,
    eta,
    features,
    safety_agent,
])
