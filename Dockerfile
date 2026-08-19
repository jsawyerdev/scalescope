FROM python:3.13-slim AS base

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV SCALESCOPE_DATA_DIR=/data \
    SCALESCOPE_DB_PATH=/data/scalescope.duckdb \
    SCALESCOPE_MODE=demo

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/workloads')" || exit 1

CMD ["uvicorn", "scalescope.main:app", "--host", "0.0.0.0", "--port", "8000"]
