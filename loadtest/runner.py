"""Phase 3 experiment runner: orchestrate one (strategy, backend, scenario) cell.

Flow:
  1. Scrape /stats baseline (counters at run start).
  2. Spawn locust headless via subprocess for ~90s using ExperimentShape.
  3. Scrape /stats end.
  4. Parse locust *_stats.csv for the Aggregated row (p50/p95/p99/total RPS).
  5. Write reports/report-<strategy>-<backend>-<scenario>-<ts>.json.

Phase 4's matrix runner invokes this per cell; that script handles the
DSL_PATH swap + redis FLUSHDB + framework restart, the runner is pure.

CLI:
  python -m loadtest.runner \
    --strategy=<no_cache|ttl|operation|tag> \
    --backend=<redis|memory|none> \
    --scenario=<read_heavy|mixed|mutation_burst> \
    [--target=http://localhost:4000] \
    [--stats-url=http://localhost:4000/stats] \
    [--out-dir=reports/]
"""

import argparse
import csv
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import httpx


SCENARIO_TO_USER_CLASS: dict[str, str] = {
    "read_heavy": "ReadHeavyUser",
    "mixed": "WriteMixUser",
    "mutation_burst": "MutationBurstUser",
}


def _utc_iso(now: datetime | None = None) -> str:
    """Return ISO-8601 UTC timestamp with Z suffix."""
    dt = now or datetime.now(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_compact_ts(now: datetime | None = None) -> str:
    """Return YYYYMMDDTHHMMSS for filename use."""
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S")


def _scrape_stats(url: str) -> dict[str, Any]:
    """GET /stats; return the parsed JSON dict. Raises on non-200."""
    r = httpx.get(url, timeout=5.0)
    r.raise_for_status()
    body: dict[str, Any] = r.json()
    return body


def _run_locust(
    locustfile: pathlib.Path,
    host: str,
    user_class: str,
    csv_prefix: pathlib.Path,
) -> int:
    """Run locust headless with ExperimentShape; return its exit code."""
    cmd: list[str] = [
        "poetry", "run", "locust",
        "--headless",
        "-f", str(locustfile),
        # ExperimentShape is auto-discovered from the locustfile's namespace
        # (imported there for that purpose); locust 2.43 has no --shape-class flag.
        "-u", "200",
        "-r", "50",
        "--host", host,
        "--csv", str(csv_prefix),
        # User-class as a positional arg per locust 2.43 (no --user-classes flag).
        user_class,
    ]
    completed = subprocess.run(cmd, check=False)
    return completed.returncode


def _parse_locust_csv(
    csv_prefix: pathlib.Path,
) -> tuple[dict[str, float], dict[str, float]]:
    """Read <csv_prefix>_stats.csv, return (latency_block, throughput_block).

    locust 2.43 writes columns including:
      Type, Name, Request Count, Failure Count, Median Response Time,
      Average Response Time, Min Response Time, Max Response Time,
      Average Content Size, Requests/s, Failures/s,
      50%, 66%, 75%, 80%, 90%, 95%, 98%, 99%, 99.9%, 99.99%, 100%

    We extract the "Aggregated" row (Type=='', Name=='Aggregated') for
    latency p50/p95/p99 and total RPS.
    """
    csv_path = pathlib.Path(f"{csv_prefix}_stats.csv")
    if not csv_path.exists():
        raise FileNotFoundError(f"locust CSV not found: {csv_path}")

    aggregated: dict[str, str] | None = None
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") == "Aggregated":
                aggregated = row
                break
    if aggregated is None:
        raise RuntimeError(f"no Aggregated row in {csv_path}")

    def _f(col: str) -> float:
        val = aggregated.get(col, "") if aggregated is not None else ""
        try:
            return float(val) if val not in ("", "N/A") else 0.0
        except ValueError:
            return 0.0

    latency = {
        "p50": _f("50%"),
        "p95": _f("95%"),
        "p99": _f("99%"),
    }
    throughput = {
        "rps_avg": _f("Requests/s"),
        "total_requests": _f("Request Count"),
    }
    return latency, throughput


def _delta_int_dict(end: dict[str, int], start: dict[str, int]) -> dict[str, int]:
    """Return end[k] - start[k] for every key, defaulting missing keys to 0."""
    keys = set(end) | set(start)
    return {k: int(end.get(k, 0)) - int(start.get(k, 0)) for k in keys}


def build_report(
    strategy: str,
    backend: str,
    scenario: str,
    start_stats: dict[str, Any],
    end_stats: dict[str, Any],
    latency: dict[str, float],
    throughput: dict[str, float],
    started_at: str,
    ended_at: str,
    duration_s: int,
) -> dict[str, Any]:
    """Assemble the per-cell report JSON."""
    cache_hits = int(end_stats.get("cache_hits", 0)) - int(start_stats.get("cache_hits", 0))
    cache_misses = int(end_stats.get("cache_misses", 0)) - int(start_stats.get("cache_misses", 0))
    cache_sets = (cache_hits + cache_misses)  # not strictly accurate; sets ~= misses but preserve spec
    cache_invalidations = int(end_stats.get("invalidations", 0)) - int(start_stats.get("invalidations", 0))
    total_for_ratio = cache_hits + cache_misses
    hit_ratio = (cache_hits / total_for_ratio) if total_for_ratio > 0 else 0.0

    return {
        "strategy": strategy,
        "backend": backend,
        "scenario": scenario,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": duration_s,
        "latency": latency,
        "throughput": throughput,
        "cache": {
            "hits": cache_hits,
            "misses": cache_misses,
            "hit_ratio": hit_ratio,
            "invalidations": cache_invalidations,
            "sets": cache_sets,
        },
        "request_count_by_op": _delta_int_dict(
            end_stats.get("request_count_by_op", {}),
            start_stats.get("request_count_by_op", {}),
        ),
        "errors": _delta_int_dict(
            end_stats.get("errors", {}),
            start_stats.get("errors", {}),
        ),
        "framework_backend": end_stats.get("backend", "none"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 experiment runner")
    parser.add_argument("--strategy", required=True,
                        choices=["no_cache", "ttl", "operation", "tag"])
    parser.add_argument("--backend", required=True,
                        choices=["redis", "memory", "none"])
    parser.add_argument("--scenario", required=True,
                        choices=list(SCENARIO_TO_USER_CLASS.keys()))
    parser.add_argument("--target", default="http://localhost:4000")
    parser.add_argument("--stats-url", default="http://localhost:4000/stats")
    parser.add_argument("--out-dir", default="reports")
    args = parser.parse_args(argv)

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    user_class = SCENARIO_TO_USER_CLASS[args.scenario]
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    locustfile = repo_root / "loadtest" / "locustfile.py"

    start_iso = _utc_iso()
    ts_compact = _utc_compact_ts()
    csv_prefix = out_dir / f"locust-{args.strategy}-{args.backend}-{args.scenario}-{ts_compact}"

    # 1. Pre-run scrape.
    start_stats = _scrape_stats(args.stats_url)

    # 2. Run locust.
    rc = _run_locust(locustfile, args.target, user_class, csv_prefix)
    if rc != 0:
        print(f"locust exited non-zero: {rc}", file=sys.stderr)
        return rc

    # 3. Post-run scrape.
    end_stats = _scrape_stats(args.stats_url)

    # 4. Parse latency.
    latency, throughput = _parse_locust_csv(csv_prefix)

    # 5. Write report.
    end_iso = _utc_iso()
    report = build_report(
        strategy=args.strategy,
        backend=args.backend,
        scenario=args.scenario,
        start_stats=start_stats,
        end_stats=end_stats,
        latency=latency,
        throughput=throughput,
        started_at=start_iso,
        ended_at=end_iso,
        duration_s=90,
    )
    out_path = out_dir / f"report-{args.strategy}-{args.backend}-{args.scenario}-{ts_compact}.json"
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
