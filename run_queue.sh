#!/usr/bin/env bash
# Sequentially download the sparse Traficom layers after Rannikkokartat.
# Resumable: re-run to continue. Launch after the Rannikkokartat run finishes
# so only one download hits Traficom at a time.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p mbtiles

echo "===== [$(date '+%F %T')] Satamakartat (descent, full-until 11) ====="
uv run traficom_dl.py --layer "Satamakartat" --mode descent --full-until 11 \
  --maxzoom 15 --concurrency 10 --out mbtiles/satamakartat.mbtiles

echo "===== [$(date '+%F %T')] Veneilykartat public (descent, full-until 10) ====="
uv run traficom_dl.py --layer "Veneilykartat public" --mode descent --full-until 10 \
  --maxzoom 15 --concurrency 10 --out mbtiles/veneilykartat.mbtiles

echo "===== [$(date '+%F %T')] queue complete ====="
ls -lh mbtiles/