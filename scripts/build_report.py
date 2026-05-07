"""Phase 4 matrix-report aggregator and chart renderer.

Reads a `reports/.matrix-<ts>.list` produced by `scripts/run_matrix.sh`,
each line a path to a per-cell `reports/report-*.json`. Produces:

  - reports/matrix-report-<ts>.json - flat summary of all cells (T5).
  - docs/presentation/{latency_p95,hit_ratio,invalidations,redis_vs_memory}.png
    - 4 matplotlib charts (T6).

CLI:
    python -m scripts.build_report --matrix-list reports/.matrix-<ts>.list

Anti-scope (roadmap section 6 + orchestrator-prompt "Anti-scope"): NO seaborn /
plotly / bokeh, NO confidence intervals, NO comparison_phase / clock_skew_ms /
fingerprint metadata in the summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import sys
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import TypedDict

import matplotlib

matplotlib.use("Agg")  # non-interactive backend; no display needed
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

logger = logging.getLogger(__name__)


_TS_RE = re.compile(r"\.matrix-(\d{8}T\d{6}Z)\.list$")


_CELL_LABELS_FULL: tuple[str, ...] = (
    "ttl-redis", "ttl-memory",
    "operation-redis", "operation-memory",
    "tag-redis", "tag-memory",
    "no_cache-none",
)
_CELL_LABELS_BACKEND_AXIS: tuple[str, ...] = (
    "ttl-redis", "ttl-memory",
    "operation-redis", "operation-memory",
    "tag-redis", "tag-memory",
)
_SCENARIOS: tuple[str, ...] = ("read_heavy", "mixed", "mutation_burst")
_FIG_SIZE: tuple[int, int] = (16, 10)
_FIG_DPI: int = 100


class CellRecord(TypedDict):
    """Per-cell report shape consumed from Phase 3's runner output."""
    strategy: str
    backend: str
    scenario: str
    duration_s: int
    latency_p95_ms: float
    latency_p50_ms: float
    latency_p99_ms: float
    hit_ratio: float
    invalidations: int
    rps_avg: float
    framework_backend: str
    report_path: str


REQUIRED_TOP_KEYS: frozenset[str] = frozenset({
    "strategy", "backend", "scenario", "duration_s",
    "latency", "throughput", "cache", "framework_backend",
})


def _load_cell(path: pathlib.Path) -> CellRecord | None:
    """Load and validate one per-cell report. Returns None if malformed (logged)."""
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("skipping unreadable report %s: %s", path, exc)
        return None

    missing = REQUIRED_TOP_KEYS - data.keys()
    if missing:
        logger.warning("skipping malformed report %s (missing %s)", path, sorted(missing))
        return None

    try:
        return CellRecord(
            strategy=str(data["strategy"]),
            backend=str(data["backend"]),
            scenario=str(data["scenario"]),
            duration_s=int(data["duration_s"]),
            latency_p50_ms=float(data["latency"]["p50"]),
            latency_p95_ms=float(data["latency"]["p95"]),
            latency_p99_ms=float(data["latency"]["p99"]),
            hit_ratio=float(data["cache"]["hit_ratio"]),
            invalidations=int(data["cache"]["invalidations"]),
            rps_avg=float(data["throughput"]["rps_avg"]),
            framework_backend=str(data["framework_backend"]),
            report_path=str(path),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("skipping report %s: schema error %s", path, exc)
        return None


def load_cells(matrix_list_path: pathlib.Path) -> list[CellRecord]:
    """Read paths from the matrix list file and load each cell."""
    if not matrix_list_path.is_file():
        raise FileNotFoundError(f"matrix list not found: {matrix_list_path}")

    paths = [
        pathlib.Path(line.strip())
        for line in matrix_list_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cells: list[CellRecord] = []
    for p in paths:
        cell = _load_cell(p)
        if cell is not None:
            cells.append(cell)
    return cells


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_report",
        description="Aggregate Phase 4 matrix per-cell reports into one summary JSON + 4 PNGs.",
    )
    parser.add_argument(
        "--matrix-list",
        required=True,
        type=pathlib.Path,
        help="Path to reports/.matrix-<ts>.list produced by scripts/run_matrix.sh.",
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=pathlib.Path("docs/presentation"),
        help="Directory for the 4 PNGs (default: docs/presentation/).",
    )
    parser.add_argument(
        "--summary-out",
        type=pathlib.Path,
        default=None,
        help="Path for the matrix summary JSON. Default: reports/matrix-report-<ts>.json "
             "where <ts> is derived from the matrix list filename.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _matrix_ts_from_list_path(matrix_list: pathlib.Path) -> str:
    """Extract the matrix timestamp from `.matrix-<ts>.list` filename, or
    fall back to current UTC time if the filename doesn't match the pattern."""
    m = _TS_RE.search(str(matrix_list))
    if m:
        return m.group(1)
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_summary(cells: list[CellRecord], matrix_ts: str) -> dict[str, object]:
    """Assemble the matrix summary JSON dict (anti-scope: subset of ADR-029)."""
    return {
        "matrix_ts": matrix_ts,
        "cell_count": len(cells),
        "cells": [dict(c) for c in cells],
    }


def write_summary(
    cells: list[CellRecord],
    matrix_ts: str,
    out_path: pathlib.Path,
) -> None:
    """Serialize the summary JSON to disk (sort_keys=True for stable diffs)."""
    summary = build_summary(cells, matrix_ts)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _cell_label(cell: CellRecord) -> str:
    """Compose 'strategy-backend' label matching _CELL_LABELS_FULL."""
    return f"{cell['strategy']}-{cell['backend']}"


def _index_cells_by_label(
    cells: list[CellRecord],
) -> dict[tuple[str, str], CellRecord]:
    """Build (cell_label, scenario) -> cell map for O(1) lookup."""
    return {(_cell_label(c), c["scenario"]): c for c in cells}


def _draw_grouped_bar(
    ax: Axes,
    cells: list[CellRecord],
    metric: str,
    y_transform: Callable[[float], float] | None = None,
    legend_labels: tuple[str, ...] = _CELL_LABELS_FULL,
) -> None:
    """Render a grouped bar chart on `ax`. Bars in legend_labels order, grouped by scenario."""
    indexed = _index_cells_by_label(cells)
    n_groups = len(_SCENARIOS)
    n_bars = len(legend_labels)
    bar_width = 0.8 / n_bars
    x_base = np.arange(n_groups)

    for i, label in enumerate(legend_labels):
        values: list[float] = []
        for s in _SCENARIOS:
            cell = indexed.get((label, s))
            v: float = float(cell[metric]) if cell is not None else 0.0  # type: ignore[literal-required]
            if y_transform is not None:
                v = y_transform(v)
            values.append(v)
        offset = (i - (n_bars - 1) / 2) * bar_width
        ax.bar(x_base + offset, values, bar_width, label=label)

    ax.set_xticks(x_base)
    ax.set_xticklabels(list(_SCENARIOS))
    ax.legend(loc="upper left", fontsize=10, ncol=2)


def _save_fig(fig: Figure, out_path: pathlib.Path) -> None:
    """Save `fig` as PNG to `out_path` (mkdir parents) and close to free memory.

    No `bbox_inches="tight"` so output dimensions are exactly figsize*dpi
    (1600x1000 px) - downstream consumers and the smoke test assert that size.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=_FIG_DPI)
    plt.close(fig)


def build_chart_latency_p95(
    cells: list[CellRecord],
    out_dir: pathlib.Path,
) -> pathlib.Path:
    """Chart 1: grouped bar of p95 latency."""
    plt.rcParams["font.family"] = "DejaVu Sans"
    fig, ax = plt.subplots(figsize=_FIG_SIZE)
    _draw_grouped_bar(ax, cells, metric="latency_p95_ms")
    ax.set_title("p95 латентности по стратегии и backend'у")
    ax.set_xlabel("Сценарий нагрузки")
    ax.set_ylabel("p95 (мс)")
    out = out_dir / "latency_p95.png"
    _save_fig(fig, out)
    return out


def build_chart_hit_ratio(
    cells: list[CellRecord],
    out_dir: pathlib.Path,
) -> pathlib.Path:
    """Chart 2: grouped bar of hit_ratio in percent."""
    plt.rcParams["font.family"] = "DejaVu Sans"
    fig, ax = plt.subplots(figsize=_FIG_SIZE)
    _draw_grouped_bar(
        ax, cells, metric="hit_ratio", y_transform=lambda v: v * 100.0,
    )
    ax.set_title("Доля попаданий в кэш по стратегии и backend'у")
    ax.set_xlabel("Сценарий нагрузки")
    ax.set_ylabel("Hit ratio (%)")
    ax.set_ylim(0, 100)
    out = out_dir / "hit_ratio.png"
    _save_fig(fig, out)
    return out


def build_chart_invalidations(
    cells: list[CellRecord],
    out_dir: pathlib.Path,
) -> pathlib.Path:
    """Chart 3: grouped bar of invalidation counts."""
    plt.rcParams["font.family"] = "DejaVu Sans"
    fig, ax = plt.subplots(figsize=_FIG_SIZE)
    _draw_grouped_bar(ax, cells, metric="invalidations")
    ax.set_title("Количество инвалидаций по стратегии и backend'у")
    ax.set_xlabel("Сценарий нагрузки")
    ax.set_ylabel("Инвалидаций за прогон")
    out = out_dir / "invalidations.png"
    _save_fig(fig, out)
    return out


def build_chart_redis_vs_memory(
    cells: list[CellRecord],
    out_dir: pathlib.Path,
) -> pathlib.Path:
    """Chart 4: 1x3 subplot - for each scenario, redis vs memory at the 3 strategies."""
    plt.rcParams["font.family"] = "DejaVu Sans"
    indexed = _index_cells_by_label(cells)
    strategies: tuple[str, ...] = ("ttl", "operation", "tag")
    backends: tuple[str, ...] = ("redis", "memory")

    fig, axes = plt.subplots(1, 3, figsize=_FIG_SIZE, sharey=True)
    fig.suptitle("Redis vs in-memory: p95 латентности (мс)", fontsize=14)

    for ax, scenario in zip(axes, _SCENARIOS, strict=True):
        x_base = np.arange(len(strategies))
        for i, backend in enumerate(backends):
            values: list[float] = []
            for strategy in strategies:
                label = f"{strategy}-{backend}"
                cell = indexed.get((label, scenario))
                values.append(cell["latency_p95_ms"] if cell else 0.0)
            offset = (i - 0.5) * 0.4
            ax.bar(x_base + offset, values, 0.4, label=backend)
        ax.set_title(scenario)
        ax.set_xticks(x_base)
        ax.set_xticklabels(list(strategies))
        ax.set_xlabel("Стратегия")
        ax.legend(loc="upper left", fontsize=9)
    axes[0].set_ylabel("p95 (мс)")

    out = out_dir / "redis_vs_memory.png"
    _save_fig(fig, out)
    return out


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args(argv)

    cells = load_cells(args.matrix_list)
    if not cells:
        logger.error("no valid cells in matrix list %s", args.matrix_list)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    matrix_ts = _matrix_ts_from_list_path(args.matrix_list)

    if args.summary_out is None:
        summary_out = pathlib.Path("reports") / f"matrix-report-{matrix_ts}.json"
    else:
        summary_out = args.summary_out

    write_summary(cells, matrix_ts, summary_out)
    logger.info("wrote summary: %s (cells=%d)", summary_out, len(cells))

    p1 = build_chart_latency_p95(cells, args.out_dir)
    p2 = build_chart_hit_ratio(cells, args.out_dir)
    p3 = build_chart_invalidations(cells, args.out_dir)
    p4 = build_chart_redis_vs_memory(cells, args.out_dir)
    for p in (p1, p2, p3, p4):
        logger.info("wrote chart: %s", p)

    print(f"loaded {len(cells)} cells from {args.matrix_list}")
    print(f"summary: {summary_out}")
    print(f"charts: {args.out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
