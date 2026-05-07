# Configurable compose runner. Defaults to `podman compose` because the
# project is developed on a Podman host. Override on Docker hosts:
#   make up COMPOSE='docker compose'
COMPOSE ?= podman compose

.PHONY: help up down test experiment experiment-quick experiment-full report

help:
	@echo "Targets:"
	@echo "  up                 Bring up Postgres + Redis + framework + locust (podman compose up -d --build)"
	@echo "  down               Tear down all services and volumes"
	@echo "  test               Run pytest (poetry run pytest -q tests/)"
	@echo "  experiment         (Phase 3) run a single experiment cell"
	@echo "  experiment-quick   (Phase 4) run the 18-cell matrix in quick mode (~5-7 min)"
	@echo "  experiment-full    (Phase 4) run the 18-cell matrix in full mode (<60 min)"
	@echo "  report             (Phase 4) aggregate matrix JSONs into PNGs"

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down -v

test:
	poetry run pytest -q tests/

experiment:
	@echo "TODO: phase 3"

experiment-quick:
	@echo "TODO: phase 3"

experiment-full:
	@echo "TODO: phase 3"

report:
	@echo "TODO: phase 4"
