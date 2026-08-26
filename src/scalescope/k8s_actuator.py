"""Write path for OBSERVE mode: applies a recommended replica count.

Deliberately separate from k8s_collector.py (read-only) so the one place
in the codebase capable of mutating the cluster is small, obvious, and
easy to audit. Requires `SCALESCOPE_ACTUATE=true` in addition to
`SCALESCOPE_MODE=observe` - actuation is opt-in even when observing.

Refuses to scale a Deployment that already has a HorizontalPodAutoscaler
targeting it. Two controllers writing the same Deployment's replica count
fight each other - the HPA reconciles continuously and will overwrite
ScaleScope's write within seconds, producing visible flapping rather than
either signal actually controlling the workload. There is no safe
default here: this must be an explicit precondition, not a warning.
"""

from __future__ import annotations

import logging

from kubernetes import client
from kubernetes.client.rest import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from scalescope.k8s_collector import K8S_REQUEST_TIMEOUT_SECONDS, load_k8s_config

logger = logging.getLogger(__name__)


class HpaConflictError(RuntimeError):
    """Raised when a Deployment already has a competing HorizontalPodAutoscaler."""


class ActuationError(RuntimeError):
    """Raised when the scale write itself fails."""


class KubernetesActuator:
    def __init__(self, namespace: str, kubeconfig_path: str | None = None) -> None:
        load_k8s_config(kubeconfig_path)
        self._namespace = namespace
        self._apps = client.AppsV1Api()
        self._autoscaling = client.AutoscalingV2Api()

    def _competing_hpa_name(self, deployment_name: str) -> str | None:
        try:
            hpas = self._autoscaling.list_namespaced_horizontal_pod_autoscaler(
                self._namespace, _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS
            ).items
        except ApiException as exc:
            raise ActuationError(
                f"could not list HorizontalPodAutoscalers in {self._namespace}: {exc.reason}"
            ) from exc
        except Urllib3HTTPError as exc:
            raise ActuationError(
                f"could not list HorizontalPodAutoscalers in {self._namespace}: {exc}"
            ) from exc
        for hpa in hpas:
            target = hpa.spec.scale_target_ref
            if target.kind == "Deployment" and target.name == deployment_name:
                return str(hpa.metadata.name)
        return None

    def scale(self, deployment_name: str, replicas: int) -> None:
        """Set `deployment_name`'s replica count. Raises on any precondition failure."""
        conflicting_hpa = self._competing_hpa_name(deployment_name)
        if conflicting_hpa is not None:
            raise HpaConflictError(
                f"deployment {self._namespace}/{deployment_name} is already targeted by "
                f"HorizontalPodAutoscaler {conflicting_hpa}; refusing to write "
                "spec.replicas to avoid fighting it"
            )

        try:
            self._apps.patch_namespaced_deployment_scale(
                deployment_name,
                self._namespace,
                body={"spec": {"replicas": replicas}},
                _request_timeout=K8S_REQUEST_TIMEOUT_SECONDS,
            )
        except ApiException as exc:
            raise ActuationError(
                f"failed to scale {self._namespace}/{deployment_name} to {replicas}: {exc.reason}"
            ) from exc
        except Urllib3HTTPError as exc:
            raise ActuationError(
                f"failed to scale {self._namespace}/{deployment_name} to {replicas}: {exc}"
            ) from exc
        logger.info(
            "scaled %s/%s to %d replicas", self._namespace, deployment_name, replicas
        )
