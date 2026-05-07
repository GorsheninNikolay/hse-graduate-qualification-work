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
	@if [ -z "$(INV)" ] || [ -z "$(BACKEND)" ] || [ -z "$(SCENARIO)" ]; then \
	  echo "Usage: make experiment INV={no_cache|ttl|operation|tag} BACKEND={redis|memory|none} SCENARIO={read_heavy|mixed|mutation_burst}"; \
	  exit 2; \
	fi
	@mkdir -p reports
	@if [ "$(INV)" = "no_cache" ]; then \
	  YAML="/app/examples/graphql-api-no-cache.yaml"; \
	else \
	  YAML="/app/examples/graphql-api-$(INV)-$(BACKEND).yaml"; \
	fi; \
	echo "Using YAML: $$YAML"; \
	echo "DSL_PATH=$$YAML" > .env; \
	$(COMPOSE) stop framework; \
	$(COMPOSE) rm -f framework 2>/dev/null || true; \
	$(COMPOSE) up -d --force-recreate framework; \
	$(COMPOSE) exec -T redis redis-cli FLUSHDB || true; \
	echo "Waiting for framework boot..."; \
	sleep 12; \
	poetry run python -m loadtest.runner \
	  --strategy=$(INV) --backend=$(BACKEND) --scenario=$(SCENARIO); \
	rm -f .env

experiment-quick:
	@MEASURED_DURATION_S=10 bash scripts/run_matrix.sh quick

experiment-full:
	@bash scripts/run_matrix.sh full

# `report` aggregates the most recent matrix run by default.
# Override the matrix list via `make report MATRIX_LIST=reports/.matrix-XXX.list`.
MATRIX_LIST ?= $(shell ls -t reports/.matrix-*.list 2>/dev/null | head -1)

report:
	@if [ -z "$(MATRIX_LIST)" ]; then \
	  echo "no matrix list found in reports/.matrix-*.list. run 'make experiment-quick' or 'make experiment-full' first."; \
	  exit 2; \
	fi
	@echo "aggregating $(MATRIX_LIST)"
	poetry run python -m scripts.build_report --matrix-list "$(MATRIX_LIST)"
