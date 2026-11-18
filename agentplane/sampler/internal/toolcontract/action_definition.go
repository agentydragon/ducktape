package toolcontract

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

// HostActionDefinition preserves the catalog schema verbatim in ALL_TOOLS. The pinned host
// accepts Codex's schema subset, so complete Action schemas travel in the description.
func HostActionDefinition(name string, tool protocol.ToolName, description string, schema json.RawMessage) (protocol.CodeToolDefinition, error) {
	name = normalizeCodeModeIdentifier(name)
	var input any
	if err := json.Unmarshal(schema, &input); err != nil {
		return protocol.CodeToolDefinition{}, err
	}
	description += "\n\nAction input JSON Schema:\n```json\n" + string(schema) + "\n```"
	sample, err := renderCodeModeSample(name, description, "function", input, nil)
	if err != nil {
		return protocol.CodeToolDefinition{}, err
	}
	return protocol.CodeToolDefinition{Name: name, ToolName: tool, Description: sample, Kind: "function"}, nil
}
