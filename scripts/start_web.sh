#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
pnpm --dir fronted install --frozen-lockfile --ignore-scripts
pnpm --dir fronted build
export FIN_WEB=1
export FRONTEND_DIST="$(pwd)/fronted/dist"
if [ -f .env ]; then
  exec uv run --project backend --locked --no-dev --extra server --env-file .env python backend/run_web.py
fi
exec uv run --project backend --locked --no-dev --extra server python backend/run_web.py
