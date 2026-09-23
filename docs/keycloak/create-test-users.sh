#!/usr/bin/env bash
# Creates the TEST users in the running Keycloak realm "epik8s" - one per role
# level, plus users that prove multi-beamline membership and deny-by-default -
# and stores their freshly generated passwords ONLY in the Secret
# keycloak-test-users (namespace keycloak). Passwords are never printed and
# never written to git.
#
# Idempotent: users that already exist are skipped, so it can be re-run after
# adding lines to USERS. Set TEST_PASSWORD to give the new users a fixed test
# password (default: a random one per user, stored only in the Secret).
# Read a password:  kubectl -n keycloak get secret keycloak-test-users \
#                     -o jsonpath='{.data.operator\.btf}' | base64 -d
set -euo pipefail

NS=${NS:-keycloak}
# username | realm role (level) | beamline groups
USERS='viewer.btf|epik8s-viewer|btf
operator.btf|epik8s-operator|btf
operator.multi|epik8s-operator|btf,sparc
expert.btf|epik8s-expert|btf
viewer.sparc|epik8s-viewer|sparc
operator.sparc|epik8s-operator|sparc
expert.sparc|epik8s-expert|sparc
viewer.euaps|epik8s-viewer|euaps
operator.euaps|epik8s-operator|euaps
expert.euaps|epik8s-expert|euaps
admin.epik8s|epik8s-admin|btf,sparc,euaps
norole.btf||btf'

# Which users exist already (kcadm inside the pod; admin creds come from the pod env).
existing_local=$(kubectl -n "$NS" exec deploy/keycloak -- sh -c '/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null && /opt/keycloak/bin/kcadm.sh get users -r epik8s --fields username --format csv --noquotes')

remote=$(mktemp) envf=$(mktemp)
trap 'rm -f "$remote" "$envf"' EXIT

cat > "$remote" <<'HDR'
set -e
K=/opt/keycloak/bin/kcadm.sh
$K config credentials --server http://localhost:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null
existing=$($K get users -r epik8s --fields username --format csv --noquotes)
HDR

while IFS='|' read -r u role groups; do
  pw=${TEST_PASSWORD:-$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-24)}
  # Skip users that already exist (checked locally against the realm).
  if grep -qx "$u" <<< "$existing_local"; then echo "skip $u (exists)"; continue; fi
  printf '%s=%s\n' "$u" "$pw" >> "$envf"
  {
    echo "echo creating $u"
    echo "\$K create users -r epik8s -s username=$u -s enabled=true -s email=$u@epik8s.invalid -s emailVerified=true -s firstName=Test -s lastName=$u >/dev/null"
    echo "\$K set-password -r epik8s --username $u --new-password '$pw'"
    [ -n "$role" ] && echo "\$K add-roles -r epik8s --uusername $u --rolename $role"
    for g in ${groups//,/ }; do
      echo "uid=\$(\$K get users -r epik8s -q username=$u --fields id --format csv --noquotes)"
      echo "gid=\$(\$K get groups -r epik8s -q search=$g --fields id --format csv --noquotes)"
      echo "\$K update users/\$uid/groups/\$gid -r epik8s -s realm=epik8s -s userId=\$uid -s groupId=\$gid -n"
    done
  } >> "$remote"
done <<< "$USERS"

if [ ! -s "$envf" ]; then echo "nothing to create"; exit 0; fi
kubectl -n "$NS" exec -i deploy/keycloak -- sh -s < "$remote"
if [ -z "${TEST_PASSWORD:-}" ]; then
  # Random passwords: keep them only in the Secret (never printed, never in git).
  kubectl -n "$NS" create secret generic keycloak-test-users --from-env-file="$envf" --dry-run=client -o yaml | kubectl apply -f -
  echo "done - passwords are in secret/keycloak-test-users (not shown)"
else
  echo "done - new users have the password given in TEST_PASSWORD"
fi
