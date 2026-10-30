package protocol

// CodeToolDefinition is the provider-neutral tool schema used to render tools into the
// Codex-compatible code-mode prompt.
type CodeToolDefinition struct {
	Name         string      `json:"name"`
	ToolName     ToolName    `json:"tool_name"`
	Description  string      `json:"description"`
	Kind         string      `json:"kind"`
	InputSchema  *JSONSchema `json:"input_schema"`
	OutputSchema *JSONSchema `json:"output_schema"`
}

type ToolName struct {
	Name      string  `json:"name"`
	Namespace *string `json:"namespace"`
}

// JSONSchema mirrors the subset and omission behavior of Codex's schema type.
type JSONSchema struct {
	Type                 string                `json:"type,omitempty"`
	Description          string                `json:"description,omitempty"`
	Enum                 []string              `json:"enum,omitempty"`
	Items                *JSONSchema           `json:"items,omitempty"`
	Properties           map[string]JSONSchema `json:"properties,omitempty"`
	Required             []string              `json:"required,omitempty"`
	AdditionalProperties *bool                 `json:"additionalProperties,omitempty"`
}
