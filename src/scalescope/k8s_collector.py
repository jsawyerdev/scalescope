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

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

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


def _k8s_error_reason(exc: ApiException | Urllib3HTTPError) -> str:
    reason = getattr(exc, "reason", None)
    return str(reason) if reason else str(exc)


def _raise_unavailable(context: str, exc: ApiException | Urllib3HTTPError) -> NoReturn:
    raise KubernetesUnavailableError(f"{context}: {_k8s_error_reason(exc)}") from exc


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


def _label_selector(selector: object) -> str:
    match_labels = getattr(selector, "match_labels", None) or {}
    parts = [f"{key}={value}" for key, value in sorted(match_labels.items())]
    for expression in getattr(selector, "match_expressions", None) or []:
        key = expression.key
        operator = expression.operator
        values = expression.values or []
        if operator == "In":
            parts.append(f"{key} in ({','.join(values)})")
        elif operator == "NotIn":
            parts.append(f"{key} notin ({','.join(values)})")
        elif operator == "Exists":
            parts.append(key)
        elif operator == "DoesNotExist":
            parts.append(f"!{key}")
        else:
            raise KubernetesUnavailableError(
                f"unsupported Kubernetes label selector operator: {operator}"
            )
    if not parts:
        raise KubernetesUnavailableError("deployment has no pod selector")
    return ",".join(parts)


class KubernetesObservationCollector:
    """Collects observation rows for visible Kubernetes Deployments."""

    def __init__(
        self,
        kubeconfig_path: str | None = None,
        metrics_urls: dict[str, str] | None = None,
    ) -> None:
        self.auth_type = load_k8s_config(kubeconfig_path)
        self.auth_identity = (
            _service_account_subject()
            if self.auth_type == "in-cluster"
            else "kubeconfig"
        )
        self._metrics_urls = metrics_urls or {}
        self._apps = client.AppsV1Api()
        self._core = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()
        self.cluster_server = client.Configuration.get_default_copy().host

    def list_targets(
        self, namespaces: tuple[str, ...]
    ) -> list[KubernetesWorkloadTarget]:
        try:
            if namespaces == ("*",):
                deployments = self._apps.list_deployment_for_all_namespaces(
                    _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
                ).items
            else:
                deployments = []
                for namespace in namespaces:
                    deployments.extend(
                        self._apps.list_namespaced_deployment(
                            namespace,
                            _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
                        ).items
                    )
        except ApiException as exc:
            _raise_unavailable("could not list observable deployments", exc)
        except Urllib3HTTPError as exc:
            _raise_unavailable("could not list observable deployments", exc)

        return sorted(
            KubernetesWorkloadTarget(
                namespace=str(deployment.metadata.namespace),
                deployment=str(deployment.metadata.name),
            )
            for deployment in deployments
            if deployment.metadata.namespace and deployment.metadata.name
        )

    def collect(self, target: KubernetesWorkloadTarget) -> dict:
        try:
            deployment = self._apps.read_namespaced_deployment(
                target.deployment,
                target.namespace,
                _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
            )
        except ApiException as exc:
            _raise_unavailable(
                f"deployment {target.namespace}/{target.deployment} unreachable", exc
            )
        except Urllib3HTTPError as exc:
            _raise_unavailable(
                f"deployment {target.namespace}/{target.deployment} unreachable", exc
            )

        label_selector = _label_selector(deployment.spec.selector)
        try:
            pods = self._core.list_namespaced_pod(
                target.namespace,
                label_selector=label_selector,
                _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
            ).items
        except ApiException as exc:
            _raise_unavailable(
                f"pods for {target.namespace}/{target.deployment} unreachable", exc
            )
        except Urllib3HTTPError as exc:
            _raise_unavailable(
                f"pods for {target.namespace}/{target.deployment} unreachable", exc
            )

        pending_pods = sum(1 for p in pods if p.status.phase == "Pending")
        restarts = sum(
            cs.restart_count for p in pods for cs in (p.status.container_statuses or [])
        )
        cpu_usage_pct, memory_usage_mb = self._pod_resource_usage(target, pods)
        scraped = self._scrape_workload_metrics(
            self._metrics_urls.get(target.workload_id)
        )

        return {
            "ts": datetime.now(UTC),
            "workload": target.workload_id,
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

    def _cpu_request_millicores(self, pods: list) -> float:
        total = 0.0
        for pod in pods:
            for container in pod.spec.containers:
                resource_requests = (
                    container.resources and container.resources.requests
                ) or {}
                cpu_request = resource_requests.get("cpu")
                if cpu_request:
                    total += _parse_cpu_millicores(cpu_request)
        return total

    def _pod_resource_usage(
        self, target: KubernetesWorkloadTarget, pods: list
    ) -> tuple[float, float]:
        if not pods:
            return 0.0, 0.0

        try:
            metrics = self._custom.list_namespaced_custom_object(
                "metrics.k8s.io",
                "v1beta1",
                target.namespace,
                "pods",
                _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
            )
        except ApiException:
            logger.warning(
                "metrics.k8s.io unavailable in namespace %s (metrics-server not "
                "installed, or RBAC lacks read access) - reporting 0 usage",
                target.namespace,
            )
            return 0.0, 0.0
        except Urllib3HTTPError as exc:
            _raise_unavailable(
                f"pod metrics for {target.namespace}/{target.deployment} unreachable",
                exc,
            )

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
