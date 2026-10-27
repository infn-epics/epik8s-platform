{{/*
Argo CD sync options for anything whose loss is data loss: a namespace,
a volume claim, or a child Application that owns them.

  Delete=false  deleting the Application that manages this resource leaves
                it in place, even when the deletion cascades (the Argo CD
                UI's default) - deleting epik8s-platform-librechat on
                2026-10-27 took LibreChat's MongoDB volume with it.
  Prune=false   removing it from git, or a values change that stops
                rendering it, never makes Argo CD delete it either; a person
                deletes it on purpose.

Usage, under metadata:
  annotations:
    {{- include "epik8s-platform.keepOnDelete" . | nindent 4 }}
*/}}
{{- define "epik8s-platform.keepOnDelete" -}}
argocd.argoproj.io/sync-options: Delete=false,Prune=false
{{- end -}}

{{/*
The headers LibreChat sends to one ARGUS MCP server (an argusMcpServers or
argusMcpInstances entry), as JSON: X-Argus-Confirm-Writes unless the entry
sets confirmWrites: false, X-Argus-User-Token when it sets forwardUserToken.
See ai-platform/librechat.yaml.
*/}}
{{- define "epik8s-platform.argusHeaders" -}}
{{- $h := dict -}}
{{- if ne (toString .confirmWrites) "false" -}}
{{- $_ := set $h "X-Argus-Confirm-Writes" "true" -}}
{{- end -}}
{{- if .forwardUserToken -}}
{{- $_ := set $h "X-Argus-User-Token" "{{LIBRECHAT_OPENID_ACCESS_TOKEN}}" -}}
{{- end -}}
{{- toJson $h -}}
{{- end -}}
