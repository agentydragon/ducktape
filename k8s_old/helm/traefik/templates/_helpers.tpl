{{/*
Helper templates for the Traefik chart.
*/}}

{{- define "traefik.labels" -}}
{{ include "common.labels" . }}
app.kubernetes.io/component: ingress-controller
{{- end -}}

{{- define "traefik.selectorLabels" -}}
app.kubernetes.io/name: {{ include "common.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: ingress-controller
{{- end -}}

{{- define "traefik.serviceAccountName" -}}
{{- $default := include "common.fullname" . -}}
{{- include "common.serviceAccountName" (dict "default" $default "values" (default (dict) .Values.serviceAccount)) -}}
{{- end -}}
