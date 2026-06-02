#!/usr/bin/env bash
# Apply database migrations, then run the provided command (the API server).
set -euo pipefail

echo "Running database migrations..."
alembic upgrade head

echo "Starting: $*"
exec "$@"
