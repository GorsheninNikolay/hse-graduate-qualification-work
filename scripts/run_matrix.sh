#!/usr/bin/env bash
set -euo pipefail

# scripts/run_matrix.sh — experiment matrix runner.
#
# Iterates 21 cells (3 strategies × 2 backends × 3 scenarios + 3 no_cache
# baselines), reusing the `make experiment` target for each cell.
# Records the per-cell report paths to reports/.matrix-<ts>.list so the
# aggregator (scripts/build_report.py) gets an exact set rather than a fuzzy
# glob.
#
# Modes:
#   quick — MEASURED_DURATION_S=10 (set via env by `make experiment-quick`).
#   full  — default 60s measured, ~30 min total.
#
# Usage: scripts/run_matrix.sh {quick|full}

MODE="${1:-full}"
case "${MODE}" in
  quick|full) ;;
  *) echo "usage: $0 {quick|full}" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

MATRIX_TS="$(date -u +%Y%m%dT%H%M%SZ)"
MATRIX_LIST="reports/.matrix-${MATRIX_TS}.list"
TIMING_FILE="reports/.matrix-${MATRIX_TS}.timing"
mkdir -p reports
: > "${MATRIX_LIST}"

START_S=$(date +%s)
echo "matrix run starting at ${MATRIX_TS} (mode=${MODE})"
echo "list: ${MATRIX_LIST}"

# --- Cache cells: 3 strategies × 2 backends × 3 scenarios ---
STRATEGIES=(ttl operation tag)
BACKENDS=(redis memory)
SCENARIOS=(read_heavy mixed mutation_burst)

CELL_NUM=0
TOTAL_CELLS=21

run_cell() {
  local inv="$1" backend="$2" scenario="$3"
  CELL_NUM=$((CELL_NUM + 1))
  echo "--- cell ${CELL_NUM}/${TOTAL_CELLS}: INV=${inv} BACKEND=${backend} SCENARIO=${scenario} ---"

  # Snapshot timestamp BEFORE the cell runs so we can find the produced report.
  local before_ts
  before_ts="$(date -u +%Y%m%dT%H%M%SZ)"

  # Continue on per-cell failure (single-run matrix; one bad cell shouldn't
  # invalidate the rest).
  if ! INV="${inv}" BACKEND="${backend}" SCENARIO="${scenario}" make experiment; then
    echo "  cell FAILED — continuing" >&2
    return 0
  fi

  # Find the report produced by this cell. The runner writes one file matching
  # report-<inv>-<backend>-<scenario>-<ts>.json with ts AFTER `before_ts`.
  local pattern="reports/report-${inv}-${backend}-${scenario}-*.json"
  local newest
  newest="$(ls -1 ${pattern} 2>/dev/null | sort | tail -n1 || true)"
  if [[ -z "${newest}" ]]; then
    echo "  WARNING: no report file matched ${pattern}" >&2
    return 0
  fi
  echo "${newest}" >> "${MATRIX_LIST}"

  # Inter-cell hygiene: redis FLUSHDB + framework restart so cache state from
  # the previous cell doesn't bleed into this one. Postgres state is preserved
  # (saves ~10s/cell over a full down/up cycle and matches Phase 2's integration
  # test convention; mutation cells leave small residue, acceptable for MVP).
  podman compose exec -T redis redis-cli FLUSHDB >/dev/null 2>&1 || true
  podman compose restart framework >/dev/null 2>&1 || true
  sleep 8
}

for inv in "${STRATEGIES[@]}"; do
  for backend in "${BACKENDS[@]}"; do
    for scenario in "${SCENARIOS[@]}"; do
      run_cell "${inv}" "${backend}" "${scenario}"
    done
  done
done

# --- No-cache baseline cells: 3 scenarios, --backend=none ---
for scenario in "${SCENARIOS[@]}"; do
  run_cell no_cache none "${scenario}"
done

END_S=$(date +%s)
WALL_S=$((END_S - START_S))
echo "${WALL_S}" > "${TIMING_FILE}"

CELLS_RECORDED="$(wc -l < "${MATRIX_LIST}" | tr -d ' ')"
echo
echo "=== matrix run done ==="
echo "mode:           ${MODE}"
echo "wall_clock_s:   ${WALL_S}"
echo "cells recorded: ${CELLS_RECORDED} / ${TOTAL_CELLS}"
echo "list:           ${MATRIX_LIST}"
echo "timing:         ${TIMING_FILE}"
