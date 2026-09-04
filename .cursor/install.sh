#!/usr/bin/env bash
# Idempotent Cloud Agent setup: system packages, Python deps, Postgres + catalog data.
set -euo pipefail
cd "$(dirname "$0")/.."

# LFS leaves a tiny pointer when GitHub bandwidth quota blocks the blob.
_is_real_file() {
  [ -f "$1" ] || return 1
  ! head -1 "$1" | grep -q '^version https://git-lfs.github.com/spec/v1'
}

# archive.ubuntu.com is unreachable from Cloud Agent egress; use the Azure mirror.
if grep -q 'archive.ubuntu.com\|security.ubuntu.com' /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null; then
  sudo sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu/|http://azure.archive.ubuntu.com/ubuntu/|g' \
    -e 's|http://security.ubuntu.com/ubuntu/|http://azure.archive.ubuntu.com/ubuntu/|g' \
    /etc/apt/sources.list.d/ubuntu.sources
fi

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  postgresql postgresql-client python3-venv

# Python environment (pinned versions from requirements.txt).
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

# Bring Postgres up so we can create the DB and restore the catalog during setup.
sudo pg_ctlcluster 16 main start 2>/dev/null || true
for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break; sleep 1; done

sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='boardgame'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE boardgame LOGIN PASSWORD 'boardgame';"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='boardgame_suggestion'" | grep -q 1 \
  || sudo -u postgres createdb -O boardgame boardgame_suggestion

# Restore the committed BGG catalog dump only when the DB has no games yet.
games=$(PGPASSWORD=boardgame psql -h 127.0.0.1 -U boardgame -d boardgame_suggestion \
  -tAc "SELECT count(*) FROM games" 2>/dev/null || echo 0)
if [ "${games:-0}" -eq 0 ]; then
  PGPASSWORD=boardgame pg_restore -h 127.0.0.1 -U boardgame -d boardgame_suggestion \
    --clean --if-exists --no-owner data/boardgame.dump
fi

# Restore the committed Chroma backup when the live index is missing. Skip when
# clone left an LFS pointer (see .lfsconfig skipdownloaderrors).
if [ ! -f data/chroma/.games_db_watermark ] && _is_real_file data/chroma.tar.gz; then
  ./.venv/bin/python scripts/restore_chroma.py
fi

# Local .env for the app. Secrets (OPENROUTER_API_KEY) arrive as injected env vars,
# which pydantic-settings prioritizes over this file.
if [ ! -f .env ]; then
  cat > .env <<'EOF'
LLM_PROVIDER=openrouter
EMBEDDING_PROVIDER=openrouter
OPENROUTER_MODEL=cohere/north-mini-code:free
OPENROUTER_EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b:free
CHROMA_PERSIST_DIR=./data/chroma
EMBEDDING_BATCH_SIZE=200
EMBEDDING_REQUEST_DELAY_SECONDS=1
LOG_LEVEL=INFO
DATABASE_URL=postgresql+psycopg://boardgame:boardgame@localhost:5432/boardgame_suggestion
EOF
fi

# The API is not started automatically. Without a restored Chroma index, first
# startup embeds the whole catalog (needs OPENROUTER_API_KEY, slow/costly).
# Run manually when ready:  ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
