"""Sample CPU-bound test workload for exercising ScaleScope / a real HPA.

Self-contained: a background task randomizes its own load internally
(CPU intensity, a simulated memory-leak-and-recover cycle, and synthetic
demand/latency/error-rate signals), so deploying this alone - with no
external load generator - produces varying CPU/memory usage a real HPA
can react to, and real anomaly signals ScaleScope's diagnosis engine can
classify. `scripts/generate-load.sh` still works for on-demand extra load
against `/work`, but is no longer required for the demo to be alive.
"""

import asyncio
import hashlib
import logging
import os
import random
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sample_workload")

_HASH_INPUT = b"scalescope-sample-workload-load-generator"
_TICK_SECONDS = float(os.environ.get("SIM_TICK_SECONDS", "1.0"))
_LATENCY_WINDOW_SIZE = 200

# Plain instantaneous gauges (not counters needing PromQL rate()) so a
# simple text scrape can read them directly - see
# src/scalescope/k8s_collector.py's `_scrape_workload_metrics`. Renaming
# these breaks that contract; keep the two in sync if either changes.
REQUEST_COUNT = Counter(
    "sample_workload_requests_total", "Total HTTP requests", ["path", "status"]
)
REQUEST_LATENCY = Histogram(
    "sample_workload_request_duration_seconds", "Request duration in seconds", ["path"]
)
DEMAND_RPS = Gauge("sample_workload_demand_rps", "Current simulated request rate")
LATENCY_P95_MS = Gauge(
    "sample_workload_latency_p95_ms", "Rolling p95 request latency, ms"
)
ERROR_RATE = Gauge(
    "sample_workload_error_rate", "Rolling fraction of requests erroring, 0-1"
)
SIMULATED_FAULT = Gauge(
    "sample_workload_simulated_fault",
    "1 if a simulated fault is currently active, else 0",
)
MEMORY_LEAK_BYTES = Gauge(
    "sample_workload_leak_bytes", "Bytes currently held by the simulated memory leak"
)

_recent_latencies_ms: deque[float] = deque(maxlen=_LATENCY_WINDOW_SIZE)
_recent_outcomes: deque[bool] = deque(maxlen=_LATENCY_WINDOW_SIZE)  # True = error
_leak_buffer: bytearray = bytearray()


def _record_outcome(latency_ms: float, is_error: bool) -> None:
    _recent_latencies_ms.append(latency_ms)
    _recent_outcomes.append(is_error)
    if _recent_latencies_ms:
        ordered = sorted(_recent_latencies_ms)
        p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        LATENCY_P95_MS.set(ordered[p95_index])
    if _recent_outcomes:
        ERROR_RATE.set(sum(_recent_outcomes) / len(_recent_outcomes))


def _burn_cpu(iterations: int) -> str:
    """Perform `iterations` rounds of SHA-256 hashing and return the digest.

    Real CPU work, not a sleep: it consumes cycles for the duration of the
    loop, which is what makes load translate into observable CPU usage.
    """
    digest = _HASH_INPUT
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()


class _LoadPhase:
    """One stretch of simulated behaviour: how much CPU/error/leak to produce."""

    def __init__(
        self,
        name: str,
        duration_s: float,
        demand_rps: tuple[float, float],
        error_rate: float,
        leak_bytes_per_tick: int,
    ) -> None:
        self.name = name
        self.duration_s = duration_s
        self.demand_rps = demand_rps
        self.error_rate = error_rate
        self.leak_bytes_per_tick = leak_bytes_per_tick


# Fixed-duration timeline, selected from wall-clock time rather than each
# replica's own random.Random(). With N independent replicas each picking
# random phases/durations, the cross-replica AVERAGE CPU (what the HPA and
# ScaleScope actually observe) barely moves - by the law of large numbers,
# independent per-pod randomness averages out to a near-constant medium
# load, which never triggers a real scale-down. Every replica computing the
# same phase from the same clock makes the whole workload move together, so
# it actually contains distinct low periods to scale down into.
_TIMELINE = [
    _LoadPhase("idle", 60, (2, 8), 0.0, 0),
    _LoadPhase("moderate", 45, (10, 25), 0.0, 0),
    _LoadPhase("traffic_spike", 30, (60, 100), 0.01, 0),
    _LoadPhase("moderate", 30, (10, 25), 0.0, 0),
    _LoadPhase("idle", 45, (2, 8), 0.0, 0),
    _LoadPhase("memory_leak", 60, (10, 20), 0.0, 300_000),
    _LoadPhase("idle", 40, (2, 8), 0.0, 0),
    _LoadPhase("error_burst", 20, (15, 30), 0.35, 0),
    _LoadPhase("idle", 50, (2, 8), 0.0, 0),
]
_TIMELINE_TOTAL_SECONDS = sum(phase.duration_s for phase in _TIMELINE)

# Container memory limit is 128Mi (see k8s/deployment.yaml); cap simulated
# leak growth below that so the demo shows a climbing-memory anomaly for the
# diagnosis engine to catch, without actually forcing an OOMKill.
_LEAK_CAP_BYTES = 90 * 1024 * 1024

_TRIGGER_PHASES = {
    "cpu": _LoadPhase("manual_cpu_spike", 0, (150, 220), 0.0, 0),
    "memory": _LoadPhase("manual_memory_leak", 0, (10, 20), 0.0, 400_000),
    "traffic": _LoadPhase("manual_traffic_spike", 0, (80, 130), 0.02, 0),
}

_manual_override: _LoadPhase | None = None
_manual_override_until: float = 0.0


def _current_phase(now: float) -> _LoadPhase:
    global _manual_override
    if _manual_override is not None:
        if now < _manual_override_until:
            return _manual_override
        _manual_override = None

    position = now % _TIMELINE_TOTAL_SECONDS
    for phase in _TIMELINE:
        if position < phase.duration_s:
            return phase
        position -= phase.duration_s
    return _TIMELINE[0]


async def _load_simulator() -> None:
    rng = random.Random()
    last_phase_name: str | None = None
    while True:
        phase = _current_phase(time.time())
        if phase.name != last_phase_name:
            logger.info("phase=%s", phase.name)
            SIMULATED_FAULT.set(0.0 if phase.name in ("idle", "moderate") else 1.0)
            if last_phase_name == "memory_leak" and _leak_buffer:
                logger.info(
                    "memory_leak phase ended, releasing %d bytes", len(_leak_buffer)
                )
                _leak_buffer.clear()
                MEMORY_LEAK_BYTES.set(0)
            last_phase_name = phase.name

        demand = rng.uniform(*phase.demand_rps)
        DEMAND_RPS.set(demand)

        iterations = int(demand * 4_000)
        start = time.monotonic()
        await asyncio.to_thread(_burn_cpu, max(iterations, 1_000))
        latency_ms = (time.monotonic() - start) * 1000
        is_error = rng.random() < phase.error_rate
        _record_outcome(latency_ms, is_error)
        REQUEST_COUNT.labels(
            path="/internal-load", status="500" if is_error else "200"
        ).inc()

        if phase.leak_bytes_per_tick:
            grow = min(phase.leak_bytes_per_tick, _LEAK_CAP_BYTES - len(_leak_buffer))
            if grow > 0:
                _leak_buffer.extend(b"x" * grow)
            MEMORY_LEAK_BYTES.set(len(_leak_buffer))

        await asyncio.sleep(_TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_load_simulator())
    logger.info("self-load simulator started, tick=%.1fs", _TICK_SECONDS)
    yield
    task.cancel()


app = FastAPI(title="scalescope-sample-workload", lifespan=lifespan)


@app.middleware("http")
async def _record_metrics(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    elapsed = time.monotonic() - start
    path = request.url.path
    REQUEST_COUNT.labels(path=path, status=str(response.status_code)).inc()
    REQUEST_LATENCY.labels(path=path).observe(elapsed)
    return response


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz", response_class=PlainTextResponse)
@app.get("/", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/work")
def work(
    iterations: int = Query(default=200_000, ge=1, le=20_000_000)
) -> dict[str, object]:
    """On-demand extra CPU work, independent of the background self-load simulator."""
    start = time.monotonic()
    digest = _burn_cpu(iterations)
    elapsed = time.monotonic() - start
    logger.debug("work request iterations=%d elapsed=%.4fs", iterations, elapsed)
    return {"iterations": iterations, "elapsed_seconds": elapsed, "digest": digest}


@app.post("/trigger")
def trigger(
    kind: str, duration_seconds: int = Query(default=45, ge=5, le=300)
) -> dict[str, object]:
    """Force a named load pattern now, overriding the background timeline for `duration_seconds`."""
    global _manual_override, _manual_override_until
    if kind not in _TRIGGER_PHASES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind: {kind} (expected one of {sorted(_TRIGGER_PHASES)})",
        )
    _manual_override = _TRIGGER_PHASES[kind]
    _manual_override_until = time.time() + duration_seconds
    logger.info("manual trigger: kind=%s duration=%ds", kind, duration_seconds)
    return {
        "kind": kind,
        "phase": _manual_override.name,
        "duration_seconds": duration_seconds,
    }
