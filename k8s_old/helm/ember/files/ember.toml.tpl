[matrix]
base_url = {{ .Values.config.matrix.base_url | quote }}
admin_user_id = {{ .Values.config.matrix.admin_user_id | quote }}

[state]
dir = {{ .Values.config.state.dir | quote }}
workspace_dir = {{ .Values.config.state.workspace_dir | quote }}

[openai]
model = {{ .Values.config.openai.model | quote }}
reasoning_effort = {{ .Values.config.openai.reasoning_effort | quote }}
include_encrypted_reasoning = {{ ternary "true" "false" .Values.config.openai.include_encrypted_reasoning }}
api_base = {{ .Values.config.openai.api_base | quote }}
