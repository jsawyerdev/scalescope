"""Unit tests for the write path: HPA-conflict refusal and the scale call.

Mocks the kubernetes client entirely - these must never touch a real
cluster. The one behavior that matters most here (refuse to write when a
competing HPA exists) is exactly the failure mode that would otherwise be
invisible until two controllers were already fighting in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scalescope.k8s_actuator import ActuationError, HpaConflictError, KubernetesActuator


def _make_actuator() -> tuple[KubernetesActuator, MagicMock, MagicMock]:
    with (
        patch("scalescope.k8s_actuator.load_k8s_config"),
        patch("scalescope.k8s_actuator.client") as mock_client,
    ):
        mock_apps = MagicMock()
        mock_autoscaling = MagicMock()
        mock_client.AppsV1Api.return_value = mock_apps
        mock_client.AutoscalingV2Api.return_value = mock_autoscaling
        actuator = KubernetesActuator("ns", kubeconfig_path=None)
    return actuator, mock_apps, mock_autoscaling


def _hpa(name: str, target_kind: str, target_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            scale_target_ref=SimpleNamespace(kind=target_kind, name=target_name)
        ),
    )


def test_scale_refuses_when_competing_hpa_targets_same_deployment():
    actuator, mock_apps, mock_autoscaling = _make_actuator()
    mock_autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        SimpleNamespace(items=[_hpa("existing-hpa", "Deployment", "sample-workload")])
    )

    with pytest.raises(HpaConflictError, match="existing-hpa"):
        actuator.scale("sample-workload", 5)

    mock_apps.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_ignores_hpa_targeting_a_different_deployment():
    actuator, mock_apps, mock_autoscaling = _make_actuator()
    mock_autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        SimpleNamespace(items=[_hpa("other-hpa", "Deployment", "unrelated-deployment")])
    )

    actuator.scale("sample-workload", 5)

    mock_apps.patch_namespaced_deployment_scale.assert_called_once_with(
        "sample-workload", "ns", body={"spec": {"replicas": 5}}
    )


def test_scale_writes_when_no_hpa_present():
    actuator, mock_apps, mock_autoscaling = _make_actuator()
    mock_autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        SimpleNamespace(items=[])
    )

    actuator.scale("sample-workload", 7)

    mock_apps.patch_namespaced_deployment_scale.assert_called_once_with(
        "sample-workload", "ns", body={"spec": {"replicas": 7}}
    )


def test_scale_wraps_api_failure_as_actuation_error():
    from kubernetes.client.rest import ApiException

    actuator, mock_apps, mock_autoscaling = _make_actuator()
    mock_autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        SimpleNamespace(items=[])
    )
    mock_apps.patch_namespaced_deployment_scale.side_effect = ApiException(
        reason="Forbidden"
    )

    with pytest.raises(ActuationError, match="Forbidden"):
        actuator.scale("sample-workload", 3)
