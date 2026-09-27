# Security policy

## Supported versions

Security fixes go into the latest release. Upgrade to it before reporting
an issue you found on an older version.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue or pull
request: use **Report a vulnerability** on the repository's
[Security tab](https://github.com/jsawyerdev/scalescope/security/advisories/new).

Include what an attacker can do, the affected version and mode (DEMO,
OBSERVE, OBSERVE with actuation), and steps to reproduce. You should get an
acknowledgement within a week. Once a fix is released, the advisory is
published with credit to you unless you prefer otherwise.

## What ScaleScope trusts

Knowing the intended boundaries helps tell a vulnerability from a
deployment choice:

- **Authentication is opt-in.** Without `SCALESCOPE_AUTH_USERNAME` and
  `SCALESCOPE_AUTH_PASSWORD`, every route is open by design, for trusted
  networks. The Kubernetes Service is `ClusterIP`. Exposing ScaleScope
  without auth, TLS, or a gateway in front is a deployment risk, not a
  vulnerability; see "Release security model" in the README.
- **Cluster access is read-only by default.** The observer ClusterRole
  allows only `get`/`list` on Deployments, Pods, and PodMetrics. Writing
  replica counts requires both `k8s/scalescope-actuation/` RBAC and
  `SCALESCOPE_ACTUATE=true`, and only ever touches the `deployments/scale`
  subresource.
- **Configured endpoints are trusted as far as reaching them.**
  `SCALESCOPE_K8S_METRICS_URL` and `SCALESCOPE_PROMETHEUS_URL` are set by
  the operator. Their responses are treated as untrusted input: parsed
  defensively, and never allowed to overwrite what ScaleScope itself
  reports.

Anything that lets a caller bypass these (reading without credentials when
auth is on, writing to the cluster without actuation enabled, injecting
content into the dashboard) is in scope.
