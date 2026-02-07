{{- define "shared-secret.name" -}}
{{ include "common.name" . }}
{{- end -}}

{{- define "shared-secret.fullname" -}}
{{ include "common.fullname" . }}
{{- end -}}

{{- define "shared-secret.labels" -}}
{{ include "common.labels" . }}
{{- end -}}
