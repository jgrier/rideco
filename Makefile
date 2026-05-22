.PHONY: install restate-up restate-down restate-logs serve register \
        sim-riders sim-drivers sim-mapping poison fix-poison clean

PYTHON ?= .venv/bin/python
INGRESS ?= http://localhost:8080
ADMIN ?= http://localhost:9070
DEPLOYMENT ?= http://host.docker.internal:9080

install:
	$(PYTHON) -m pip install -e .

restate-up:
	docker compose up -d
	@echo "Restate up. Web UI: $(ADMIN)  Ingress: $(INGRESS)"

restate-down:
	docker compose down

restate-logs:
	docker compose logs -f restate

serve:
	$(PYTHON) -m hypercorn --config hypercorn-config.toml rideco.services.app:app

# Stop hypercorn reliably (pkill sometimes fails on macOS for forked children).
stop:
	@PIDS=$$(lsof -nP -iTCP:9080 -sTCP:LISTEN -t); \
	if [ -z "$$PIDS" ]; then echo "9080 already free"; else echo "killing $$PIDS"; kill -9 $$PIDS; fi

register:
	restate -y deployments register --force $(DEPLOYMENT)

register-kafka:
	# Create the only Kafka subscription in RideCo: mapping_events topic →
	# Features.set. Restate routes each Kafka record's KEY into the Features
	# VO key, and the JSON value into Features.set's payload.
	curl -s -X POST http://localhost:9070/subscriptions \
	  -H 'Content-Type: application/json' \
	  --data '{"source":"kafka://rideco/mapping_events","sink":"service://Features/set","options":{"auto.offset.reset":"earliest"}}' \
	  | python3 -m json.tool

sim-riders:
	$(PYTHON) -m rideco.sim.rider

sim-drivers:
	$(PYTHON) -m rideco.sim.driver

sim-mapping:
	$(PYTHON) -m rideco.sim.mapping_events

# Stage choreography for the poison-pill demo. Publishes weather="BAD" to
# the mapping_events Kafka topic for SF.
poison:
	$(PYTHON) -m rideco.sim.mapping_events --poison SF --interval 30

fix-poison:
	@echo "Edit rideco/services/eta.py: set HANDLE_BAD_WEATHER_GRACEFULLY = True"
	@echo "Then re-run: make register"
	@echo "Stuck ETA invocations will drain on the next retry."

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf *.egg-info
