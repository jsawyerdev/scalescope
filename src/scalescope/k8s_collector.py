"""Read-only Kubernetes telemetry collector for OBSERVE mode.

Populates the same observation schema DEMO mode's simulator produces, from
a real Deployment's Pod/metrics.k8s.io state. Uses only get/list verbs on
apps/v1 Deployments, core/v1 Pods, and (if metrics-server is installed)
metrics.k8s.io PodMetrics (the ClusterRole in k8s/scalescope/, or the
namespace Role in k8s/rbac/). Never writes to the cluster.

Request rate, p95 latency, error rate, and CPU throttling are not
derivable from the Kubernetes API or metrics-server. The first three come
from the workload's own Prometheus `/metrics` when `metrics_url` is
configured (the `sample_workload_*` gauges in `sample-workload/app/main.py`
define the contract); any of the four can come from a Prometheus server,
one per-pod query per signal each tick (`PrometheusQueries`). A signal
with no source is reported as 0.0 rather than a fabricated value.
`restarts` is the pods' cumulative container restart count.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import httpx
from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

logger = logging.getLogger(__name__)

# The kubernetes client sets no timeout by default (Configuration.retries
# is None, no socket timeout configured) - an unresponsive API server would
# otherwise hang the calling thread indefinitely. Every API call in this
# module and k8s_actuator.py passes this explicitly.
K8S_REQUEST_TIMEOUT_SECONDS = 10

_METRIC_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9.eE+\-]+|NaN|\+Inf|-Inf)\s*$"
)
_SCRAPED_GAUGE_NAMES = {
    "sample_workload_demand_rps": "request_rate",
    "sample_workload_latency_p95_ms": "latency_p95_ms",
    "sample_workload_error_rate": "error_rate",
}
_SERVICE_ACCOUNT_TOKEN_PATH = Path(
    "/var/run/secrets/kubernetes.io/serviceaccount/token"
)


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
            value = float(match.group(2))
        except ValueError:
            continue
        # NaN/Inf would poison every forecast over the stored history window;
        # treat them like an absent gauge.
        if math.isfinite(value):
            values[match.group(1)] = value
    return values


# Kubernetes resource.Quantity suffixes. Binary suffixes are two characters
# and must be matched before the one-character decimal ones.
_BINARY_QUANTITY_SUFFIXES = {
    "Ki": 1024.0,
    "Mi": 1024.0**2,
    "Gi": 1024.0**3,
    "Ti": 1024.0**4,
    "Pi": 1024.0**5,
    "Ei": 1024.0**6,
}
_DECIMAL_QUANTITY_SUFFIXES = {
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}


class KubernetesUnavailableError(RuntimeError):
    """Raised when the configured cluster/deployment cannot be reached."""


@dataclass(frozen=True, order=True)
class KubernetesWorkloadTarget:
    """A Deployment selected for observation."""

    namespace: str
    deployment: str

    @property
    def workload_id(self) -> str:
        return workload_id(self.namespace, self.deployment)

    def to_dict(self, metrics_url_configured: bool) -> dict[str, str | bool]:
        return {
            "id": self.workload_id,
            "namespace": self.namespace,
            "deployment": self.deployment,
            "metrics_url_configured": metrics_url_configured,
        }


def workload_id(namespace: str, deployment: str) -> str:
    return f"{namespace}:{deployment}"


def load_k8s_config(kubeconfig_path: str | None) -> str:
    """Load in-cluster config when running as a Pod, else a kubeconfig file."""
    try:
        k8s_config.load_incluster_config()
        return "in-cluster"
    except k8s_config.ConfigException:
        k8s_config.load_kube_config(config_file=kubeconfig_path)
        return "kubeconfig"


def _service_account_subject(
    token_path: Path = _SERVICE_ACCOUNT_TOKEN_PATH,
) -> str | None:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        payload_part = token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part).decode("utf-8"))
    except IndexError, OSError, UnicodeDecodeError, ValueError, binascii.Error:
        return None

    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def k8s_error_reason(exc: ApiException | Urllib3HTTPError) -> str:
    """The most specific human-readable reason a Kubernetes API call failed."""
    reason = getattr(exc, "reason", None)
    return str(reason) if reason else str(exc)


def _raise_unavailable(context: str, exc: ApiException | Urllib3HTTPError) -> NoReturn:
    raise KubernetesUnavailableError(f"{context}: {k8s_error_reason(exc)}") from exc


def _parse_quantity(value: str) -> float:
    """Parse a Kubernetes resource quantity (e.g. `250m`, `1.5Gi`, `2k`).

    Raises ValueError for strings that are not a valid quantity.
    """
    for suffixes in (_BINARY_QUANTITY_SUFFIXES, _DECIMAL_QUANTITY_SUFFIXES):
        for suffix, multiplier in suffixes.items():
            if value.endswith(suffix):
                return float(value[: -len(suffix)]) * multiplier
    return float(value)


def _parse_cpu_millicores(value: str) -> float:
    return _parse_quantity(value) * 1000


def _cpu_request_millicores(containers: list[Any]) -> float:
    total = 0.0
    for container in containers:
        resource_requests = (container.resources and container.resources.requests) or {}
        cpu_request = resource_requests.get("cpu")
        if cpu_request:
            total += _parse_cpu_millicores(cpu_request)
    return total


def _selector_matches(selector: object, labels: dict[str, str]) -> bool:
    """Client-side `LabelSelector` match, so one pod list serves a namespace.

    Raises KubernetesUnavailableError for an empty selector or an operator
    outside the four Kubernetes defines.
    """
    match_labels = getattr(selector, "match_labels", None) or {}
    expressions = getattr(selector, "match_expressions", None) or []
    if not match_labels and not expressions:
        raise KubernetesUnavailableError("deployment has no pod selector")
    if any(labels.get(key) != value for key, value in match_labels.items()):
        return False
    for expression in expressions:
        key, operator = expression.key, expression.operator
        values = expression.values or []
        if operator == "In":
            matched = labels.get(key) in values
        elif operator == "NotIn":
            matched = labels.get(key) not in values
        elif operator == "Exists":
            matched = key in labels
        elif operator == "DoesNotExist":
            matched = key not in labels
        else:
            raise KubernetesUnavailableError(
                f"unsupported Kubernetes label selector operator: {operator}"
            )
        if not matched:
            return False
    return True


@dataclass(frozen=True)
class PrometheusQueries:
    """Instant queries returning one series per pod, labelled `namespace` and `pod`.

    An empty query is skipped. Values: requests/s, CPU-throttled fraction of
    periods (0-1), p95 latency in ms, and error fraction of requests (0-1).
    """

    request_rate: str = ""
    throttled_fraction: str = ""
    latency_p95_ms: str = ""
    error_rate: str = ""


# Every series value per (namespace, pod). A query may return several series
# for one pod (one per container, say); each signal's own rule in
# `_observe` aggregates them, since summing is wrong for latency or ratios.
PodSeries = dict[tuple[str, str], list[float]]


def _pod_values(
    series: PodSeries | None, namespace: str, pods: list[Any]
) -> list[float]:
    if not series:
        return []
    keys = ((namespace, pod.metadata.name) for pod in pods)
    return [value for key in keys for value in series.get(key, [])]


@dataclass(frozen=True)
class CollectionResult:
    """One tick's rows, plus the targets seen and any per-target failures."""

    targets: list[KubernetesWorkloadTarget]
    rows: list[dict[str, Any]]
    errors: list[str]


class KubernetesObservationCollector:
    """Collects observation rows for visible Kubernetes Deployments.

    Each tick costs one Deployment list, plus one Pod list and one PodMetrics
    list per namespace that has Deployments, plus one query per configured
    Prometheus signal, however many Deployments there are.
    """

    def __init__(
        self,
        kubeconfig_path: str | None = None,
        metrics_urls: dict[str, str] | None = None,
        prometheus_url: str | None = None,
        prometheus_queries: PrometheusQueries | None = None,
    ) -> None:
        self.auth_type = load_k8s_config(kubeconfig_path)
        self.auth_identity = (
            _service_account_subject()
            if self.auth_type == "in-cluster"
            else "kubeconfig"
        )
        self._metrics_urls = metrics_urls or {}
        self._prometheus_url = prometheus_url.rstrip("/") if prometheus_url else None
        self._prometheus_queries = prometheus_queries or PrometheusQueries()
        self._failing_queries: set[str] = set()
        self._apps = client.AppsV1Api()
        self._core = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()
        self.cluster_server = client.Configuration.get_default_copy().host

    def collect(self, namespaces: tuple[str, ...]) -> CollectionResult:
        """Observe every Deployment in `namespaces` (`("*",)` for all).

        Raises KubernetesUnavailableError if Deployments cannot be listed;
        failures confined to one namespace or Deployment are returned in
        `errors` instead.
        """
        deployments = [
            deployment
            for deployment in self._list_deployments(namespaces)
            if deployment.metadata.namespace and deployment.metadata.name
        ]
        by_namespace: dict[str, list[Any]] = {}
        for deployment in deployments:
            by_namespace.setdefault(str(deployment.metadata.namespace), []).append(
                deployment
            )

        targets = sorted(
            KubernetesWorkloadTarget(
                namespace=str(deployment.metadata.namespace),
                deployment=str(deployment.metadata.name),
            )
            for deployment in deployments
        )
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        pod_series = self._prometheus_pod_series()
        for namespace, namespace_deployments in sorted(by_namespace.items()):
            try:
                pods = self._list_pods(namespace)
                usage_by_pod = self._pod_usage(namespace)
            except KubernetesUnavailableError as exc:
                errors.append(str(exc))
                continue
            for deployment in namespace_deployments:
                try:
                    rows.append(
                        self._observe(deployment, pods, usage_by_pod, pod_series)
                    )
                except KubernetesUnavailableError as exc:
                    errors.append(f"{namespace}/{deployment.metadata.name}: {exc}")
        return CollectionResult(targets=targets, rows=rows, errors=errors)

    def _list_deployments(self, namespaces: tuple[str, ...]) -> list[Any]:
        try:
            if namespaces == ("*",):
                return list(
                    self._apps.list_deployment_for_all_namespaces(
                        _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
                    ).items
                )
            deployments: list[Any] = []
            for namespace in namespaces:
                deployments.extend(
                    self._apps.list_namespaced_deployment(
                        namespace,
                        _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
                    ).items
                )
            return deployments
        except (ApiException, Urllib3HTTPError) as exc:
            _raise_unavailable("could not list observable deployments", exc)

    def _list_pods(self, namespace: str) -> list[Any]:
        try:
            return list(
                self._core.list_namespaced_pod(
                    namespace, _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS
                ).items
            )
        except (ApiException, Urllib3HTTPError) as exc:
            _raise_unavailable(f"pods in {namespace} unreachable", exc)

    def _pod_usage(self, namespace: str) -> dict[str, tuple[float, float]]:
        """(CPU millicores, memory bytes) per pod; empty without metrics-server."""
        try:
            metrics = self._custom.list_namespaced_custom_object(
                "metrics.k8s.io",
                "v1beta1",
                namespace,
                "pods",
                _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
            )
        except ApiException:
            logger.warning(
                "metrics.k8s.io unavailable in namespace %s (metrics-server not "
                "installed, or RBAC lacks read access) - reporting 0 usage",
                namespace,
            )
            return {}
        except Urllib3HTTPError as exc:
            _raise_unavailable(f"pod metrics in {namespace} unreachable", exc)

        try:
            usage: dict[str, tuple[float, float]] = {}
            for item in metrics.get("items", []):
                cpu = memory = 0.0
                for container in item["containers"]:
                    cpu += _parse_cpu_millicores(container["usage"]["cpu"])
                    memory += _parse_quantity(container["usage"]["memory"])
                usage[item["metadata"]["name"]] = (cpu, memory)
        except (KeyError, TypeError, ValueError) as exc:
            raise KubernetesUnavailableError(
                f"malformed pod metrics in {namespace}: {exc!r}"
            ) from exc
        return usage

    def _observe(
        self,
        deployment: Any,
        namespace_pods: list[Any],
        usage_by_pod: dict[str, tuple[float, float]],
        pod_series: dict[str, PodSeries],
    ) -> dict[str, Any]:
        target = KubernetesWorkloadTarget(
            namespace=str(deployment.metadata.namespace),
            deployment=str(deployment.metadata.name),
        )
        selector = deployment.spec.selector
        pods = [
            pod
            for pod in namespace_pods
            if _selector_matches(selector, pod.metadata.labels or {})
        ]
        try:
            cpu_request_per_pod = _cpu_request_millicores(
                deployment.spec.template.spec.containers
            )
            cpu_requested = sum(
                _cpu_request_millicores(pod.spec.containers) for pod in pods
            )
        except (TypeError, ValueError) as exc:
            raise KubernetesUnavailableError(
                f"unparseable CPU request: {exc!r}"
            ) from exc

        usage = [usage_by_pod.get(pod.metadata.name, (0.0, 0.0)) for pod in pods]
        cpu_used = sum(cpu for cpu, _ in usage)
        memory_used = sum(memory for _, memory in usage)
        cpu_usage_pct = (
            min(100.0, cpu_used / cpu_requested * 100) if cpu_requested else 0.0
        )
        scraped = self._scrape_workload_metrics(
            self._metrics_urls.get(target.workload_id)
        )

        def per_pod(signal: str) -> list[float]:
            return _pod_values(pod_series.get(signal), target.namespace, pods)

        # The workload's own /metrics wins; Prometheus fills the rest.
        # Rates add across pods; fractions average; latency takes the worst.
        rates, throttled = per_pod("request_rate"), per_pod("throttled_fraction")
        latencies, errors = per_pod("latency_p95_ms"), per_pod("error_rate")
        request_rate = scraped.get("request_rate", sum(rates) if rates else None)
        latency = scraped.get("latency_p95_ms", max(latencies) if latencies else 0.0)
        error_rate = scraped.get(
            "error_rate", sum(errors) / len(errors) if errors else 0.0
        )
        throttled_pct = 100 * sum(throttled) / len(throttled) if throttled else 0.0

        replicas = deployment.status.replicas or 0
        desired = deployment.spec.replicas
        return {
            "ts": datetime.now(UTC),
            "workload": target.workload_id,
            "replicas": replicas,
            "desired_replicas": replicas if desired is None else desired,
            "request_rate": request_rate if request_rate is not None else 0.0,
            "cpu_usage_pct": round(cpu_usage_pct, 2),
            "cpu_usage_millicores": round(cpu_used, 1),
            "cpu_request_millicores": round(cpu_request_per_pod, 1),
            "cpu_throttled_pct": round(throttled_pct, 2),
            "memory_usage_mb": round(memory_used / (1024 * 1024), 2),
            "latency_p95_ms": round(latency, 2),
            "error_rate": round(error_rate, 4),
            "pending_pods": sum(1 for pod in pods if pod.status.phase == "Pending"),
            "restarts": sum(
                status.restart_count
                for pod in pods
                for status in (pod.status.container_statuses or [])
            ),
        }

    def _scrape_workload_metrics(self, metrics_url: str | None) -> dict[str, float]:
        if not metrics_url:
            return {}
        try:
            response = httpx.get(metrics_url, timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.debug("metrics scrape failed for %s", metrics_url)
            return {}
        raw = _parse_prometheus_gauges(response.text, set(_SCRAPED_GAUGE_NAMES))
        return {_SCRAPED_GAUGE_NAMES[name]: value for name, value in raw.items()}

    def _prometheus_pod_series(self) -> dict[str, PodSeries]:
        """Every configured per-pod signal for this tick, keyed by signal name."""
        if self._prometheus_url is None:
            return {}
        queries = vars(self._prometheus_queries)
        series: dict[str, PodSeries] = {}
        for signal, query in queries.items():
            if not query:
                continue
            values = self._prometheus_query(query)
            if values is None:
                if signal not in self._failing_queries:
                    logger.warning(
                        "prometheus %s query failed or returned series without "
                        "namespace and pod labels",
                        signal,
                    )
                    self._failing_queries.add(signal)
                continue
            if signal in self._failing_queries:
                logger.info("prometheus %s query recovered", signal)
                self._failing_queries.discard(signal)
            series[signal] = values
        return series

    def _prometheus_query(self, query: str) -> PodSeries | None:
        try:
            response = httpx.get(
                f"{self._prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=5.0,
            )
            response.raise_for_status()
            values: PodSeries = {}
            for series in response.json()["data"]["result"]:
                labels = series["metric"]
                value = float(series["value"][1])
                if math.isfinite(value):
                    key = (labels["namespace"], labels["pod"])
                    values.setdefault(key, []).append(value)
        except httpx.HTTPError, KeyError, TypeError, ValueError, IndexError:
            return None
        return values
