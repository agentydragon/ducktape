package mcpexec

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestGmailThreadSummary(t *testing.T) {
	const raw = `{"id":"thread-1","messages":[{"snippet":"private body","payload":{"headers":[{"name":"sUbJeCt","value":"Deployment review"}]}}]}`
	var structured any
	if err := json.Unmarshal([]byte(raw), &structured); err != nil {
		t.Fatal(err)
	}
	for name, response := range map[string]*mcp.CallToolResult{
		"structured": {StructuredContent: structured},
		"text":       {Content: []mcp.Content{&mcp.TextContent{Text: raw}}},
	} {
		t.Run(name, func(t *testing.T) {
			got := gmailThreadSummary("thread-1", response)
			if got.GetThreadId() != "thread-1" || got.GetSubject() != "Deployment review" {
				t.Errorf("summary = %v", got)
			}
			if got := gmailThreadSummary("wrong-thread", response); got != nil {
				t.Errorf("mismatched thread produced %v", got)
			}
		})
	}
}

func TestTanaNodeSummary(t *testing.T) {
	response := &mcp.CallToolResult{Content: []mcp.Content{
		&mcp.TextContent{Text: "- Different node <!-- node-id: draft-2 -->\n  - [x] Draft notes <!-- node-id: draft -->\nPrivate description"},
	}}
	got := tanaNodeSummary("draft", response)
	if got.GetNodeId() != "draft" || got.GetName() != "Draft notes" {
		t.Errorf("summary = %v", got)
	}
	if got := tanaNodeSummary("not-returned", response); got != nil {
		t.Errorf("missing node produced %v", got)
	}
}

func TestTanaSummariesRetryHTTPFailureAndKeepPartialResults(t *testing.T) {
	var validNodeCalls, missingNodeCalls atomic.Int32
	server := mcp.NewServer(&mcp.Implementation{Name: "tana-preview-test"}, nil)
	server.AddTool(&mcp.Tool{Name: "read_node", InputSchema: json.RawMessage(`{
		"type": "object",
		"properties": {"nodeId": {"type": "string"}, "maxDepth": {"const": 0}},
		"required": ["nodeId", "maxDepth"],
		"additionalProperties": false
	}`)}, func(_ context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		var args struct {
			NodeID string `json:"nodeId"`
		}
		if err := json.Unmarshal(req.Params.Arguments, &args); err != nil {
			return nil, err
		}
		if args.NodeID != "draft" {
			missingNodeCalls.Add(1)
			return &mcp.CallToolResult{IsError: true}, nil
		}
		return &mcp.CallToolResult{Content: []mcp.Content{
			&mcp.TextContent{Text: "- Draft notes <!-- node-id: draft -->"},
		}}, nil
	})
	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			body, err := io.ReadAll(r.Body)
			if err != nil {
				http.Error(w, "cannot read MCP request", http.StatusBadRequest)
				return
			}
			var call struct {
				Params struct {
					Arguments struct {
						NodeID string `json:"nodeId"`
					}
				}
			}
			if json.Unmarshal(body, &call) == nil && call.Params.Arguments.NodeID == "draft" && validNodeCalls.Add(1) == 1 {
				http.Error(w, "injected temporary MCP failure", http.StatusInternalServerError)
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
		}
		handler.ServeHTTP(w, r)
	}))
	defer upstream.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "preview-client"}, nil)
	connected, err := client.Connect(t.Context(), &mcp.StreamableClientTransport{Endpoint: upstream.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer connected.Close()
	executor := &Executor{key: "tana", available: true, current: &session{client: connected}}
	got := executor.TanaNodeSummaries(t.Context(), []string{"missing", "draft"})
	if len(got) != 1 || got[0].GetName() != "Draft notes" {
		t.Errorf("Tana summaries = %v", got)
	}
	if got := validNodeCalls.Load(); got != 2 {
		t.Errorf("valid node was looked up %d times, want 2", got)
	}
	if got := missingNodeCalls.Load(); got != 1 {
		t.Errorf("missing node was looked up %d times, want 1", got)
	}
}
