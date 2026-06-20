# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: install the backend (and its dependencies) into an isolated venv.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build into a self-contained virtual environment so we can copy just that
# into the final image.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Only the files needed to build/install the server package. The client code is
# listed in pyproject's package list, so it must be present for the build, but
# its (heavier) dependencies are not installed via the "server" extra.
COPY pyproject.toml README.md ./
COPY package.json package-lock.json ./
COPY common ./common
COPY server ./server
COPY client ./client

RUN npm ci --omit=dev
RUN pip install ".[server]"

# ---------------------------------------------------------------------------
# Runtime: copy the prepared venv into a clean, minimal image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NODE_PATH="/opt/rtp-node/node_modules" \
    RTP_SERVER_HOST=0.0.0.0 \
    RTP_SERVER_PORT=8000

# Run as an unprivileged user.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

# The installed package (including the bundled web UI) lives entirely in the venv.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /src/node_modules /opt/rtp-node/node_modules

# Working directory where an optional .env can be mounted at runtime.
WORKDIR /app
USER appuser

EXPOSE 8000

# Basic liveness check against the unauthenticated health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; \
urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('RTP_SERVER_PORT','8000')}/api/health\").read()" \
    || exit 1

CMD ["rtp-server"]
