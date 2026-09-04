#!/usr/bin/env bash
# Per-boot: ensure Postgres is running before the API terminal starts.
set -euo pipefail
sudo pg_ctlcluster 16 main start 2>/dev/null || true
for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break; sleep 1; done
pg_isready -h 127.0.0.1 -p 5432
