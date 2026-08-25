# Runbook: epik8s-platform takeover (replace epik8s-backend + standalone grafana)

Goal: move to one platform owner so epik8s-platform is the single GitOps layer for cluster setup required by EPIK8S.

Scope of this runbook:
- Backend services currently tracked by ArgoCD app epik8s-backend (Elasticsearch, MongoDB, Kafka)
- Standalone Helm releases in monitoring (grafana, prometheus)
- Existing cluster-scoped platform objects (networking/storage/logging/backup)

Out of scope:
- Beamline namespace services deployed by epik8s-chart
- Any destructive data migration (PVC recreation, DB reinitialization)

## Preconditions

- You have cluster-admin access.
- The epik8s-platform chart renders with values-k8sda.yaml and matches live intent.
- You can run kubectl, helm, and argocd CLI from your workstation.

Recommended maintenance posture:
- Freeze manual changes during cutover.
- Disable auto-sync temporarily on involved ArgoCD apps.

Cluster-specific discovery (k8sda, verified 2026-08-25):
- `epik8s-backend` exists and is active.
- `epik8s-platform-librechat` exists, but it points to `argus-helm-chart` and is NOT the full platform app.
- No ArgoCD Application currently points to the `epik8s-platform` repository/chart for full platform ownership.

Set these variables once per session:

  export APP_BACKEND_OLD=epik8s-backend
  export APP_PLATFORM_CORE=epik8s-platform-core
  export APP_PLATFORM_GRAFANA=grafana
  export APP_PLATFORM_PROMETHEUS=prometheus
  export APP_PLATFORM_BACKEND=epik8s-platform-backend
  export APP_PLATFORM_AI=epik8s-platform-ai

## Domain split (recommended)

Use one ArgoCD Application per platform domain, each with an explicit destination
namespace and explicit feature toggles.

Domain matrix:
- `APP_PLATFORM_CORE` -> destination `kube-system` (cluster-wide/core objects)
- `APP_PLATFORM_GRAFANA` -> destination `monitoring` (Grafana only)
- `APP_PLATFORM_PROMETHEUS` -> destination `monitoring` (Prometheus stack only)
- `APP_PLATFORM_BACKEND` -> destination `backend` (Elasticsearch/MongoDB/Kafka)
- `APP_PLATFORM_AI` -> destination `ai-platform` (optional AI platform domain)

Create missing apps (SSH repo URL):

  argocd app create "$APP_PLATFORM_CORE" \
    --repo git@github.com:infn-epics/epik8s-platform.git \
    --path . \
    --revision main \
    --dest-server https://kubernetes.default.svc \
    --dest-namespace kube-system \
    --values values-k8sda.yaml \
    --values values-domain-core.yaml

  argocd app create "$APP_PLATFORM_GRAFANA" \
    --repo git@github.com:infn-epics/epik8s-platform.git \
    --path . \
    --revision main \
    --dest-server https://kubernetes.default.svc \
    --dest-namespace monitoring \
    --values values-k8sda.yaml \
    --values values-domain-grafana.yaml

  argocd app create "$APP_PLATFORM_PROMETHEUS" \
    --repo git@github.com:infn-epics/epik8s-platform.git \
    --path . \
    --revision main \
    --dest-server https://kubernetes.default.svc \
    --dest-namespace monitoring \
    --values values-k8sda.yaml \
    --values values-domain-prometheus.yaml

  argocd app create "$APP_PLATFORM_BACKEND" \
    --repo git@github.com:infn-epics/epik8s-platform.git \
    --path . \
    --revision main \
    --dest-server https://kubernetes.default.svc \
    --dest-namespace backend \
    --values values-k8sda.yaml \
    --values values-domain-backend.yaml

  argocd app create "$APP_PLATFORM_AI" \
    --repo git@github.com:infn-epics/epik8s-platform.git \
    --path . \
    --revision main \
    --dest-server https://kubernetes.default.svc \
    --dest-namespace ai-platform \
    --values values-k8sda.yaml \
    --values values-domain-ai-platform.yaml

If ArgoCD is not configured for SSH git access, switch `--repo` to:
`https://github.com/infn-epics/epik8s-platform.git`.

Domain overlays used above:
- `values-domain-core.yaml`
- `values-domain-grafana.yaml`
- `values-domain-prometheus.yaml`
- `values-domain-backend.yaml`
- `values-domain-ai-platform.yaml`

## 0) Safety snapshots and evidence capture

Run before any ownership change.

  kubectl get applications.argoproj.io -n argocd -o wide > /tmp/argocd-apps-before.txt
  kubectl get all,cm,secret,sa,role,rolebinding,pvc,ingress -n backend -o yaml > /tmp/backend-before.yaml
  kubectl get all,cm,secret,sa,role,rolebinding,pvc,ingress -n monitoring -o yaml > /tmp/monitoring-before.yaml
  helm list -A > /tmp/helm-list-before.txt
  helm get values grafana -n monitoring > /tmp/grafana-values-before.yaml
  helm get values prometheus -n monitoring > /tmp/prometheus-values-before.yaml

## 1) Freeze ArgoCD automation for old/new owners

  argocd app set "$APP_BACKEND_OLD" --sync-policy none

Freeze new per-domain apps during transition:

  argocd app set "$APP_PLATFORM_CORE" --sync-policy none
  argocd app set "$APP_PLATFORM_GRAFANA" --sync-policy none
  argocd app set "$APP_PLATFORM_PROMETHEUS" --sync-policy none
  argocd app set "$APP_PLATFORM_BACKEND" --sync-policy none
  argocd app set "$APP_PLATFORM_AI" --sync-policy none

## 2) Verify epik8s-platform render for takeover domains

  cd /Users/michelottilabs/progetti/epik8s-platform
  helm dependency build
  helm template epik8s-platform . -f values.yaml -f values-k8sda.yaml > /tmp/epik8s-platform-render.yaml

Quick checks (must show expected objects):

  rg -n "kind: (Elasticsearch|Kafka|KafkaNodePool|StatefulSet|Deployment|Service|Ingress|PersistentVolumeClaim)" /tmp/epik8s-platform-render.yaml
  rg -n "name: grafana|name: prometheus-kube-prometheus|name: mongodb|name: elasticsearch" /tmp/epik8s-platform-render.yaml

## 3) Handoff backend objects from epik8s-backend to epik8s-platform-backend

Strategy: prevent old app from reconciling, then let new app adopt matching objects.

3.1 Keep epik8s-backend app present but inert during transition

  argocd app set "$APP_BACKEND_OLD" --sync-policy none

3.2 Ensure `APP_PLATFORM_BACKEND` is configured as backend-only domain (see domain split above).

3.3 Sync backend domain app in a controlled way

  argocd app sync "$APP_PLATFORM_BACKEND"
  argocd app wait "$APP_PLATFORM_BACKEND" --health --sync --timeout 600

3.4 Validate tracking moved to epik8s-platform for backend key objects

  kubectl get elasticsearch -n backend elasticsearch -o yaml | rg "argocd.argoproj.io/tracking-id"
  kubectl get statefulset -n backend mongodb -o yaml | rg "argocd.argoproj.io/tracking-id"
  kubectl get kafka -n backend eph-kafka -o yaml | rg "argocd.argoproj.io/tracking-id"

Expected: tracking-id references `APP_PLATFORM_BACKEND`, not epik8s-backend.

## 4) Adopt standalone Grafana/Prometheus Helm resources into split monitoring releases

Important: this is ownership metadata adoption, not resource recreation.

4.1 Preview candidate monitoring resources to adopt

  cd /Users/michelottilabs/progetti/epik8s-platform
  MODE=preview OLD_RELEASES=grafana NEW_RELEASE=grafana NEW_RELEASE_NS=monitoring \
    ./docs/runbooks/adopt-monitoring-ownership-k8sda.sh

  MODE=preview OLD_RELEASES=prometheus NEW_RELEASE=prometheus NEW_RELEASE_NS=monitoring \
    ./docs/runbooks/adopt-monitoring-ownership-k8sda.sh

4.2 Apply ownership metadata patch (non-destructive)

  MODE=apply OLD_RELEASES=grafana NEW_RELEASE=grafana NEW_RELEASE_NS=monitoring \
    ./docs/runbooks/adopt-monitoring-ownership-k8sda.sh

  MODE=apply OLD_RELEASES=prometheus NEW_RELEASE=prometheus NEW_RELEASE_NS=monitoring \
    ./docs/runbooks/adopt-monitoring-ownership-k8sda.sh

4.3 Sync monitoring domain app and verify no recreate/drift storm

  argocd app sync "$APP_PLATFORM_GRAFANA"
  argocd app wait "$APP_PLATFORM_GRAFANA" --health --sync --timeout 900
  argocd app sync "$APP_PLATFORM_PROMETHEUS"
  argocd app wait "$APP_PLATFORM_PROMETHEUS" --health --sync --timeout 900

4.4 Confirm monitoring workloads stayed healthy

  kubectl get pods -n monitoring -o wide
  kubectl get svc,ingress,pvc -n monitoring

## 5) Decommission old owners after stable soak

After at least one full business day of stable operations:

5.1 Remove legacy ArgoCD backend app

  argocd app delete "$APP_BACKEND_OLD" --cascade=false

5.2 Uninstall standalone monitoring releases only if they are fully adopted

  helm uninstall grafana -n monitoring
  helm uninstall prometheus -n monitoring

Only do step 5.2 if step 4 was successful and epik8s-platform remains healthy.

## 6) Post-cutover verification checklist

- ArgoCD:
  - `APP_PLATFORM_CORE`, `APP_PLATFORM_GRAFANA`, `APP_PLATFORM_PROMETHEUS`, and `APP_PLATFORM_BACKEND` are Synced + Healthy
  - if enabled, `APP_PLATFORM_AI` is Synced + Healthy
  - epik8s-backend removed or disabled
- Backend:
  - Elasticsearch, MongoDB, Kafka healthy in namespace backend
- Monitoring:
  - Grafana reachable, datasources present
  - Prometheus scrape targets healthy
- Storage:
  - No PVC recreated unexpectedly
- Backups:
  - cluster-backup CronJob present and runs successfully

Useful checks:

  kubectl get applications.argoproj.io -n argocd
  kubectl get pods -n backend
  kubectl get pods -n monitoring
  kubectl get pvc -A
  kubectl -n backup get cronjob,job

## Rollback plan

If ownership handoff causes instability:

1. Stop new reconciliations

  argocd app set "$APP_PLATFORM_BACKEND" --sync-policy none
  argocd app set "$APP_PLATFORM_GRAFANA" --sync-policy none
  argocd app set "$APP_PLATFORM_PROMETHEUS" --sync-policy none
  argocd app set "$APP_PLATFORM_AI" --sync-policy none

2. Restore old owner

  argocd app set "$APP_BACKEND_OLD" --sync-policy automated
  argocd app sync "$APP_BACKEND_OLD"

3. Revert Helm ownership annotations for monitoring resources using saved resource lists and previous release metadata.

4. Validate workloads with the before snapshots captured in step 0.

## Notes for this cluster (k8sda)

- Live checks showed:
  - Elasticsearch and MongoDB tracked by Argo app epik8s-backend
  - Grafana managed by standalone Helm release grafana in namespace monitoring
- This runbook intentionally avoids delete/recreate flows for stateful services.
- During core-domain adoption, two pre-existing NetworkAttachmentDefinition
  objects (`default/euaps-cam-multinode`, `euaps/euaps-cams`) failed Argo apply
  with `metadata.resourceVersion must be specified for an update`.
  Non-destructive workaround used:
  1. `kubectl apply` the rendered NAD manifests once (no delete/recreate).
  2. Remove non-functional legacy annotations on `default/euaps-cam-multinode`
     (`capacity`, `cni-type`, `description`, `ip-range`, `nodes`).
  3. Re-run Argo sync with `prune=false`.
