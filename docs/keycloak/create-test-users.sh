#!/usr/bin/env bash
# Creates the TEST users in the running Keycloak realm "epik8s" - one per role
# level, plus users that prove multi-beamline membership and deny-by-default -
# and stores their freshly generated passwords ONLY in the Secret
# keycloak-test-users (namespace keycloak). Passwords are never printed and
# never written to git.
#
# Idempotent guard: refuses to run if any of the users already exists.
# Read a password:  kubectl -n keycloak get secret keycloak-test-users \
#                     -o jsonpath='{.data.operator\.btf}' | base64 -d
set -euo pipefail

NS=${NS:-keycloak}
# username | realm role (level) | beamline groups
USERS='viewer.btf|epik8s-viewer|btf
operator.btf|epik8s-operator|btf
operator.multi|epik8s-operator|btf,sparc
expert.sparc|epik8s-expert|sparc
admin.epik8s|epik8s-admin|btf,sparc,euaps
norole.btf||btf'

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
  pw=$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-24)
  printf '%s=%s\n' "$u" "$pw" >> "$envf"
  {
    echo "echo \"\$existing\" | grep -qx '$u' && { echo 'user $u already exists - aborting' >&2; exit 1; }"
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

kubectl -n "$NS" exec -i deploy/keycloak -- sh -s < "$remote"
kubectl -n "$NS" create secret generic keycloak-test-users --from-env-file="$envf"
echo "done - passwords are in secret/keycloak-test-users (not shown)"
