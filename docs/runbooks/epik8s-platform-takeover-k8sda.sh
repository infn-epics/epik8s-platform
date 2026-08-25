#!/usr/bin/env bash
set -euo pipefail

# k8sda takeover helper: backend-domain handoff (old backend app -> new backend app).
# This script is intentionally non-destructive. It does NOT delete apps or
# uninstall releases.

APP_BACKEND_OLD="${APP_BACKEND_OLD:-epik8s-backend}"
APP_PLATFORM_NEW="${APP_PLATFORM_NEW:-epik8s-platform-backend}"

if [[ "$APP_PLATFORM_NEW" == "epik8s-platform-librechat" ]]; then
  echo "ERROR: APP_PLATFORM_NEW cannot be epik8s-platform-librechat"
  echo "That app targets argus-helm-chart only, not full platform takeover."
  exit 1
fi

echo "Checking ArgoCD applications in namespace argocd..."
kubectl get applications.argoproj.io -n argocd "$APP_BACKEND_OLD" >/dev/null
kubectl get applications.argoproj.io -n argocd "$APP_PLATFORM_NEW" >/dev/null

echo "Verifying $APP_PLATFORM_NEW source is epik8s-platform repo..."
REPO_URL=$(kubectl get applications.argoproj.io -n argocd "$APP_PLATFORM_NEW" -o jsonpath='{.spec.source.repoURL}')
if [[ "$REPO_URL" != *"epik8s-platform"* ]]; then
  echo "ERROR: $APP_PLATFORM_NEW repoURL does not look like epik8s-platform"
  echo "repoURL=$REPO_URL"
  exit 1
fi

echo "Capturing safety snapshots in /tmp..."
kubectl get applications.argoproj.io -n argocd -o wide > /tmp/argocd-apps-before.txt
kubectl get all,cm,secret,sa,role,rolebinding,pvc,ingress -n backend -o yaml > /tmp/backend-before.yaml
kubectl get all,cm,secret,sa,role,rolebinding,pvc,ingress -n monitoring -o yaml > /tmp/monitoring-before.yaml
helm list -A > /tmp/helm-list-before.txt
helm get values grafana -n monitoring > /tmp/grafana-values-before.yaml
helm get values prometheus -n monitoring > /tmp/prometheus-values-before.yaml

echo "Freezing old owner app: $APP_BACKEND_OLD"
argocd app set "$APP_BACKEND_OLD" --sync-policy none

echo "Freezing new owner app before controlled sync: $APP_PLATFORM_NEW"
argocd app set "$APP_PLATFORM_NEW" --sync-policy none

echo "Running controlled sync on new owner app: $APP_PLATFORM_NEW"
argocd app sync "$APP_PLATFORM_NEW"
argocd app wait "$APP_PLATFORM_NEW" --health --sync --timeout 900

echo "Verifying backend tracking annotations"
kubectl get elasticsearch -n backend elasticsearch -o yaml | rg "argocd.argoproj.io/tracking-id" || true
kubectl get statefulset -n backend mongodb -o yaml | rg "argocd.argoproj.io/tracking-id" || true
kubectl get kafka -n backend eph-kafka -o yaml | rg "argocd.argoproj.io/tracking-id" || true

echo "Takeover helper completed."
echo "Next: run monitoring ownership adoption script, then sync the monitoring domain app."
