{{/*
Helper templates for the MetalLB chart.
*/}}

{{- define "metallb.labels" -}}
{{ include "common.labels" . }}
app.kubernetes.io/component: metallb-config
{{- end -}}

{{- define "metallb.namespaceLabels" -}}
{{- if .Values.namespace.labels -}}
{{ toYaml .Values.namespace.labels }}
{{- end -}}
{{- end -}}
