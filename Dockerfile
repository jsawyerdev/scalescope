# syntax=docker/dockerfile:1.7

FROM python:3.14-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/London \
    PYTHONOPTIMIZE=1 \
    PYTHONHASHSEED=0 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_PROGRESS_BAR=off

RUN apt-get update -q && \
    apt-get upgrade -qy && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies install from pyproject.toml alone so this layer only
# invalidates when declared dependencies change, not on every source edit.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p src/scalescope && touch src/scalescope/__init__.py \
    && pip install --upgrade pip setuptools wheel \
    && pip install . 

COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade --no-cache-dir  --no-deps .

FROM python:3.14-slim AS runtime

RUN apt-get update -q && \
    apt-get upgrade -qy && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 1000 scalescope

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    SCALESCOPE_DB_PATH=/data/scalescope.duckdb \
    SCALESCOPE_MODE=demo \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown scalescope:scalescope /data
USER scalescope

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/workloads')" || exit 1

CMD ["uvicorn", "scalescope.main:app", "--host", "0.0.0.0", "--port", "8000"]
