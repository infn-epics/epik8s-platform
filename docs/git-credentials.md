# Git credentials for beamline pods (cloning private repositories)

Beamline workloads clone their configuration at start-up, in an init container
(`ghcr.io/infn-epics/container-init`). The top-level beamline repository is
usually public, but many **submodules** (soft-IOC scripts, OPIs, sites) live in
private groups on `baltig.infn.it`, and those need a token.

Two Secrets can provide tokens, both created out of band per beamline
namespace (like everything else in this repo, **no secret content is
committed**):

| Secret (values key)                                   | Tokens                    | Status        |
|-------------------------------------------------------|---------------------------|---------------|
| `epik8s-secret` (`epik8s_secrets`)                    | one per **host**          | existing      |
| `epik8s-git-credentials` (`git_credentials_secret`)   | one per **URL prefix**    | opt-in        |

They combine: a URL that matches a prefix of the second uses that token,
anything else falls back to the first.

## 1. `epik8s-secret`: one token per host

| Key          | Content                                                       |
|--------------|---------------------------------------------------------------|
| `git_token`  | one line, git-credential-store format (see below)             |
| `id_rsa`     | SSH private key (git over SSH, legacy)                        |
| `id_rsa.pub` | SSH public key                                                |

Mounted read-only at `/.ssh`; the init script does:

```sh
git config --global credential.helper "store --file=/.ssh/git_token"
git clone --depth 1 --recurse-submodules -b <branch> <repo> temp-config
```

Consumers: ioc-chart (IOCs, soft IOCs), phoebus-services-chart (channelfinder
feeder). generic-application-chart (`applications:` / `cronjobs:` entries) does
**not** use it: it has its own per-repo `gitRepoApp.token|secret` /
`gitRepoConfig.token|secret`, which put the token in the clone URL and
therefore do **not** reach private submodules. Use section 3 for those.

### `git_token` format

`git_token` is a [git-credential-store](https://git-scm.com/docs/git-credential-store)
file, **not** the bare token:

```
https://oauth2:<TOKEN>@baltig.infn.it
```

- GitLab ignores the username for token auth; `oauth2` is conventional
  (`x-access-token` for GitHub).
- The line matches **every repository on that host** (store matches on
  protocol + host only). One line per host, so **one token per host**.

### Token requirements

On baltig (GitLab), use a **group access token** (or a project/personal access
token of a bot user) with:

- scope `read_repository` (nothing else is needed to clone);
- role `Reporter` or higher;
- access to **every group** whose repositories are cloned. A group token only
  covers its own group and subgroups: SPARC submodules come from both
  `lnf-da-control` and `infn-epics`. With one token per host it must cover
  both; with section 3 each group gets its own;
- an **expiry date written down somewhere**: GitLab tokens expire (max 1
  year). An expired token is the most likely cause of a sudden, cluster-wide
  clone failure.

### Create / rotate

Check the token first, **without** your local credential helpers (they would
hide a bad token):

```sh
export TOKEN='<new token>'
for r in lnf-da-control/laser-synch-soft-ioc infn-epics/py-ioc-collector; do
  GIT_TERMINAL_PROMPT=0 HOME=/nonexistent git -c credential.helper= \
    ls-remote "https://oauth2:${TOKEN}@baltig.infn.it/${r}.git" HEAD
done
```

Create (new namespace) or rotate the token only, keeping the SSH keys:

```sh
kubectl -n <beamline-ns> create secret generic epik8s-secret \
  --from-literal=git_token="https://oauth2:${TOKEN}@baltig.infn.it" \
  --from-file=id_rsa=./id_rsa --from-file=id_rsa.pub=./id_rsa.pub

kubectl -n <beamline-ns> patch secret epik8s-secret --type merge \
  -p "{\"stringData\":{\"git_token\":\"https://oauth2:${TOKEN}@baltig.infn.it\n\"}}"
```

kubelet refreshes mounted Secret files within ~1 minute. Pods stuck in
`Init:CrashLoopBackOff` pick it up on their next retry; running pods are not
affected (they only clone at start).

### Symptoms and gotchas

- `fatal: could not read Username for 'https://baltig.infn.it': No such device or address`
  → no **valid** credential for that URL (missing, expired or revoked token,
  or a token without access to that group).
- The top-level repo clones fine and only submodules fail → the top repo is
  public and the token is bad. Don't let that mislead you.
- `fatal: unable to get credential storage lock in 1000 ms: Read-only file system`
  → **harmless**: after a successful auth git tries to write the credential
  back into the read-only mounted file.
- Pods that have been running for months keep working with an expired token;
  only restarts fail. A token expiry shows up on the first IOC restart.
- Repository visibility on baltig changes over time (some SPARC submodules
  became public). Test tokens against a repo you know is **private**, or a
  "working" test proves nothing.

## 2. Why more than one token

- One token per host: every private group must be readable by that one token.
- One blast radius: a leaked token reads everything it covers, in every
  namespace that holds a copy.
- Rotation is all-or-nothing.

## 3. `epik8s-git-credentials`: one token per URL prefix

### How it works

Git picks a credential helper by **URL prefix**, path included
(`credential.<url>.helper`, see `git help config`). The Secret holds a
`gitconfig` mapping prefixes to token files, plus one token file per key:

| Key                     | Content                                             |
|-------------------------|-----------------------------------------------------|
| `gitconfig`             | prefix → token file mapping (below)                 |
| `baltig-lnf-da-control` | `https://oauth2:<TOKEN_LNF>@baltig.infn.it`         |
| `baltig-infn-epics`     | `https://oauth2:<TOKEN_INFN_EPICS>@baltig.infn.it`  |
| `github-infn-epics`     | `https://x-access-token:<GH_TOKEN>@github.com`      |

```ini
[credential "https://baltig.infn.it/lnf-da-control"]
	helper =
	helper = store --file=/etc/git-credentials/baltig-lnf-da-control
[credential "https://baltig.infn.it/infn-epics"]
	helper =
	helper = store --file=/etc/git-credentials/baltig-infn-epics
```

With `git_credentials_secret` set, the charts mount the Secret (optional,
whole, mode 0444) at `/etc/git-credentials` and add one line to every clone
script:

```sh
git config --global include.path /etc/git-credentials/gitconfig
```

- The empty `helper =` resets the helper list, so for that prefix the
  `epik8s-secret` token is **not** tried.
- Prefixes match on path segments and include subgroups
  (`.../lnf-da-control/sub/x.git` uses the `lnf-da-control` token).
- URLs matching no prefix fall back to `epik8s-secret`'s `git_token` (if
  `epik8s_secrets` is set), else go unauthenticated.
- If the Secret does not exist the volume is empty (`optional: true`) and git
  silently ignores the missing include: same behaviour as without it.
- The token file URL has **no path**: path selection is done by the
  gitconfig, not by the store.
- **Do not set `credential.useHttpPath=true`**: the store helper then
  requires an exact path match and no entry matches.

Verified in `ghcr.io/infn-epics/container-init` (git 2.43) against baltig,
default `git_token` deliberately wrong: the private `infn-epics/py-ioc-collector`
cloned with its prefix token; the private `lnf-da-control/laser-synch-soft-ioc`,
with no prefix entry, fell back to the wrong default and was denied; without
the Secret mounted the behaviour was unchanged.

### Chart support

| Chart                     | Where                                                    |
|---------------------------|----------------------------------------------------------|
| ioc-chart                 | `init-container-git` (IOCs, soft IOCs)                   |
| phoebus-services-chart    | channelfinder feeder `init-git-clone`                    |
| generic-application-chart | `init-config-container-git`, `init-app-container-git` (Deployment and CronJob) |
| epik8s-chart              | passes `git_credentials_secret` to iocs, softiocs, services, cronjobs, applications |

Unset (the default), every chart renders byte-identical manifests to before:
existing beamlines see no change until they opt in.

### Create the Secret

[`docs/scripts/git-credentials-secret.sh`](scripts/git-credentials-secret.sh)
builds the `gitconfig` and the token files and applies the Secret. Tokens come
from environment variables or a hidden prompt, never from the command line:

```sh
export GIT_TOKEN_BALTIG_LNF_DA_CONTROL='<lnf-da-control token>'
export GIT_TOKEN_BALTIG_INFN_EPICS='<infn-epics token>'
# optional: verify each token against a private repo first
export CHECK_REPO_BALTIG_LNF_DA_CONTROL=laser-synch-soft-ioc
export CHECK_REPO_BALTIG_INFN_EPICS=py-ioc-collector

docs/scripts/git-credentials-secret.sh -n sparc --check --dry-run \
  baltig-lnf-da-control=https://baltig.infn.it/lnf-da-control \
  baltig-infn-epics=https://baltig.infn.it/infn-epics
# looks right? run again without --dry-run
```

The script rewrites the whole Secret: pass **all** entries each time. To rotate
one token by hand instead:

```sh
kubectl -n sparc patch secret epik8s-git-credentials --type merge \
  -p "{\"stringData\":{\"baltig-infn-epics\":\"https://oauth2:${TOKEN}@baltig.infn.it\n\"}}"
```

### Enable it on a beamline

In the beamline values (e.g. `epik8-sparc/deploy/values.yaml`):

```yaml
epik8s_secrets: epik8s-secret                  # unchanged, fallback
git_credentials_secret: epik8s-git-credentials
```

It can also be set on a single `iocs`/`softiocs`/`services`/`cronjobs`/
`applications` entry (or in `iocDefaults`). An entry value wins over the
global one, and `git_credentials_secret: ""` on an entry disables it there.

**Turning it on changes the pod template of every IOC/app it reaches** (new
volume + mount + script line): on sync, ArgoCD restarts them all. Plan the
global switch as a maintenance action, or enable it entry by entry and let
the rest follow at the next planned restart.

### Rollout checklist

1. One token per group (`read_repository`, Reporter); record each expiry date.
2. Create `epik8s-git-credentials` with the script (`--check`).
3. Enable on one IOC entry, sync, check its init log: every
   `Submodule path '...': checked out`, no `could not read Username`.
4. Enable beamline-wide in a maintenance window.
5. Once every private group has its own entry, `git_token` in
   `epik8s-secret` is only a fallback; narrow or remove it.
