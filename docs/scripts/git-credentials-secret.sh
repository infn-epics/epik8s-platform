#!/usr/bin/env bash
# Create/update the per-URL-prefix Git credentials Secret used by the beamline
# charts (value git_credentials_secret). See docs/git-credentials.md.
#
#   git-credentials-secret.sh -n <namespace> [-s <secret>] [--check] [--dry-run] KEY=URL_PREFIX ...
#
# KEY        Secret key holding that token, e.g. baltig-lnf-da-control
# URL_PREFIX e.g. https://baltig.infn.it/lnf-da-control (subgroups included)
#
# The token of each KEY is read from env GIT_TOKEN_<KEY> (upper case, '-' and
# '.' -> '_'), e.g. GIT_TOKEN_BALTIG_LNF_DA_CONTROL, or prompted for (hidden).
# Tokens never appear on the command line or in the generated gitconfig.
#
#   --check    git ls-remote <URL_PREFIX>/<repo>.git with each token first
#              (repo from env CHECK_REPO_<KEY>, e.g. CHECK_REPO_BALTIG_LNF_DA_CONTROL=klystron-loop-soft-ioc)
#   --dry-run  print the Secret YAML with token values redacted, apply nothing
set -euo pipefail

ns="" secret="epik8s-git-credentials" check=0 dry=0 entries=()
while [ $# -gt 0 ]; do
  case "$1" in
    -n) ns="$2"; shift 2 ;;
    -s) secret="$2"; shift 2 ;;
    --check) check=1; shift ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *=*) entries+=("$1"); shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ns" ] && [ ${#entries[@]} -gt 0 ] || { sed -n '2,19p' "$0" >&2; exit 2; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT; chmod 700 "$work"
mount=/etc/git-credentials   # must match the charts' mountPath
args=()
: > "$work/gitconfig"

for e in "${entries[@]}"; do
  key=${e%%=*}; prefix=${e#*=}; prefix=${prefix%/}
  [[ "$key" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || { echo "bad key '$key' (lower case, digits, '-', '.')" >&2; exit 2; }
  [[ "$prefix" =~ ^https://[^/@]+(/.*)?$ ]] || { echo "bad prefix '$prefix' (https://host[/path], no user@)" >&2; exit 2; }
  host=${prefix#https://}; host=${host%%/*}
  var=GIT_TOKEN_$(echo "$key" | tr 'a-z.-' 'A-Z__')
  token=${!var:-}
  if [ -z "$token" ]; then read -r -s -p "token for $key ($prefix): " token; echo >&2; fi
  [ -n "$token" ] || { echo "empty token for $key" >&2; exit 2; }
  user=oauth2; [ "$host" = github.com ] && user=x-access-token

  if [ $check = 1 ]; then
    rvar=CHECK_REPO_$(echo "$key" | tr 'a-z.-' 'A-Z__'); repo=${!rvar:-}
    if [ -n "$repo" ]; then
      # token via env, not argv (visible in ps)
      GIT_PW="$token" GIT_USER="$user" GIT_TERMINAL_PROMPT=0 HOME=/nonexistent git -c credential.helper= \
        -c credential.helper='!f() { echo "username=$GIT_USER"; echo "password=$GIT_PW"; }; f' \
        ls-remote "$prefix/$repo.git" HEAD >/dev/null 2>&1 \
        && echo "check $key: OK ($prefix/$repo)" >&2 \
        || { echo "check $key: FAILED on $prefix/$repo" >&2; exit 1; }
    else
      echo "check $key: skipped (set $rvar=<repo>)" >&2
    fi
  fi

  printf 'https://%s:%s@%s\n' "$user" "$token" "$host" > "$work/$key"
  printf '[credential "%s"]\n\thelper =\n\thelper = store --file=%s/%s\n' "$prefix" "$mount" "$key" >> "$work/gitconfig"
  args+=(--from-file="$key=$work/$key")
done

yaml=$(kubectl -n "$ns" create secret generic "$secret" --from-file=gitconfig="$work/gitconfig" \
  "${args[@]}" --dry-run=client -o yaml)

if [ $dry = 1 ]; then
  echo "# gitconfig:"; sed 's/^/#   /' "$work/gitconfig"
  echo "$yaml" | awk '/^[^ ]/{d=($0=="data:")} d && /^  [^ ]+: / && !/^  gitconfig: /{sub(/: .*/, ": <redacted>")} 1'
else
  echo "$yaml" | kubectl apply -f -
  echo "Set git_credentials_secret: $secret in the beamline values, then sync." >&2
fi
