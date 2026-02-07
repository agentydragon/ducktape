{{/*
Helper templates for the rspcache chart.
*/}}

{{- define "rspcache.fullname" -}}
{{- include "common.fullname" . -}}
{{- end -}}

{{- define "rspcache.labels" -}}
{{- include "common.labels" . -}}
{{- end -}}

{{- define "rspcache.selectorLabels" -}}
{{- include "common.selectorLabels" . -}}
{{- end -}}

{{- define "rspcache.configName" -}}
{{- printf "%s-config" (include "rspcache.fullname" .) -}}
{{- end -}}

{{- define "rspcache.proxyName" -}}
{{- printf "%s-proxy" (include "rspcache.fullname" .) -}}
{{- end -}}

{{- define "rspcache.adminName" -}}
{{- printf "%s-admin" (include "rspcache.fullname" .) -}}
{{- end -}}

{{- define "rspcache.postgresName" -}}
{{- .Values.postgres.name | default (printf "%s-postgres" (include "rspcache.fullname" .)) -}}
{{- end -}}
