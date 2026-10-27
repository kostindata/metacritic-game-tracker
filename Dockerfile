FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install dependencies only (layer cached until pyproject.toml changes); the code is
# run from /app, not installed, so tests and the app always use the source tree.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install ".[dev]" \
    && pip uninstall -y metacritic-game-tracker \
    && rm -rf app build *.egg-info

# Headless Chromium for pages that only render with JavaScript (fallback path).
ARG INSTALL_PLAYWRIGHT=true
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then playwright install --with-deps chromium; fi

COPY . .

# /data/covers is a volume; creating it here gives a new volume the app user's ownership.
RUN useradd --create-home appuser && mkdir -p /data/covers && chown -R appuser /app /data/covers
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
