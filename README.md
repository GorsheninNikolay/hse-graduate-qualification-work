# Declarative GraphQL Cache Framework

MVP for a master's thesis (ВКР) on declarative cache-invalidation strategies for auto-generated GraphQL APIs. The framework compares three invalidation strategies (`ttl`, `operation`, `tag`) across two cache backends (`redis`, `in_memory`) under matched load. The eventual experiment output is an 18-cell matrix (3 strategies x 2 backends x 3 workloads) producing latency p95, hit-ratio, and invalidation-count PNG reports.

## Prerequisites

- Podman 5+ (or Docker — the Makefile uses `COMPOSE ?= podman compose`, overridable to `docker compose`).
- Python 3.14+.
- Poetry 2.x.

## Quick start

```bash
make up                            # 4 containers: postgres, redis, framework, locust
curl http://127.0.0.1:4000/stats   # {"status":"booting"}
make test                          # smoke test (1 passed)
make down                          # tear down
```

Run `make help` to list all available targets.

## What's working today (Phase 0)

- 4-service podman compose stack: `postgres-15`, `redis-7`, `framework`, `locust` (idling).
- Multi-stage Dockerfile producing `framework:phase0` (~353 MB).
- One Starlette endpoint: `GET /stats` returning `{"status":"booting"}`.
- In-process pytest smoke test.

## What's NOT yet built

- GraphQL server and DSL parser — Phase 1.
- Three cache strategies (`ttl`, `operation`, `tag`) and two backends (`redis`, `in_memory`) — Phase 2.
- Locust load harness — Phase 3.
- 18-cell experiment matrix runner and matplotlib report — Phase 4.
- Polish, RUNBOOK, and final thesis-defense documentation — Phase 5.

## Project layout

```
.
├── Dockerfile
├── Makefile
├── docker-compose.yml
├── framework/        # Python package — only cli.py implements /stats today
├── pyproject.toml
└── tests/            # one smoke test
```

## License

MIT — see [LICENSE](./LICENSE).
