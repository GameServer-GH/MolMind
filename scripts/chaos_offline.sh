#!/usr/bin/env bash
# 断网混沌：强制 offline，验证无外网仍可出 CSV。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${TMPDIR:-/tmp}/molmind_chaos_out.csv"
export MOLMIND_MODE=offline
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$ROOT/mvp2/.venv/bin/python" ]]; then
    PYTHON="$ROOT/mvp2/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || command -v python)"
  fi
fi
"$PYTHON" -m apps.cli.main --input data/sample.sdf --output "$OUT" --mode offline
test -s "$OUT"
# 断言无 SI/EC50 列
head -1 "$OUT" | grep -qv 'SI\|EC50\|CC50'
echo "CHAOS PASS: offline CSV written to $OUT"
