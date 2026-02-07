{{/*
Helper templates for the Atuin chart.
*/}}

{{- define "atuin.serviceAccountName" -}}
{{- $default := include "common.fullname" . -}}
{{- include "common.serviceAccountName" (dict "default" $default "values" (default (dict) .Values.serviceAccount)) -}}
{{- end -}}

{{- define "atuin.serverLabels" -}}
{{ include "common.labels" . }}
app.kubernetes.io/component: server
{{- end -}}

{{- define "atuin.serverName" -}}
{{ printf "%s-server" (include "common.fullname" .) }}
{{- end -}}

{{- define "atuin.serverSelectorLabels" -}}
app.kubernetes.io/name: {{ include "common.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: server
{{- end -}}

{{- define "atuin.postgresLabels" -}}
{{ include "common.labels" . }}
app.kubernetes.io/component: postgres
{{- end -}}

{{- define "atuin.postgresSelectorLabels" -}}
app.kubernetes.io/name: {{ include "common.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end -}}

{{- define "atuin.postgresServiceName" -}}
{{- default "postgres" .Values.postgres.fullname -}}
{{- end -}}

{{- define "atuin.postgresPvcName" -}}
{{- if .Values.postgres.persistence.enabled -}}
{{- if .Values.postgres.persistence.existingClaim -}}
{{ .Values.postgres.persistence.existingClaim }}
{{- else -}}
{{ printf "%s-pvc" (include "atuin.postgresServiceName" .) }}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "atuin.postgresSecretName" -}}
{{- default (printf "%s-postgres" (include "common.fullname" .)) .Values.secrets.postgres.name -}}
{{- end -}}

{{- define "atuin.serverConfig" -}}
{{- if .Values.config.serverToml -}}
{{- tpl .Values.config.serverToml . -}}
{{- else -}}
{{- tpl (.Files.Get "files/config/server.toml") . -}}
{{- end -}}
{{- end -}}
