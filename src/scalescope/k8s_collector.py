"""Read-only Kubernetes telemetry collector for OBSERVE mode.

Populates the same observation schema DEMO mode's simulator produces, from
a real Deployment's Pod/metrics.k8s.io state. Uses only get/list verbs on
apps/v1 Deployments, core/v1 Pods, and (if metrics-server is installed)
metrics.k8s.io PodMetrics - see k8s/rbac/ for the exact Role this needs.
Never writes to the cluster.

Honesty note: `request_rate`, `latency_p95_ms`, and `error_rate` are not
derivable from the Kubernetes API or metrics-server alone - they need
request-level instrumentation this collector does not otherwise have. When
`metrics_url` is configured, they're read from that workload's own
Prometheus `/metrics` (see `sample-workload/app/main.py`'s
`sample_workload_demand_rps`/`latency_p95_ms`/`error_rate` gauges for the
expected contract); when it isn't, they're reported as 0.0 rather than a
fabricated value. `cpu_throttled_pct` has no source in either case yet
(needs cAdvisor container_cpu_cfs_throttled data, not exposed here) and is
always 0.0.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx
from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

_METRIC_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9.eE+\-]+|NaN|\+Inf|-Inf)\s*$"
)
_SCRAPED_GAUGE_NAMES = {
    "sample_workload_demand_rps": "request_rate",
    "sample_workload_latency_p95_ms": "latency_p95_ms",
    "sample_workload_error_rate": "error_rate",
}


def _parse_prometheus_gauges(text: str, names: set[str]) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE_RE.match(line)
        if not match or match.group(1) not in names:
            continue
        try:
            values[match.group(1)] = float(match.group(2))
        except ValueError:
            continue
    return values


_MEMORY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


class KubernetesUnavailableError(RuntimeError):
    """Raised when the configured cluster/deployment cannot be reached."""


def load_k8s_config(kubeconfig_path: str | None) -> None:
    """Load in-cluster config when running as a Pod, else a kubeconfig file."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config(config_file=kubeconfig_path)


def _parse_cpu_millicores(value: str) -> float:
    if value.endswith("n"):
        return float(value[:-1]) / 1_000_000
    if value.endswith("u"):
        return float(value[:-1]) / 1_000
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000


def _parse_memory_bytes(value: str) -> float:
    for suffix, multiplier in _MEMORY_SUFFIXES.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier
    return float(value)


class KubernetesObservationCollector:
    """Collects one observation row per tick for a single Deployment."""

    def __init__(
        self,
        namespace: str,
        deployment_name: str,
        kubeconfig_path: str | None = None,
        metrics_url: str | None = None,
    ) -> None:
        load_k8s_config(kubeconfig_path)
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._metrics_url = metrics_url
        self._apps = client.AppsV1Api()
        self._core = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()

    def collect(self) -> dict:
        try:
            deployment = self._apps.read_namespaced_deployment(
                self._deployment_name, self._namespace
            )
        except ApiException as exc:
            raise KubernetesUnavailableError(
                f"deployment {self._namespace}/{self._deployment_name} unreachable: {exc.reason}"
            ) from exc

        match_labels = deployment.spec.selector.match_labels or {}
        label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        pods = self._core.list_namespaced_pod(
            self._namespace, label_selector=label_selector
        ).items

        pending_pods = sum(1 for p in pods if p.status.phase == "Pending")
        restarts = sum(
            cs.restart_count for p in pods for cs in (p.status.container_statuses or [])
        )
        cpu_usage_pct, memory_usage_mb = self._pod_resource_usage(pods)
        scraped = self._scrape_workload_metrics()

        return {
            "ts": datetime.now(UTC),
            "workload": self._deployment_name,
            "replicas": deployment.status.replicas or 0,
            "request_rate": scraped.get("request_rate", 0.0),
            "cpu_usage_pct": cpu_usage_pct,
            "cpu_throttled_pct": 0.0,
            "memory_usage_mb": memory_usage_mb,
            "latency_p95_ms": scraped.get("latency_p95_ms", 0.0),
            "error_rate": scraped.get("error_rate", 0.0),
            "pending_pods": pending_pods,
            "restarts": restarts,
        }

    def _scrape_workload_metrics(self) -> dict[str, float]:
        if not self._metrics_url:
            return {}
        try:
            response = httpx.get(self._metrics_url, timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.debug("metrics scrape failed for %s", self._metrics_url)
            return {}
        raw = _parse_prometheus_gauges(response.text, set(_SCRAPED_GAUGE_NAMES))
        return {_SCRAPED_GAUGE_NAMES[name]: value for name, value in raw.items()}

    def _cpu_request_millicores(self, pods: list) -> float:
        total = 0.0
        for pod in pods:
            for container in pod.spec.containers:
                requests = (container.resources and container.resources.requests) or {}
                cpu_request = requests.get("cpu")
                if cpu_request:
                    total += _parse_cpu_millicores(cpu_request)
        return total

    def _pod_resource_usage(self, pods: list) -> tuple[float, float]:
        if not pods:
            return 0.0, 0.0

        try:
            metrics = self._custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", self._namespace, "pods"
            )
        except ApiException:
            logger.warning(
                "metrics.k8s.io unavailable in namespace %s (metrics-server not "
                "installed, or RBAC lacks read access) - reporting 0 usage",
                self._namespace,
            )
            return 0.0, 0.0

        usage_by_pod = {
            item["metadata"]["name"]: item["containers"]
            for item in metrics.get("items", [])
        }
        cpu_used_millicores = 0.0
        memory_used_bytes = 0.0
        for pod in pods:
            for container_metrics in usage_by_pod.get(pod.metadata.name, []):
                cpu_used_millicores += _parse_cpu_millicores(
                    container_metrics["usage"]["cpu"]
                )
                memory_used_bytes += _parse_memory_bytes(
                    container_metrics["usage"]["memory"]
                )

        cpu_requested_millicores = self._cpu_request_millicores(pods)
        cpu_usage_pct = (
            min(100.0, cpu_used_millicores / cpu_requested_millicores * 100)
            if cpu_requested_millicores
            else 0.0
        )
        memory_usage_mb = memory_used_bytes / (1024 * 1024)
        return round(cpu_usage_pct, 2), round(memory_usage_mb, 2)
