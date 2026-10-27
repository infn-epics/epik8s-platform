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
