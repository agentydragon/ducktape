{{/*
Helper templates for the Ducktape Gitea stack chart.
*/}}

{{- define "gitea-stack.fullname" -}}
{{ include "common.fullname" . }}
{{- end -}}

{{- define "gitea-stack.sharedStorageClaimName" -}}
{{- if .Values.sharedStorage.claimName }}
{{- .Values.sharedStorage.claimName | quote -}}
{{- else }}
{{- printf "%s-shared-storage" (include "gitea-stack.fullname" .) | quote -}}
{{- end }}
{{- end -}}

{{- define "gitea-stack.emberServiceAccountName" -}}
{{- default "gitea-ember-bootstrap" .Values.emberBootstrap.serviceAccountName -}}
{{- end -}}

{{- define "gitea-stack.emberSecretWriterRoleName" -}}
{{- default "ember-secret-writer" .Values.emberBootstrap.secretWriterRoleName -}}
{{- end -}}

{{- define "gitea-stack.emberScriptConfigMapName" -}}
{{- default "gitea-ember-bootstrap" .Values.emberBootstrap.scriptConfigMapName -}}
{{- end -}}
