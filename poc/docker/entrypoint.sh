#!/bin/sh
# Container startup: make the image self-sufficient, then run the app.
#
# The synthetic book (backend/data/) is deliberately not baked into the
# image -- it's regenerable and gitignored for the same reason (see
# .gitignore). Generation is deterministic (a fixed SEED and a fixed TODAY
# in generate_synthetic_data.py), so producing it fresh on first boot is
# safe and always yields the same demo book. `init_db.py` is idempotent --
# safe to run on every start, whether or not the book was just generated.
#
# Secrets (FOUNDRY_API_KEY, JWT_SECRET, ...) are read from the environment
# at request time by app.config, never baked into this image or this
# script. See backend/app/config.py.
#
# Relies on the container's working directory being /app (the Dockerfile's
# WORKDIR), with backend/, frontend/dist/ and scripts/ as its siblings --
# the same layout scripts/*.py and backend/app/config.py already assume
# when resolving paths from `Path(__file__).resolve().parents[...]`.
set -e

if [ ! -f backend/data/loans.json ]; then
  echo "No synthetic book on disk -- generating the demo data (deterministic, ~12 loans)..."
  python scripts/generate_synthetic_data.py
fi

echo "Ensuring the database exists and is seeded..."
python scripts/init_db.py

echo "Starting the API on port ${PORT:-8000}..."
exec uvicorn app.api:app --host 0.0.0.0 --port "${PORT:-8000}" --app-dir backend
