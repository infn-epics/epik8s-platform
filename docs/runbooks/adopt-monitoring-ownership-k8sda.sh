#!/usr/bin/env bash
set -euo pipefail

# Adopt existing monitoring resources from standalone Helm releases
# (grafana/prometheus) into the epik8s-platform release ownership metadata.
#
# Modes:
#   MODE=preview  -> list candidate resources only (default)
#   MODE=apply    -> patch metadata

MODE="${MODE:-preview}"
NAMESPACE="${NAMESPACE:-monitoring}"
NEW_RELEASE="${NEW_RELEASE:-epik8s-platform}"
NEW_RELEASE_NS="${NEW_RELEASE_NS:-monitoring}"
OLD_RELEASES="${OLD_RELEASES:-grafana prometheus}"

if [[ "$MODE" != "preview" && "$MODE" != "apply" ]]; then
  echo "ERROR: MODE must be preview or apply"
  exit 1
fi

echo "Collecting namespaced resources by Helm instance label in namespace $NAMESPACE..."
NAMESPACED=$(kubectl get deploy,statefulset,daemonset,service,configmap,secret,serviceaccount,role,rolebinding,pvc,ingress,servicemonitor,prometheusrule -n "$NAMESPACE" -o name --show-managed-fields=false 2>/dev/null || true)

CANDIDATES_FILE=$(mktemp)
> "$CANDIDATES_FILE"

for old in $OLD_RELEASES; do
  echo "- release: $old"
  while IFS= read -r r; do
    [[ -z "$r" ]] && continue
    # Avoid touching Helm release bookkeeping secrets directly.
    if [[ "$r" == secret/sh.helm.release.v1.* ]]; then
      continue
    fi
    if kubectl -n "$NAMESPACE" get "$r" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}' 2>/dev/null | grep -qx "$old"; then
      echo "$r" >> "$CANDIDATES_FILE"
    fi
  done <<< "$NAMESPACED"
done

echo "Collecting cluster-scoped RBAC/webhook resources by Helm instance label..."
CLUSTER_SCOPED=$(kubectl get clusterrole,clusterrolebinding,mutatingwebhookconfiguration,validatingwebhookconfiguration -o name --show-managed-fields=false 2>/dev/null || true)
for old in $OLD_RELEASES; do
  while IFS= read -r r; do
    [[ -z "$r" ]] && continue
    if kubectl get "$r" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}' 2>/dev/null | grep -qx "$old"; then
      echo "$r" >> "$CANDIDATES_FILE"
    fi
  done <<< "$CLUSTER_SCOPED"
done

sort -u "$CANDIDATES_FILE" -o "$CANDIDATES_FILE"
COUNT=$(wc -l < "$CANDIDATES_FILE" | tr -d ' ')

echo "Candidate resources: $COUNT"
cat "$CANDIDATES_FILE"

if [[ "$MODE" == "preview" ]]; then
  echo "Preview mode complete. Re-run with MODE=apply to patch ownership metadata."
  exit 0
fi

echo "Applying ownership patch to release $NEW_RELEASE in namespace $NEW_RELEASE_NS..."
while IFS= read -r r; do
  [[ -z "$r" ]] && continue
  if [[ "$r" == clusterrole/* || "$r" == clusterrolebinding/* || "$r" == mutatingwebhookconfiguration/* || "$r" == validatingwebhookconfiguration/* ]]; then
    kubectl annotate "$r" \
      meta.helm.sh/release-name="$NEW_RELEASE" \
      meta.helm.sh/release-namespace="$NEW_RELEASE_NS" \
      --overwrite >/dev/null
    kubectl label "$r" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
  else
    kubectl -n "$NAMESPACE" annotate "$r" \
      meta.helm.sh/release-name="$NEW_RELEASE" \
      meta.helm.sh/release-namespace="$NEW_RELEASE_NS" \
      --overwrite >/dev/null
    kubectl -n "$NAMESPACE" label "$r" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
  fi
  echo "patched $r"
done < "$CANDIDATES_FILE"

echo "Patch completed."
echo "Next: sync Argo app owning epik8s-platform and verify monitoring health."
