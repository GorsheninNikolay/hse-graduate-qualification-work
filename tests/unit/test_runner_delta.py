"""Unit tests for loadtest.runner.{build_report, _delta_int_dict, SCENARIO_TO_USER_CLASS}."""

from typing import Any

from loadtest.runner import (
    SCENARIO_TO_USER_CLASS,
    _delta_int_dict,
    build_report,
)


def test_scenario_user_class_map() -> None:
    assert SCENARIO_TO_USER_CLASS == {
        "read_heavy": "ReadHeavyUser",
        "mixed": "WriteMixUser",
        "mutation_burst": "MutationBurstUser",
    }


def test_delta_int_dict_basic() -> None:
    end = {"a": 5, "b": 3}
    start = {"a": 2, "b": 0}
    assert _delta_int_dict(end, start) == {"a": 3, "b": 3}


def test_delta_int_dict_handles_missing_keys() -> None:
    end = {"a": 5, "c": 10}
    start = {"a": 2, "b": 1}
    assert _delta_int_dict(end, start) == {"a": 3, "b": -1, "c": 10}


def _stats_baseline() -> dict[str, Any]:
    return {
        "cache_hits": 0,
        "cache_misses": 0,
        "invalidations": 0,
        "errors": {},
        "request_count_by_op": {},
        "backend": "redis",
    }


def test_build_report_required_keys_and_shape() -> None:
    start = _stats_baseline()
    end = {
        "cache_hits": 100,
        "cache_misses": 50,
        "invalidations": 5,
        "errors": {},
        "request_count_by_op": {"getUser": 150},
        "backend": "redis",
    }

    report = build_report(
        strategy="tag",
        backend="redis",
        scenario="read_heavy",
        start_stats=start,
        end_stats=end,
        latency={"p50": 5.0, "p95": 12.0, "p99": 25.0},
        throughput={"rps_avg": 150.0, "total_requests": 150},
        started_at="2026-05-07T13:00:00Z",
        ended_at="2026-05-07T13:01:30Z",
        duration_s=90,
    )

    required_keys = {
        "strategy", "backend", "scenario", "started_at", "ended_at",
        "duration_s", "latency", "throughput", "cache",
        "request_count_by_op", "errors", "framework_backend",
    }
    assert set(report) == required_keys
    assert report["strategy"] == "tag"
    assert report["framework_backend"] == "redis"
    assert report["cache"]["hits"] == 100
    assert report["cache"]["misses"] == 50
    assert abs(report["cache"]["hit_ratio"] - 100 / 150) < 1e-6
    assert report["cache"]["invalidations"] == 5
    assert report["latency"] == {"p50": 5.0, "p95": 12.0, "p99": 25.0}
    assert report["request_count_by_op"] == {"getUser": 150}


def test_build_report_zero_requests_gives_zero_hit_ratio() -> None:
    """Avoid div-by-zero when no traffic hit the framework during the cell."""
    start = _stats_baseline()
    end = _stats_baseline()

    report = build_report(
        strategy="ttl",
        backend="memory",
        scenario="read_heavy",
        start_stats=start,
        end_stats=end,
        latency={"p50": 0.0, "p95": 0.0, "p99": 0.0},
        throughput={"rps_avg": 0.0, "total_requests": 0},
        started_at="2026-05-07T13:00:00Z",
        ended_at="2026-05-07T13:01:30Z",
        duration_s=90,
    )
    assert report["cache"]["hit_ratio"] == 0.0


def test_build_report_errors_delta() -> None:
    start = {**_stats_baseline(), "errors": {"multiplicity.violation": 2}}
    end = {**_stats_baseline(), "errors": {"multiplicity.violation": 5, "framework.internal_error": 1}}

    report = build_report(
        strategy="operation",
        backend="redis",
        scenario="mixed",
        start_stats=start,
        end_stats=end,
        latency={"p50": 1.0, "p95": 2.0, "p99": 3.0},
        throughput={"rps_avg": 100.0, "total_requests": 100},
        started_at="2026-05-07T13:00:00Z",
        ended_at="2026-05-07T13:01:30Z",
        duration_s=90,
    )
    assert report["errors"] == {"multiplicity.violation": 3, "framework.internal_error": 1}
