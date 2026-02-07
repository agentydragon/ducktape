{{- define "matrix-stack.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "matrix-stack.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "matrix-stack.chart" -}}
{{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "matrix-stack.matrixSecretName" -}}
{{- if and .Values.matrix (hasKey .Values.matrix "secret") (.Values.matrix.secret.name) -}}
{{- .Values.matrix.secret.name -}}
{{- else -}}
{{- .Release.Name -}}
{{- end -}}
{{- end -}}

{{- define "matrix-stack.bootstrapSecretName" -}}
{{- if and .Values.bootstrap (hasKey .Values.bootstrap "secret") (.Values.bootstrap.secret.name) -}}
{{- .Values.bootstrap.secret.name -}}
{{- else -}}
{{- printf "%s-bootstrap" (include "matrix-stack.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "matrix-stack.synapse-internal-url" -}}
{{- $service := .Release.Name -}}
{{- $port := 80 -}}
{{- if and .Values.matrix (.Values.matrix.service) (.Values.matrix.service.port) -}}
{{- $port = .Values.matrix.service.port -}}
{{- end -}}
{{- printf "http://%s:%s" $service (toString $port) -}}
{{- end -}}
