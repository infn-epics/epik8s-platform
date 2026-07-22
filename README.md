# epik8s-platform

Platform-layer configuration for EPIK8S clusters: networking, storage, monitoring,
backend (MongoDB/Elasticsearch/Kafka) and the AI platform - everything a beamline
deployed via `epik8s-chart` needs underneath it, kept separate from beamline
charts (`epik8s-chart` and the per-facility `epik8-sparc` / `epik8s-btf` /
`epik8s-eli` repos).

This codifies what is *actually running* on the `k8sda` cluster today into a
Helm chart, so that state has a git source of truth for the first time. It does
**not** change anything on the live cluster - see "What this is not" below.

## Why this exists

Before this chart, the platform layer had no single source of truth:

- MetalLB `IPAddressPool`/`L2Advertisement` and all `NetworkAttachmentDefinition`s
  were applied by hand, no git source.
- The 4 site `StorageClass`es (`nfs20t`, `archiver-lts`, `archiver-mts`, `cephfs-rwx`)
  were applied by hand.
- Monitoring (`kube-prometheus-stack` + `grafana`) was installed via plain
  `helm install`, not tracked by ArgoCD.
- The `ai-platform` namespace (vllm, litellm, qdrant, embeddings,
  confluence-ingester, mcp servers, `openwebui`) existed as a kustomize tree in
  `k8sda/ai-platform/` that was **not** wired into ArgoCD - the live objects were
  applied from it manually, and had already drifted from that tree in places
  (e.g. `epics-mcp` and `git-mcp` exist live but have no corresponding directory
  in `k8sda/ai-platform/`). `openwebui` has since been replaced by a
  centralized LibreChat - see "Centralized LibreChat" below; that part is new
  capability, not codification of something live.
- The `backend` namespace (MongoDB, Elasticsearch/Kibana via the ECK operator,
  Kafka via the Strimzi operator - referenced by every beamline's
  `backend.mongo.host` / `.elasticsearch.host` / `.kafka.host` values in
  `epik8s-chart`) *is* ArgoCD-managed, but by a real external repo
  (`github.com/infn-epics/epik8s-backend.git`, branch `backend-operators`) whose
  locally-checked-out branch (`main`) had already drifted from what's live - see
  the note in `templates/backend/` below.

This chart was built directly from live `kubectl`/`helm` state (not from the
`k8sda` git tree, which was found to be stale/incomplete in places), and every
resource it renders was diffed against the live cluster - see "Verification"
below.

## Scope: what's codified vs. what's a prerequisite

This chart codifies **site-specific policy objects** - the CRs/values that
encode "this cluster, these subnets, these hostnames, these credentials-by-reference".
It does **not** install the underlying operators/controllers:

| Codified here | Prerequisite (install separately) |
|---|---|
| MetalLB `IPAddressPool` / `L2Advertisement` | MetalLB controller (native manifest, `quay.io/metallb/controller:v0.15.2` on this cluster - not a Helm release) |
| `NetworkAttachmentDefinition`s | Multus, whereabouts IPAM (RKE2 addon on this cluster) |
| `StorageClass`es | csi-driver-nfs, cephfs-csi-operator |
| kube-prometheus-stack + grafana | ingress-nginx (RKE2 addon), cert-manager if TLS is terminated by cert-manager-issued secrets |
| ai-platform workloads | GPU operator (for `vllm`'s `nvidia.com/gpu` request), ingress-nginx |
| Elasticsearch/Kibana/Kafka CRs | eck-operator, strimzi-kafka-operator - both ARE Helm dependencies of this chart (see below), so nothing extra to install for those two specifically |
| Centralized LibreChat (`aiPlatform.librechat`) | **ArgoCD** (this is the one domain in this chart that isn't a plain `helm template`/`helm install` target - see "Centralized LibreChat" below), and `argus-helm-chart` pushed to a real git remote |
| Loki + Alloy (`logging.loki`/`logging.alloy`) | ingress-nginx not required (no Ingress in this domain) - just a default StorageClass for Loki's PVC. Both ARE Helm dependencies of this chart, so nothing extra to install for those two specifically |

A from-scratch cluster needs those prerequisites in place first; this chart
covers everything layered on top of them.

## Centralized LibreChat

`aiPlatform.librechat` replaces the old `openwebui` component with a single,
cluster-wide [LibreChat](https://www.librechat.ai/) instance, deployed via
[argus-helm-chart](../argus-helm-chart) (LibreChat + optional
[ARGUS MCP](https://github.com/infn-epics/argus-mcp-server), the
accelerator-control MCP server). Unlike every other `ai-platform` component,
`templates/ai-platform/librechat.yaml` does **not** render a raw manifest or a
native Helm dependency - `argus-helm-chart` isn't published to any Helm/OCI
repository (git-only), and a `file://../argus-helm-chart` Chart.yaml
dependency would only resolve on a machine that happens to have both repos
checked out as siblings, silently breaking the moment ArgoCD (which only
checks out this repo) tries to `helm dependency build` it. Instead it renders
**one ArgoCD `Application`** sourcing `argus-helm-chart`'s repo directly - the
same pattern `epik8s-da-infra` already uses for `epik8s-platform` itself. This
is a deliberate, scoped exception: it's the only place in this chart that
assumes ArgoCD at runtime.

**Known blocker, same category as `epik8s-da-infra`'s note about this repo**:
`aiPlatform.librechat.repoURL` in `values-k8sda.yaml` points at
`https://baltig.infn.it/lnf-da-control/argus-helm-chart.git`, which doesn't
exist yet - `argus-helm-chart` has only been developed locally. Push it there
(or wherever it ends up, updating `repoURL` accordingly) before this
Application will sync.

**Why LibreChat, not each beamline's own instance**: every beamline used to
deploy a full `argus-helm-chart` release (its own LibreChat + MongoDB +
Meilisearch + a local ARGUS MCP wired to that beamline's own EPICS/archiver/
channelfinder/etc.) via `epik8s-chart`'s `argus` service branch. The chat
frontend is now centralized here instead; the per-beamline part that's
genuinely beamline-specific - the ARGUS MCP server itself - stays exactly
where it is. `argus-helm-chart` gained a `librechat.enabled` toggle (default
`true`, so every existing per-beamline release is unaffected) so a beamline
release can run ARGUS MCP only and register with this central instance
instead - see that repo's own values.yaml. **No beamline has been migrated as
part of this** - existing per-beamline releases keep running exactly as
before until someone deliberately flips a specific one over later.

**Registering a beamline's ARGUS MCP**: append to
`aiPlatform.librechat.argusMcpServers` in `values-k8sda.yaml`:

```yaml
aiPlatform:
  librechat:
    argusMcpServers:
      - name: btf-argus
        namespace: btf
        serviceName: argus-argus-helm-chart-argus-mcp # confirm, don't assume - see below
        # port defaults to 8000, path defaults to "/sse"
```

`serviceName` is **not** simply `argus-mcp` - `argus-helm-chart`'s Helm naming
convention makes it `<release-name>-argus-helm-chart-argus-mcp` unless that
beamline's release set `fullnameOverride`. Confirm with
`kubectl get svc -n <beamline-namespace> | grep argus-mcp` before adding an
entry, don't assume the name above. This is a manually-maintained list, not
automatic discovery - by design, matching the scope of this change.

Also carries forward what `openwebui`'s `TOOL_SERVER_CONNECTIONS` already
registered (`kubernetes-mcp`, `rag-mcp`) as `aiPlatform.librechat.mcpServers`
entries, and points `llm.endpoints` at the already-deployed `litellm` gateway
rather than duplicating a direct-to-vllm endpoint (`argus-helm-chart`'s own
`vllmService` "facility-wide singleton" stays disabled, since this chart
already has that singleton as `aiPlatform.vllm`).

## Centralized logging

`logging.loki`/`logging.alloy` add cluster-wide pod log ingestion: Grafana
[Loki](https://grafana.com/oss/loki/) as the log store, and [Grafana
Alloy](https://grafana.com/docs/alloy/latest/) (a DaemonSet) as the collector,
shipping every pod's logs to it. Both are real Helm dependencies of this
chart, same pattern as `kube-prometheus-stack`/`eck-operator`/
`strimzi-kafka-operator` - not bespoke templates.

**Why Loki, not the existing `backend` Elasticsearch**: that Elasticsearch is
genuinely control-critical - live ChannelFinder (`sparc-channelfinder`:
1.74M docs, `euaps-channelfinder`: 2.37M docs), olog, and save-and-restore
data the control room actively depends on, not a spare metadata store.
High-volume pod logs sharing that cluster risks resource contention against
services that matter operationally. Loki is also the right storage model for
this kind of data - it indexes labels (`namespace`/`pod`/`container`/`app`),
not full log content, which is both cheaper and matches what the original
architecture doc already called for (Loki + Grafana, not a second
Elasticsearch).

**Loki** runs `deploymentMode: SingleBinary` (this isn't at a scale that
needs the chart's distributed read/write/backend split), `storage.type:
filesystem` on the cluster's default StorageClass (no S3/GCS/MinIO stood up
just for a short retention window), `limits_config.retention_period: 168h`
(7 days on `values-k8sda.yaml`, 72h on `values-generic.yaml`) - logs are
disposable troubleshooting data, unlike the control-critical Elasticsearch
content next to it, which already has its own much longer retention.

**Alloy** ships as a DaemonSet, configured (via the chart's `configMap.content`
value, in [River](https://grafana.com/docs/alloy/latest/get-started/configuration-syntax/)
syntax) to discover every pod cluster-wide via `discovery.kubernetes`,
attach `namespace`/`pod`/`container`/`app` labels, and forward to Loki's
write endpoint via `loki.source.kubernetes` (reads pod logs through the
Kubernetes API, not a hostPath tail) - the standard k8s-logs-to-Loki recipe.
Its `ClusterRole` is extended (`pods`, `pods/log`, `namespaces`, `nodes` -
`get`/`list`/`watch`) to allow that.

**Double-nesting gotcha**: the chart dependency is named `alloy` (an outer
key that just scopes values to that subchart, same as every other
dependency here), but Alloy's own `values.yaml` *separately* has a
top-level key also called `alloy:`, wrapping `configMap`/`stabilityLevel`.
So `logging.*`-adjacent overrides in this repo need `alloy: { alloy: {
configMap: { content: ... } } }` - two `alloy:` keys, not one - while `rbac:`
sits at the first level only. Getting this wrong doesn't error, it just
silently renders the chart's bundled example config instead of the real
pipeline (caught by inspecting the rendered ConfigMap directly, not by
`helm lint`/`helm template` exiting clean - see Verification below).

`grafana:` (already deployed, already has a SPARC-archiver datasource from
before this domain existed) gets a `Loki` datasource added too, so there's a
log-browsing UI in Grafana itself immediately, independent of anything else
consuming Loki later.

**Not included in this domain**: any ARGUS/`argus-mcp-server` integration.
`argus-mcp-server` already has a `get_logs` tool shaped for exactly this kind
of data, but wiring it up (a `LokiProvider`, `argus-helm-chart` exposure,
`epik8s-chart` `argusDefaults` plumbing) is a deliberate later step - this
phase only stands up the ingestion pipeline and confirms logs actually flow,
so there's real data to wire AI troubleshooting to when that follow-up
happens.

## Observability: Langfuse

`langfuse.enabled` adds self-hosted [Langfuse](https://langfuse.com/) - LLM/MCP
observability for the centralized LibreChat: every chat turn becomes a
queryable trace, thumbs-up/down feedback becomes a structured score (with
`conversationId`/`messageId`/`userId` attached), and Langfuse ships its own
MCP server so an LLM session can directly query its own trace/feedback
history to help debug and improve ARGUS's tools - not just a human staring at
a dashboard.

**Real new footprint, not a lightweight addition**: the `langfuse/langfuse`
chart (one Helm dependency here, same pattern as `loki`/`kube-prometheus-stack`)
bundles Postgres, ClickHouse (which itself pulls in Zookeeper, even at a
single replica - Langfuse's schema uses replicated table engines that need
Keeper coordination regardless of replica count), Redis/Valkey, and MinIO as
dependencies. That's **6 stateful/app workloads** (Postgres, Zookeeper,
ClickHouse, Redis, MinIO, plus `langfuse-web`/`langfuse-worker`), not one.
Nothing on this cluster was reusable for any of the four datastores at the
time this was added (checked: no existing Postgres/ClickHouse/Redis/MinIO
anywhere, only ArgoCD's own internal Redis, which isn't shareable) - all
provisioned fresh.

**Deliberately single-replica/dev-mode throughout** (`postgresql`/`redis`
`architecture: standalone`, `clickhouse.replicaCount: 1`, its bundled
`clickhouse.zookeeper.replicaCount: 1` - the chart's own default is 3,
independent of the top-level `clickhouse.replicaCount` and easy to miss),
same right-sized-not-textbook-production posture as Loki's `SingleBinary`
mode - not the chart's own HA-by-default guidance, which assumes a much
larger production deployment than this cluster's scale warrants today.

**Secrets**: same policy as everywhere else in this chart - no literal
values in git. `langfuse.langfuse.salt`/`encryptionKey`/`nextauth.secret`
and every datastore's `auth.existingSecret` point at one `langfuse-secrets`
Secret, provisioned once, out of band:

```bash
kubectl create secret generic langfuse-secrets -n langfuse \
  --from-literal=salt="$(openssl rand -base64 32)" \
  --from-literal=encryption-key="$(openssl rand -hex 32)" \
  --from-literal=nextauth-secret="$(openssl rand -base64 32)" \
  --from-literal=postgres-password="$(openssl rand -base64 24)" \
  --from-literal=redis-password="$(openssl rand -base64 24)" \
  --from-literal=clickhouse-password="$(openssl rand -base64 24)" \
  --from-literal=minio-root-user="langfuse" \
  --from-literal=minio-root-password="$(openssl rand -base64 24)"
```

**Bootstrap ordering, not a bug**: `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
(the credentials LibreChat needs to actually send traces) can only be
generated *from Langfuse's own UI*, after first login to the deployed
instance - there's no way to pre-generate them before Langfuse exists. Until
that manual, one-time step happens and the result is patched into
`argus-helm-chart`'s `secrets.data.LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
(same out-of-band pattern already used for `RAGFLOW_API_KEY`), this domain's
plumbing is ready but traces won't actually flow yet.

**Not included in this phase**: deep per-tool-call span instrumentation
inside `argus-mcp-server` itself via the Langfuse Python SDK (would nest
individual tool-call spans under each LibreChat trace, giving one unified
view instead of two separately-correlated systems - Loki's `conversation_id`/
`message_id` labels vs. Langfuse's own trace data). Bigger lift, deliberately
deferred until the lighter correlation-ID approach below proves useful.

### MCP tool-call correlation (Loki side)

Separately from Langfuse itself, ARGUS's own MCP server registration (both
the per-beamline self-registered `argus:` entry in `argus-helm-chart` and
the centralized `argusMcpServers` entries this chart's own
`templates/ai-platform/librechat.yaml` renders) carries `headers:` using
LibreChat's `{{LIBRECHAT_BODY_CONVERSATIONID}}`/`{{LIBRECHAT_BODY_MESSAGEID}}`/
`{{LIBRECHAT_USER_ID}}` placeholders - confirmed (LibreChat's own docs) these
are freshly resolved before every individual tool call, not just once per
connection. `argus-mcp-server` reads them per-call
(`server.request_context.request.headers`, the MCP Python SDK's per-message
request context) and binds them into its existing structured-logging scope,
so every tool-call log line already flowing into Loki (see "Centralized
logging" above) now carries which LibreChat conversation/message/user it
came from - a bad-rated conversation's `conversationId` (from Langfuse) can
be grepped straight against Loki to see exactly which tools ran.

## What's a pre-existing oddity, reproduced not fixed

Per the compatibility mandate, known inconsistencies on the live cluster are
reproduced as-is rather than silently corrected:

- `metallb-system` has two `L2Advertisement`s (`advert-valan-109` and `l2`) bound
  to the same pool/selector - redundant, harmless.
- `kube-prometheus-stack`'s live release has `nodeExporter.extraArgs` /
  `hostNetwork` / `service.port(9200)` set as Helm values, but those keys are
  actually inert for this chart version (the real subchart scoping key is
  `prometheus-node-exporter:`, not `nodeExporter:` - the latter is only used for
  the dependency's enable/disable condition). The live DaemonSet actually runs
  on hostNetwork with the chart's default port 9100. This chart reproduces what's
  **actually running** (port 9100), not what was attempted.
- `embeddings`, `epics-mcp`, and `git-mcp` are currently `hashicorp/http-echo`
  placeholders, not real backends.
- The `eph-kafka` cluster has **two** `KafkaNodePool`s (`dual`, ephemeral storage
  with no resource limits, and `eph-kafka-pool`, persistent + resourced) - looks
  like a leftover from a migration between the two, reproduced as-is.
- MongoDB has no auth enabled (`mongo.auth.enabled: false` live) and no
  `NetworkPolicy` restricting access - reproduced as-is, not a new gap.

## Secrets handling

No Secret content is stored in this repo. Where a live Deployment consumes a
Secret (grafana admin credentials, ai-platform API keys, confluence-ingester
credentials), the chart references the **existing** Secret object by name
(`existingSecret`, `secretKeyRef`) rather than creating or embedding one:

- `grafana`: `admin.existingSecret: grafana` (keys `admin-user`/`admin-password`)
  instead of the live release's literal `adminPassword` value.
- ai-platform: `qdrant-secrets`, `confluence-ingester-secrets` are expected to
  already exist in the `ai-platform` namespace; this chart does not create them.
- Centralized LibreChat: `aiPlatform.librechat.*` never sets `secrets.data` -
  real LLM API keys and any argus-mcp backend credentials must be supplied
  out-of-band (e.g. `--set` on the generated Application, or a separate
  untracked overlay), the same policy argus-helm-chart's own README documents.

## Repo layout

```
Chart.yaml            # kube-prometheus-stack, grafana, eck-operator, strimzi-kafka-operator,
                       #   loki, alloy - as Helm dependencies, pinned to specific versions
values.yaml            # domain toggles + chart-level defaults
values-k8sda.yaml       # this cluster's exact current state
files/ai-platform/      # embedded script sources (mounted into ConfigMaps via .Files.Get)
templates/
  networking/           # values-driven: IPAddressPool, L2Advertisement, NAD
  storage/               # values-driven: StorageClass
  backend/                # Elasticsearch/Kibana CRs, Kafka+KafkaNodePool CRs, MongoDB
  ai-platform/             # one file per component, values for the site-specific bits;
                            #   librechat.yaml is the one exception that renders an
                            #   ArgoCD Application instead of a raw manifest, see below
```

Monitoring and the ECK/Strimzi operators are modelled as Helm chart
**dependencies** rather than bespoke templates, since they're large upstream
charts - reinventing them would be pure risk for no benefit. Because this bundles
formerly-independent Helm releases (`prometheus`, `grafana`) under one umbrella
release, `fullnameOverride` is set explicitly (`kube-prometheus-stack`,
`kube-state-metrics`, `prometheus-node-exporter`, `grafana` sub-blocks in
`values-k8sda.yaml`) to reproduce the exact live resource names - without it,
Helm's default naming would derive names from the umbrella release name instead
and rename every resource.

The Elasticsearch/Kibana/Kafka **CRs themselves** (not the operators) are raw
templates in `templates/backend/`, built directly from live `kubectl get
elasticsearch|kibana|kafka|kafkanodepool -o yaml` output - the actual
`epik8s-backend` repo's checked-out branch (`main`) doesn't match what's
deployed (live runs on branch `backend-operators`: ECK-managed Elasticsearch/
Kibana and KRaft-mode Kafka, `main` has the classic `helm.elastic.co` chart
approach and a ZooKeeper-based Kafka values block). MongoDB's raw manifests
(Service + PVC + StatefulSet) *do* match `main` and were verified against live
objects the same way.

## What this is NOT

This phase produces **config, not a cutover**. Nothing in this repo has been
applied to the cluster, and no ArgoCD `Application` points at it. Wiring ArgoCD
to actually manage/adopt these already-running resources is a separate,
higher-risk step - `kubectl apply`/`helm upgrade --install` against objects an
operator (prometheus-operator) or another ArgoCD app (`cephfs-config` for
`cephfs-rwx`) already partially manages needs its own explicit review and is out
of scope here.

## Verification

Every resource this chart renders was diffed against live cluster state as part
of building it:

- All 11 `NetworkAttachmentDefinition`s: `spec.config` string compared
  **byte-for-byte** against `kubectl get network-attachment-definitions -A -o json`.
- All 4 `StorageClass`es: `provisioner`/`reclaimPolicy`/`volumeBindingMode`/
  `allowVolumeExpansion`/`mountOptions`/`parameters` compared field-by-field
  against `kubectl get storageclass -o json`.
- `IPAddressPool`/`L2Advertisement`: compared against
  `kubectl get ipaddresspools,l2advertisements -n metallb-system -o yaml`.
- Monitoring: resource names, grafana Deployment (image/env/volumes/init
  containers), grafana Ingress, grafana PVC (`existingClaim: grafana-nfs`), and
  Prometheus/node-exporter/kube-state-metrics Service ports compared against live
  objects and `helm get values prometheus|grafana -n monitoring`.
- ai-platform: all 44 rendered resources (9 Deployments, 9 Services, 4 Ingresses,
  3 PVCs, 1 CronJob, 4 HPAs, 10 ConfigMaps, RBAC) compared field-by-field
  (image, command/args, env wiring including `configMapKeyRef`/`secretKeyRef`
  names, Service ports/selectors, Ingress rules/tls, PVC accessModes/
  storageClassName/size, ConfigMap data, HPA replica bounds) against
  `kubectl get deploy,svc,ingress,pvc,cronjob,hpa,cm -n ai-platform -o json` -
  zero mismatches. The three embedded Python scripts
  (`kubernetes-mcp-server.py`, `rag-mcp-server.py`, `confluence_ingest.py`) were
  compared byte-for-byte against the live ConfigMap data.
- backend: `Elasticsearch`, `Kibana`, `Kafka`, both `KafkaNodePool`s, and the
  MongoDB `StatefulSet`/`Service`/`PersistentVolumeClaim` compared field-by-field
  against `kubectl get elasticsearch,kibana,kafka,kafkanodepool,statefulset,
  service,pvc -n backend -o json` - all match except fields the API server/ECK
  webhook injects as defaults (empty sub-objects, `imagePullPolicy`, etc.), which
  is expected and not something a manifest should specify.

Centralized logging (Loki + Alloy) is also new capability, not codification -
there's no live instance to diff against either. Verified by rendering both
value profiles (`values-k8sda.yaml`, `values-generic.yaml`) and inspecting
the actual output rather than trusting a clean `helm lint`/`helm template`
exit code: `helm template epik8s-platform . -f <profile> --show-only
charts/alloy/templates/configmap.yaml` to confirm the rendered River config
contains the real `loki.write` endpoint URL (not the chart's bundled example
pipeline - this is exactly the double-nesting bug described above, which
`helm template` alone doesn't catch since a wrong key nesting still renders
successfully, just with the wrong content), `--show-only
charts/alloy/templates/rbac.yaml` to confirm the custom `ClusterRole` rule is
present alongside the chart's defaults, `--show-only
charts/loki/templates/config.yaml` to confirm `retention_period` matches each
profile (168h/72h), and `--show-only charts/grafana/templates/configmap.yaml`
to confirm the `Loki` datasource entry renders next to the existing SPARC
Archiver one. Per "What this is NOT" above, nothing has been applied to the
live cluster - deploying Loki/Alloy (new pods, new storage) needs its own
explicit go-ahead, and once live, actually confirming logs are flowing
(`logcli` or Grafana Explore against Loki) before starting the deferred ARGUS
follow-up.

Centralized LibreChat is new capability, not codification - there's no live
instance to diff against, so it was verified differently: the rendered
Application's embedded Helm values were extracted and fed into
`argus-helm-chart` directly (`helm template argus-helm-chart -f
<extracted-values>`), confirming the whole LibreChat/MongoDB/Meilisearch stack
renders, `argusMcp` stays disabled (deployed per-beamline separately, not by
this Application), and - a real bug this caught - that `mcpServers` entries
actually appear in the rendered `librechat.yaml` (argus-helm-chart's own
template expects each entry to still carry `enabled`, since it does its own
`omit`; an earlier version of `librechat.yaml` here stripped that field first,
which silently dropped every entry). Also verified: `argus-helm-chart` renders
byte-identical output for its existing example values before/after adding the
`librechat.enabled` toggle (module random secret generation, expected across
separate `helm template` runs), and that `--set librechat.enabled=false`
renders ARGUS MCP's Deployment/Service/RBAC with no LibreChat/MongoDB/
Meilisearch/ConfigMap and no orphaned Secret dependency.

To re-verify after any change:

```bash
helm dependency build .
helm template epik8s-platform . -f values-k8sda.yaml > /tmp/rendered.yaml
# then diff relevant resources against:
kubectl get ipaddresspools,l2advertisements -n metallb-system -o yaml
kubectl get network-attachment-definitions -A -o yaml
kubectl get storageclass <name> -o yaml
kubectl get deploy,svc,ingress,pvc,cronjob,hpa,cm -n ai-platform -o yaml
kubectl get deploy,svc -n monitoring -o yaml
kubectl get elasticsearch,kibana,kafka,kafkanodepool,statefulset,svc,pvc -n backend -o yaml
```

No `helm install`, `kubectl apply`, or ArgoCD change was made to the live
cluster while building this chart.
