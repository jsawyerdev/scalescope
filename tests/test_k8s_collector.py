"""Unit tests for OBSERVE-mode Kubernetes collection boundaries."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from scalescope.k8s_collector import (
    KubernetesObservationCollector,
    KubernetesUnavailableError,
    KubernetesWorkloadTarget,
    _label_selector,
    _parse_prometheus_gauges,
    _parse_quantity,
    _service_account_subject,
)

_TARGET = KubernetesWorkloadTarget(
    namespace="scalescope-demo", deployment="sample-workload"
)


def _collector() -> tuple[KubernetesObservationCollector, MagicMock, MagicMock]:
    with (
        patch("scalescope.k8s_collector.load_k8s_config", return_value="in-cluster"),
        patch(
            "scalescope.k8s_collector._service_account_subject",
            return_value="system:serviceaccount:scalescope-system:scalescope",
        ),
        patch("scalescope.k8s_collector.client") as mock_client,
    ):
        mock_apps = MagicMock()
        mock_core = MagicMock()
        mock_custom = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.CoreV1Api.return_value = mock_core
        mock_client.CustomObjectsApi.return_value = mock_custom
        mock_client.Configuration.get_default_copy.return_value.host = "https://cluster"
        collector = KubernetesObservationCollector(kubeconfig_path=None)
    return collector, mock_apps, mock_core


def test_collector_reports_cluster_auth_identity() -> None:
    collector, _, _ = _collector()

    assert collector.auth_type == "in-cluster"
    assert (
        collector.auth_identity == "system:serviceaccount:scalescope-system:scalescope"
    )


def test_service_account_subject_decodes_projected_token(tmp_path: Path) -> None:
    subject = "system:serviceaccount:scalescope-system:scalescope"
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    token_path = tmp_path / "token"
    token_path.write_text(f"header.{payload}.signature", encoding="utf-8")

    assert _service_account_subject(token_path) == subject


def _deployment(namespace: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name=name),
    )


def test_list_targets_returns_sorted_namespace_deployment_ids() -> None:
    collector, mock_apps, _ = _collector()
    mock_apps.list_namespaced_deployment.side_effect = [
        SimpleNamespace(items=[_deployment("payments", "api")]),
        SimpleNamespace(items=[_deployment("checkout", "api")]),
    ]

    targets = collector.list_targets(("payments", "checkout"))

    assert targets == [
        KubernetesWorkloadTarget(namespace="checkout", deployment="api"),
        KubernetesWorkloadTarget(namespace="payments", deployment="api"),
    ]


def test_list_targets_wraps_kubernetes_transport_errors() -> None:
    collector, mock_apps, _ = _collector()
    mock_apps.list_namespaced_deployment.side_effect = Urllib3HTTPError(
        "connection refused"
    )

    with pytest.raises(KubernetesUnavailableError, match="could not list"):
        collector.list_targets(("payments",))


def test_collect_wraps_deployment_transport_errors() -> None:
    collector, mock_apps, _ = _collector()
    mock_apps.read_namespaced_deployment.side_effect = Urllib3HTTPError(
        "connection refused"
    )

    with pytest.raises(KubernetesUnavailableError, match="sample-workload"):
        collector.collect(_TARGET)


def test_label_selector_supports_match_labels_and_expressions() -> None:
    selector = SimpleNamespace(
        match_labels={"app": "api"},
        match_expressions=[
            SimpleNamespace(key="tier", operator="In", values=["web", "worker"]),
            SimpleNamespace(key="track", operator="NotIn", values=["canary"]),
            SimpleNamespace(key="ready", operator="Exists", values=None),
            SimpleNamespace(key="disabled", operator="DoesNotExist", values=None),
        ],
    )

    assert (
        _label_selector(selector)
        == "app=api,tier in (web,worker),track notin (canary),ready,!disabled"
    )


def test_label_selector_rejects_empty_selector() -> None:
    selector = SimpleNamespace(match_labels=None, match_expressions=None)

    with pytest.raises(KubernetesUnavailableError, match="no pod selector"):
        _label_selector(selector)


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        ("250m", 0.25),
        ("100n", 1e-7),
        ("5u", 5e-6),
        ("2", 2.0),
        ("1.5", 1.5),
        ("2k", 2000.0),
        ("3M", 3e6),
        ("64Mi", 64 * 1024**2),
        ("1Gi", 1024**3),
        ("1Ei", 1024**6),
    ],
)
def test_parse_quantity_handles_kubernetes_suffixes(
    quantity: str, expected: float
) -> None:
    assert _parse_quantity(quantity) == pytest.approx(expected)


def test_prometheus_gauges_drop_non_finite_values() -> None:
    names = {"a", "b", "c", "d"}
    text = "a 1.5\nb NaN\nc +Inf\nd -Inf\n"

    assert _parse_prometheus_gauges(text, names) == {"a": 1.5}


def _pod(name: str, cpu_request: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    resources=SimpleNamespace(requests={"cpu": cpu_request})
                )
            ]
        ),
    )


def _collector_with_pod_metrics(
    pod_metrics: dict[str, object],
) -> KubernetesObservationCollector:
    collector, mock_apps, mock_core = _collector()
    mock_apps.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(
            selector=SimpleNamespace(
                match_labels={"app": "sample-workload"}, match_expressions=None
            )
        ),
        status=SimpleNamespace(replicas=1),
    )
    mock_core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[_pod("pod-a", "100m")]
    )
    collector._custom.list_namespaced_custom_object.return_value = pod_metrics
    return collector


def test_collect_reports_usage_relative_to_cpu_request() -> None:
    collector = _collector_with_pod_metrics(
        {
            "items": [
                {
                    "metadata": {"name": "pod-a"},
                    "containers": [{"usage": {"cpu": "50m", "memory": "64Mi"}}],
                }
            ]
        }
    )

    row = collector.collect(_TARGET)

    assert row["cpu_usage_pct"] == 50.0
    assert row["memory_usage_mb"] == 64.0


def test_collect_wraps_malformed_pod_metrics() -> None:
    collector = _collector_with_pod_metrics(
        {
            "items": [
                {
                    "metadata": {"name": "pod-a"},
                    "containers": [{"usage": {"cpu": "fast", "memory": "64Mi"}}],
                }
            ]
        }
    )

    with pytest.raises(KubernetesUnavailableError, match="malformed pod metrics"):
        collector.collect(_TARGET)
