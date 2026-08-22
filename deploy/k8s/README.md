# Kubernetes Templates

This directory is a deployment placeholder for future Kubernetes work. It is not a production-ready
manifest set yet.

Layout:

- `base/` contains the production-shaped application services: `agent-api` and `usage-admin`.
- `local/` extends `base/` with Mock MES, PostgreSQL, and Redis for local or integration clusters.

Secrets are intentionally represented by `base/secrets.example.yaml`. Copy that file outside source
control or replace it with your cluster's secret manager before applying any manifests.

Examples:

```bash
kubectl apply -k deploy/k8s/base
kubectl apply -k deploy/k8s/local
```

Open items before real Kubernetes deployment:

- ingress, TLS, DNS, and allowed outbound hosts
- production secret source
- persistent volume class and backup policy
- migrations and rollout order
- resource requests and limits from measured workloads
- network policies and service account permissions
- observability, log redaction, and retention settings
