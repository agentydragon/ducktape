{{/*
Render a SealedSecret resource.
Expected dict keys:
- context (required) : root context (usually ".")
- namespace (required)
- name (optional; defaults to include "common.fullname" context)
- component (optional; adds app.kubernetes.io/component label)
- addAppLabel (optional bool, default true)
- extraLabels (map, optional)
- annotations (map, optional)
- encryptedData (map, required)
- type (string, optional, defaults to Opaque)
*/}}
{{- define "common.sealedSecret" -}}
{{- $ctx := required "common.sealedSecret: context is required" .context -}}
{{- $namespace := required "common.sealedSecret: namespace is required" .namespace -}}
{{- $name := .name | default (include "common.fullname" $ctx) -}}
{{- $component := .component -}}
{{- $addApp := .addAppLabel | default true -}}
{{- $extra := dict -}}
{{- if $component }}{{- $_ := set $extra "app.kubernetes.io/component" $component }}{{- end }}
{{- if $addApp }}{{- $_ := set $extra "app" (include "common.name" $ctx) }}{{- end }}
{{- range $k, $v := (.extraLabels | default (dict)) }}{{- $_ := set $extra $k $v }}{{- end }}
{{- $labels := include "common.labelsWith" (dict "context" $ctx "extra" $extra) | fromYaml -}}
{{- $annotations := .annotations | default (dict) -}}
{{- $encrypted := required "common.sealedSecret: encryptedData map is required" .encryptedData -}}
{{- $type := .type | default "Opaque" -}}
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: {{ $name }}
  namespace: {{ $namespace }}
{{- if $labels }}
  labels:
{{ toYaml $labels | indent 4 }}
{{- end }}
{{- if $annotations }}
  annotations:
{{ toYaml $annotations | indent 4 }}
{{- end }}
spec:
  encryptedData:
{{- range $key, $value := $encrypted }}
    {{ $key }}: {{ $value }}
{{- end }}
  template:
    metadata:
      name: {{ $name }}
      namespace: {{ $namespace }}
{{- if $labels }}
      labels:
{{ toYaml $labels | indent 8 }}
{{- end }}
{{- with $annotations }}
      annotations:
{{ toYaml . | indent 8 }}
{{- end }}
    type: {{ $type }}
{{- end -}}
