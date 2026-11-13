package mcpserver

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/action_service/docs"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type getGuideInput struct {
	Name docs.Name `json:"name"`
}

func (t *tools) getGuide(_ context.Context, input getGuideInput) (*mcp.CallToolResult, error) {
	guide, err := docs.Read(input.Name)
	if errors.Is(err, docs.ErrUnknownGuide) {
		return nil, toolError(`Unknown guide; use get_guide(name="index") to discover the published guides.`)
	}
	if err != nil {
		return nil, err
	}
	return structuredResult(guide)
}
