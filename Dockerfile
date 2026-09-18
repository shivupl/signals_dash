FROM python:3.12-slim

# UTC everywhere inside the container. Eastern time exists only at the parse and
# render boundaries; anything else is a DST bug waiting for November.
ENV TZ=UTC \
    PGTZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# lxml needs libxml2/libxslt at build time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e ".[dev]"

COPY config/ ./config/
COPY db/ ./db/
COPY scripts/ ./scripts/

RUN useradd --create-home --uid 1000 signals && chown -R signals:signals /app
USER signals

CMD ["python", "-m", "signals", "worker"]
