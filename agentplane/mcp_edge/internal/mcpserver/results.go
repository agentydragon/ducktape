package mcpserver

import (
	"encoding/json"
	"strconv"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// structuredResult answers a view as structured content, with the same JSON as its one text block.
func structuredResult(view any) (*mcp.CallToolResult, error) {
	text, err := json.Marshal(view)
	if err != nil {
		return nil, err
	}
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: string(text)}},
		StructuredContent: json.RawMessage(text),
	}, nil
}

func seconds(value float64) string {
	return strconv.FormatFloat(value, 'g', -1, 64)
}
