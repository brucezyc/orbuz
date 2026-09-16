#!/usr/bin/env bash
# Run both arms of every review case and aggregate the comparison.
#
# Per case, the fan-out and the equal-cost baseline run in parallel: both arms wait on the same
# API and nothing about the comparison depends on them not overlapping. A case that already has
# both reports is skipped, so the pool can be resumed after an interruption.
set -u
cd "$(dirname "$0")/../.."
ROOT=${1:-/root/bench/review}

for case in $(find "$ROOT" -mindepth 3 -maxdepth 3 -name meta.json -not -path '*_stale*' -printf '%h\n' | sort); do
  [ -f "$case/review-fanout.json" ] && [ -f "$case/review-baseline.json" ] && continue
  echo "=== $(basename "$case") ==="
  python3 bench/review/run_review.py "$case" --personas all --max-calls 10 > "$case/fanout.log" 2>&1 &
  fan=$!
  python3 bench/review/run_review.py "$case" --personas merged --max-calls 60 > "$case/baseline.log" 2>&1 &
  base=$!
  wait "$fan" "$base"
  grep -h '"mode"' "$case/fanout.log" "$case/baseline.log" 2>/dev/null | tail -2
done

python3 bench/review/aggregate.py --root "$ROOT" --out "$ROOT/pool.json"
