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
    PrometheusQueries,
    _parse_prometheus_gauges,
    _parse_quantity,
    _selector_matches,
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


def _deployment(
    namespace: str,
    name: str,
    labels: dict[str, str] | None = None,
    cpu_request: str = "100m",
    replicas: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name=name),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(
                match_labels=labels or {"app": name}, match_expressions=None
            ),
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[_container(cpu_request)])
            ),
        ),
        status=SimpleNamespace(replicas=replicas),
    )


def _container(cpu_request: str) -> SimpleNamespace:
    return SimpleNamespace(resources=SimpleNamespace(requests={"cpu": cpu_request}))


def _pod(
    name: str, labels: dict[str, str], cpu_request: str = "100m"
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        status=SimpleNamespace(phase="Running", container_statuses=[]),
        spec=SimpleNamespace(containers=[_container(cpu_request)]),
    )


def _usage(pod: str, cpu: str, memory: str = "64Mi") -> dict[str, object]:
    return {
        "metadata": {"name": pod},
        "containers": [{"usage": {"cpu": cpu, "memory": memory}}],
    }


def test_collect_lists_pods_and_metrics_once_per_namespace() -> None:
    collector, mock_apps, mock_core = _collector()
    mock_apps.list_namespaced_deployment.side_effect = [
        SimpleNamespace(
            items=[_deployment("payments", "api"), _deployment("payments", "worker")]
        ),
        SimpleNamespace(items=[_deployment("checkout", "api")]),
    ]
    mock_core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[
            _pod("api-1", {"app": "api"}),
            _pod("api-2", {"app": "api"}),
            _pod("worker-1", {"app": "worker"}),
        ]
    )
    collector._custom.list_namespaced_custom_object.return_value = {
        "items": [
            _usage("api-1", "50m"),
            _usage("api-2", "30m"),
            _usage("worker-1", "10m"),
        ]
    }

    result = collector.collect(("payments", "checkout"))

    assert result.targets == [
        KubernetesWorkloadTarget(namespace="checkout", deployment="api"),
        KubernetesWorkloadTarget(namespace="payments", deployment="api"),
        KubernetesWorkloadTarget(namespace="payments", deployment="worker"),
    ]
    assert mock_core.list_namespaced_pod.call_count == 2
    assert collector._custom.list_namespaced_custom_object.call_count == 2
    mock_apps.read_namespaced_deployment.assert_not_called()
    rows = {row["workload"]: row for row in result.rows}
    api = rows["payments:api"]
    assert api["cpu_usage_millicores"] == 80.0
    assert api["cpu_request_millicores"] == 100.0
    assert api["cpu_usage_pct"] == 40.0
    assert api["memory_usage_mb"] == 128.0
    assert rows["payments:worker"]["cpu_usage_millicores"] == 10.0


def test_collect_records_desired_replicas_from_spec() -> None:
    collector, mock_apps, mock_core = _collector()
    deployment = _deployment("payments", "api", replicas=3)
    deployment.spec.replicas = 6  # just scaled; status has not caught up
    mock_apps.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[deployment]
    )
    mock_core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    collector._custom.list_namespaced_custom_object.return_value = {"items": []}

    (row,) = collector.collect(("payments",)).rows

    assert row["replicas"] == 3
    assert row["desired_replicas"] == 6


def test_collect_wraps_deployment_list_transport_errors() -> None:
    collector, mock_apps, _ = _collector()
    mock_apps.list_namespaced_deployment.side_effect = Urllib3HTTPError(
        "connection refused"
    )

    with pytest.raises(KubernetesUnavailableError, match="could not list"):
        collector.collect(("payments",))


def test_collect_reports_malformed_metrics_as_a_namespace_error() -> None:
    collector, mock_apps, mock_core = _collector()
    mock_apps.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[_deployment("payments", "api")]
    )
    mock_core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[_pod("api-1", {"app": "api"})]
    )
    collector._custom.list_namespaced_custom_object.return_value = {
        "items": [_usage("api-1", "fast")]
    }

    result = collector.collect(("payments",))

    assert result.rows == []
    assert "malformed pod metrics" in result.errors[0]


def test_selector_matching_supports_labels_and_expressions() -> None:
    selector = SimpleNamespace(
        match_labels={"app": "api"},
        match_expressions=[
            SimpleNamespace(key="tier", operator="In", values=["web", "worker"]),
            SimpleNamespace(key="track", operator="NotIn", values=["canary"]),
            SimpleNamespace(key="ready", operator="Exists", values=None),
            SimpleNamespace(key="disabled", operator="DoesNotExist", values=None),
        ],
    )
    base = {"app": "api", "tier": "web", "ready": "true"}

    assert _selector_matches(selector, base)
    assert not _selector_matches(selector, {**base, "app": "other"})
    assert not _selector_matches(selector, {**base, "tier": "batch"})
    assert not _selector_matches(selector, {**base, "track": "canary"})
    assert not _selector_matches(selector, {"app": "api", "tier": "web"})
    assert not _selector_matches(selector, {**base, "disabled": "yes"})


def test_selector_matching_rejects_empty_selector() -> None:
    selector = SimpleNamespace(match_labels=None, match_expressions=None)

    with pytest.raises(KubernetesUnavailableError, match="no pod selector"):
        _selector_matches(selector, {"app": "api"})


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


class _FakePromResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._payload


def _prometheus_collector(
    deployments: list[SimpleNamespace],
    pods: list[SimpleNamespace],
    queries: PrometheusQueries | None = None,
) -> KubernetesObservationCollector:
    with (
        patch("scalescope.k8s_collector.load_k8s_config", return_value="kubeconfig"),
        patch("scalescope.k8s_collector.client") as mock_client,
    ):
        mock_client.AppsV1Api.return_value.list_namespaced_deployment.return_value = (
            SimpleNamespace(items=deployments)
        )
        mock_client.CoreV1Api.return_value.list_namespaced_pod.return_value = (
            SimpleNamespace(items=pods)
        )
        mock_client.CustomObjectsApi.return_value.list_namespaced_custom_object.return_value = {
            "items": []
        }
        return KubernetesObservationCollector(
            prometheus_url="http://prometheus:9090/",
            prometheus_queries=queries or PrometheusQueries(request_rate="rps"),
        )


def _series(namespace: str, pod: str, value: str) -> dict[str, object]:
    return {"metric": {"namespace": namespace, "pod": pod}, "value": [0, value]}


def test_one_prometheus_query_attributes_pod_rates_to_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float) -> object:
        calls.append((url, params))
        return _FakePromResponse(
            {
                "data": {
                    "result": [
                        _series("payments", "api-1", "12.5"),
                        _series("payments", "api-2", "2.5"),
                        _series("payments", "api-worker-1", "100"),
                        _series("payments", "api-3", "NaN"),
                    ]
                }
            }
        )

    monkeypatch.setattr("scalescope.k8s_collector.httpx.get", fake_get)
    collector = _prometheus_collector(
        [_deployment("payments", "api"), _deployment("payments", "api-worker")],
        [
            _pod("api-1", {"app": "api"}),
            _pod("api-2", {"app": "api"}),
            _pod("api-3", {"app": "api"}),
            _pod("api-worker-1", {"app": "api-worker"}),
        ],
    )

    rows = {row["workload"]: row for row in collector.collect(("payments",)).rows}

    assert calls == [
        (
            "http://prometheus:9090/api/v1/query",
            {"query": "rps"},
        )
    ]
    assert rows["payments:api"]["request_rate"] == 15.0
    assert rows["payments:api-worker"]["request_rate"] == 100.0


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"result": []}},
        {"data": {"result": [{"metric": {"pod": "api-1"}, "value": [0, "1"]}]}},
        {"error": "bad query"},
    ],
)
def test_request_rate_is_zero_without_a_matching_prometheus_series(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(
        "scalescope.k8s_collector.httpx.get",
        lambda url, params, timeout: _FakePromResponse(payload),
    )
    collector = _prometheus_collector(
        [_deployment("payments", "api")], [_pod("api-1", {"app": "api"})]
    )

    (row,) = collector.collect(("payments",)).rows

    assert row["request_rate"] == 0.0


def test_prometheus_signals_aggregate_per_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    per_query = {
        "rps": [("api-1", "10"), ("api-2", "30")],
        "throttled": [("api-1", "0.1"), ("api-2", "0.3")],
        "latency": [("api-1", "40"), ("api-2", "90")],
        "errors": [("api-1", "0.0"), ("api-2", "0.02")],
    }
    queried: list[str] = []

    def fake_get(url: str, params: dict[str, str], timeout: float) -> object:
        queried.append(params["query"])
        result = [_series("payments", pod, v) for pod, v in per_query[params["query"]]]
        return _FakePromResponse({"data": {"result": result}})

    monkeypatch.setattr("scalescope.k8s_collector.httpx.get", fake_get)
    collector = _prometheus_collector(
        [_deployment("payments", "api")],
        [_pod("api-1", {"app": "api"}), _pod("api-2", {"app": "api"})],
        PrometheusQueries(
            request_rate="rps",
            throttled_fraction="throttled",
            latency_p95_ms="latency",
            error_rate="errors",
        ),
    )

    (row,) = collector.collect(("payments",)).rows

    assert sorted(queried) == ["errors", "latency", "rps", "throttled"]
    assert row["request_rate"] == 40.0  # rates add up
    assert row["cpu_throttled_pct"] == 20.0  # fractions average
    assert row["latency_p95_ms"] == 90.0  # the slowest pod
    assert row["error_rate"] == 0.01


def test_several_series_for_one_pod_use_each_signals_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two containers in one pod, each with its own series.
    per_query = {"rps": ["10", "5"], "latency": ["40", "90"]}

    def fake_get(url: str, params: dict[str, str], timeout: float) -> object:
        values = per_query[params["query"]]
        result = [_series("payments", "api-1", value) for value in values]
        return _FakePromResponse({"data": {"result": result}})

    monkeypatch.setattr("scalescope.k8s_collector.httpx.get", fake_get)
    collector = _prometheus_collector(
        [_deployment("payments", "api")],
        [_pod("api-1", {"app": "api"})],
        PrometheusQueries(request_rate="rps", latency_p95_ms="latency"),
    )

    (row,) = collector.collect(("payments",)).rows

    assert row["request_rate"] == 15.0
    assert row["latency_p95_ms"] == 90.0  # not 130: latencies do not add


def test_empty_prometheus_queries_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    queried: list[str] = []

    def fake_get(url: str, params: dict[str, str], timeout: float) -> object:
        queried.append(params["query"])
        return _FakePromResponse({"data": {"result": []}})

    monkeypatch.setattr("scalescope.k8s_collector.httpx.get", fake_get)
    collector = _prometheus_collector(
        [_deployment("payments", "api")],
        [_pod("api-1", {"app": "api"})],
        PrometheusQueries(request_rate="rps", latency_p95_ms=""),
    )

    collector.collect(("payments",))

    assert queried == ["rps"]
