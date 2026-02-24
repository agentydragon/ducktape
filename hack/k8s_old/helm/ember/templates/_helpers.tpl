{{/*
Helper templates for the Ember chart.
*/}}

{{- define "ember.fullname" -}}
{{- include "common.fullname" . -}}
{{- end -}}

{{- define "ember.labels" -}}
{{- include "common.labels" . -}}
{{- end -}}

{{- define "ember.selectorLabels" -}}
{{- include "common.selectorLabels" . -}}
{{- end -}}

{{- define "ember.configName" -}}
{{- printf "%s-config" (include "ember.fullname" .) -}}
{{- end -}}

{{- define "ember.serviceName" -}}
{{- include "ember.fullname" . -}}
{{- end -}}

{{- define "ember.pvcName" -}}
{{- if .Values.persistence.existingClaim -}}
{{- .Values.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-history" (include "ember.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "ember.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (printf "%s-sa" (include "ember.fullname" .)) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "ember.componentLabel" -}}
app.kubernetes.io/component: agent
{{- end -}}

{{- define "ember.runtimeLabels" -}}
app.kubernetes.io/component: runtime
{{- end -}}

{{- define "ember.storageLabels" -}}
app.kubernetes.io/component: storage
{{- end -}}

{{- define "ember.rotatorLabels" -}}
app.kubernetes.io/component: rotator
{{- end -}}

{{- define "ember.rotatorServiceAccountName" -}}
{{- if .Values.serviceAccountRotator.create -}}
{{- default "ember-rspcache-rotator" .Values.serviceAccountRotator.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccountRotator.name -}}
{{- end -}}
{{- end -}}
