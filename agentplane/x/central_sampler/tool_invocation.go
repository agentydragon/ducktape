package centralsampler

import "strings"

// ToolInvocation is a provider-neutral projection of a model tool call. SourceRecord is the
// one-based position in the native conversation, so an adapter can answer the originating call
// without embedding provider-specific item types in the execution layer.
type ToolInvocation struct {
	Encoding     ExecEncoding `json:"encoding"`
	CallID       string       `json:"call_id"`
	Name         string       `json:"name"`
	Input        string       `json:"input"`
	SourceRecord int          `json:"source_record,omitempty"`
}

func (call ToolInvocation) HasValidShape() bool {
	return (call.Encoding == ExecEncodingJSON || call.Encoding == ExecEncodingFreeform) &&
		strings.TrimSpace(call.CallID) != "" && strings.TrimSpace(call.Name) != ""
}

func (call ToolInvocation) IsConfiguredToolName(config ModelConfig) bool {
	if config.ExecEncoding == ExecEncodingJSON {
		return call.Name == "exec"
	}
	return call.Name == "exec" || call.Name == "wait"
}

// IsModelTool checks the exact tool surface declared for this model route.
func (call ToolInvocation) IsModelTool(config ModelConfig) bool {
	if config.ExecEncoding == ExecEncodingJSON {
		return call.Name == "exec" && call.Encoding == ExecEncodingJSON
	}
	return call.Name == "exec" && call.Encoding == ExecEncodingFreeform ||
		call.Name == "wait" && call.Encoding == ExecEncodingJSON
}

func unsupportedToolCallMessage(call ToolInvocation) string {
	if call.Encoding == ExecEncodingFreeform {
		return "unsupported custom tool call: " + call.Name
	}
	return "unsupported call: " + call.Name
}
