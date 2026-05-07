"""Phase 4 unit tests for scripts/build_report.py.

Tests aggregation (load_cells, build_summary, write_summary,
_matrix_ts_from_list_path) and chart shape (4 PNG functions return 1600x1000
PNGs without raising). Pixel-level content is NOT asserted (matplotlib drift).
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Callable

import pytest
from PIL import Image

from scripts.build_report import (
    _matrix_ts_from_list_path,
    build_chart_hit_ratio,
    build_chart_invalidations,
    build_chart_latency_p95,
    build_chart_redis_vs_memory,
    build_summary,
    load_cells,
    write_summary,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "data" / "phase4"


def _matrix_list(tmp_path: pathlib.Path, fixture_paths: list[pathlib.Path]) -> pathlib.Path:
    """Write a temp matrix list pointing at the given fixture paths."""
    list_path = tmp_path / ".matrix-20260507T180000Z.list"
    list_path.write_text(
        "\n".join(str(p) for p in fixture_paths) + "\n", encoding="utf-8",
    )
    return list_path


def test_load_cells_reads_3_synthetic_fixtures(tmp_path: pathlib.Path) -> None:
    fixtures = sorted(FIXTURE_DIR.glob("cell-*.json"))
    assert len(fixtures) == 3
    list_path = _matrix_list(tmp_path, fixtures)
    cells = load_cells(list_path)
    assert len(cells) == 3
    triples = {(c["strategy"], c["backend"], c["scenario"]) for c in cells}
    assert ("tag", "redis", "read_heavy") in triples
    assert ("ttl", "memory", "mixed") in triples
    assert ("no_cache", "none", "mutation_burst") in triples


def test_load_cells_skips_malformed_silently(tmp_path: pathlib.Path) -> None:
    fixtures = sorted(FIXTURE_DIR.glob("cell-*.json"))
    paths = [*fixtures, tmp_path / "missing-cell.json"]
    list_path = _matrix_list(tmp_path, paths)
    cells = load_cells(list_path)
    assert len(cells) == 3  # missing path skipped


def test_build_summary_shape(tmp_path: pathlib.Path) -> None:
    fixtures = sorted(FIXTURE_DIR.glob("cell-*.json"))
    cells = load_cells(_matrix_list(tmp_path, fixtures))
    summary = build_summary(cells, "20260507T180000Z")
    assert summary["matrix_ts"] == "20260507T180000Z"
    assert summary["cell_count"] == 3
    cells_field = summary["cells"]
    assert isinstance(cells_field, list)
    assert len(cells_field) == 3
    # build_summary flattens to the CellRecord shape (latency_p95_ms etc.),
    # not the original nested {"latency": {...}} fixture shape.
    sample = cells_field[0]
    assert isinstance(sample, dict)
    for key in (
        "strategy", "backend", "scenario", "duration_s",
        "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
        "hit_ratio", "invalidations", "rps_avg",
        "framework_backend", "report_path",
    ):
        assert key in sample, f"missing key {key!r} in flattened cell"


def test_write_summary_writes_json(tmp_path: pathlib.Path) -> None:
    fixtures = sorted(FIXTURE_DIR.glob("cell-*.json"))
    cells = load_cells(_matrix_list(tmp_path, fixtures))
    out = tmp_path / "summary.json"
    write_summary(cells, "20260507T180000Z", out)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["matrix_ts"] == "20260507T180000Z"
    assert data["cell_count"] == 3
    assert isinstance(data["cells"], list)
    assert len(data["cells"]) == 3


def test_matrix_ts_extraction() -> None:
    p = pathlib.Path("reports/.matrix-20260507T173000Z.list")
    assert _matrix_ts_from_list_path(p) == "20260507T173000Z"

    # Fallback: pattern miss -> current UTC timestamp matching the format.
    fallback = _matrix_ts_from_list_path(pathlib.Path("custom-name.list"))
    assert re.fullmatch(r"\d{8}T\d{6}Z", fallback)


@pytest.mark.parametrize(
    ("build_fn", "filename"),
    [
        (build_chart_latency_p95, "latency_p95.png"),
        (build_chart_hit_ratio, "hit_ratio.png"),
        (build_chart_invalidations, "invalidations.png"),
        (build_chart_redis_vs_memory, "redis_vs_memory.png"),
    ],
)
def test_build_chart_returns_1600x1000_png(
    tmp_path: pathlib.Path,
    build_fn: Callable[..., pathlib.Path],
    filename: str,
) -> None:
    fixtures = sorted(FIXTURE_DIR.glob("cell-*.json"))
    cells = load_cells(_matrix_list(tmp_path, fixtures))
    out = build_fn(cells, tmp_path)
    assert out == tmp_path / filename
    assert out.exists()
    assert out.stat().st_size > 5000  # not a stub
    with Image.open(out) as img:
        assert img.size == (1600, 1000)


def test_charts_handle_missing_cells_gracefully(tmp_path: pathlib.Path) -> None:
    """Single-cell input shouldn't crash any chart fn; missing cells default to 0."""
    fixtures = [FIXTURE_DIR / "cell-tag-redis-read_heavy.json"]
    cells = load_cells(_matrix_list(tmp_path, fixtures))
    chart_fns: tuple[Callable[..., pathlib.Path], ...] = (
        build_chart_latency_p95,
        build_chart_hit_ratio,
        build_chart_invalidations,
        build_chart_redis_vs_memory,
    )
    for build_fn in chart_fns:
        out = build_fn(cells, tmp_path)
        with Image.open(out) as img:
            assert img.size == (1600, 1000)
