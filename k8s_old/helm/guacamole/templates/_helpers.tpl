{{- define "guacamole.name" -}}
{{ include "common.name" . }}
{{- end -}}

{{- define "guacamole.fullname" -}}
{{ include "common.fullname" . }}
{{- end -}}

{{- define "guacamole.labels" -}}
{{ include "common.labels" . }}
{{- end -}}

{{- define "guacamole.selectorLabels" -}}
{{ include "common.selectorLabels" . }}
{{- end -}}

{{- define "guacamole.serviceAccountName" -}}
{{ include "common.serviceAccountName" (dict "values" .Values.serviceAccount "default" (printf "%s" (include "guacamole.fullname" .))) }}
{{- end -}}

{{- define "guacamole.guacdServiceName" -}}
{{ printf "%s-guacd" (include "guacamole.fullname" .) }}
{{- end -}}

{{- define "guacamole.guacamoleServiceName" -}}
{{ printf "%s-guacamole" (include "guacamole.fullname" .) }}
{{- end -}}

{{- define "guacamole.dbSecretName" -}}
{{- default "guacamole-db-credentials" .Values.postgres.secretName -}}
{{- end -}}

{{- define "guacamole.envSecretKey" -}}
{{- default "POSTGRES_PASSWORD" .Values.postgres.secretKeys.envPasswordKey -}}
{{- end -}}

{{- define "guacamole.userSecretKey" -}}
{{- default "password" .Values.postgres.secretKeys.userPasswordKey -}}
{{- end -}}

{{- define "guacamole.adminSecretKey" -}}
{{- default "postgres-password" .Values.postgres.secretKeys.adminPasswordKey -}}
{{- end -}}
