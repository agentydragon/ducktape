package protocol

import "encoding/json"

// ToolSurface describes the sampler-owned executor contract for native request rendering.
type ToolSurface struct {
	Description     string
	Parameters      json.RawMessage
	FreeformGrammar string
	WaitDescription string
	WaitParameters  json.RawMessage
}
