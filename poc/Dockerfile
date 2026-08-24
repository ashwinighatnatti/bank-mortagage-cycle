# AI-Native Mortgage — Origination to Post-Closing
#
# Two stages: build the React UI, then serve it from the same FastAPI process
# that serves the API -- the pattern README.md calls out under "Step 6":
#
#     cd frontend && npm run build       # writes frontend/dist
#     cd ../backend && python -m uvicorn app.api:app
#
# api.py mounts frontend/dist as a sibling of backend/ (it resolves the path
# from its own __file__, two parents up), so the final stage's layout has to
# keep backend/, frontend/dist/ and scripts/ siblings under the same root --
# see docker/entrypoint.sh for the same assumption on the scripts side.
#
# No secret ever lands in a layer here. FOUNDRY_API_KEY, JWT_SECRET and the
# rest arrive as environment variables at container start (App Service
# settings, a Key Vault reference, or --env-file locally) -- see the header
# of backend/app/config.py for the three delivery mechanisms this expects.

# ---------------------------------------------------------------------------
# Stage 1 — build the React UI
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /src/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — the API, serving that build
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime
WORKDIR /app

# Matches backend/requirements.txt exactly -- see requirements.docker.lock.txt
# for why this is a separate lock from the Windows-resolved
# requirements.lock.txt, and how to regenerate it.
COPY backend/requirements.docker.lock.txt ./backend/requirements.docker.lock.txt
RUN pip install --no-cache-dir --no-deps -r backend/requirements.docker.lock.txt

COPY backend/app ./backend/app
COPY scripts ./scripts
COPY --from=frontend-build /src/frontend/dist ./frontend/dist
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN chmod +x ./docker/entrypoint.sh

# Runs as an unprivileged user; backend/data/ (the generated demo book and
# the SQLite file) is created under this same tree at first boot, so it
# needs to be writable by that user before we drop root.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/backend/data \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
ENTRYPOINT ["./docker/entrypoint.sh"]
