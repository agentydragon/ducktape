{{/*
Helper templates for the Registry chart.
*/}}

{{- define "registry.labels" -}}
{{ include "common.labels" . }}
app.kubernetes.io/component: registry
{{- end -}}

{{- define "registry.selectorLabels" -}}
app.kubernetes.io/name: {{ include "common.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: registry
{{- end -}}

{{- define "registry.serviceAccountName" -}}
{{- $default := include "common.fullname" . -}}
{{- include "common.serviceAccountName" (dict "default" $default "values" (default (dict) .Values.serviceAccount)) -}}
{{- end -}}

{{- define "registry.pvcName" -}}
{{- if and .Values.persistence.enabled .Values.persistence.existingClaim -}}
{{ .Values.persistence.existingClaim }}
{{- else -}}
{{ printf "%s-storage" (include "common.fullname" .) }}
{{- end -}}
{{- end -}}
