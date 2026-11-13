package mcpexec

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// upstream is an MCP server in this process that an Executor reaches over streamable HTTP.
type upstream struct {
	http *httptest.Server
	// server answers each new session; a stateless upstream answers every request with the one it holds then.
	server atomic.Pointer[mcp.Server]
	// down answers every request 503 while it is set.
	down atomic.Bool
	// listing, while set, holds the next tools/list request until the test releases it.
	listing atomic.Pointer[gate]
	// ended counts the requests that ended a session: streamable HTTP's DELETE.
	ended atomic.Int32

	mu sync.Mutex
	// headers are those of every request that reached the upstream.
	headers []http.Header
}

func newUpstream(t *testing.T, server *mcp.Server, options *mcp.StreamableHTTPOptions) *upstream {
	t.Helper()
	u := &upstream{}
	u.server.Store(server)
	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return u.server.Load() }, options)
	u.http = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.mu.Lock()
		u.headers = append(u.headers, r.Header.Clone())
		u.mu.Unlock()
		if r.Method == http.MethodDelete {
			u.ended.Add(1)
		}
		if u.down.Load() {
			http.Error(w, "the test upstream is down", http.StatusServiceUnavailable)
			return
		}
		if held := u.listing.Load(); held != nil && method(t, r) == "tools/list" && u.listing.CompareAndSwap(held, nil) {
			if !held.hold(r.Context()) {
				return
			}
		}
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(func() {
		// A session left open would hold Close until the test times out, hiding the failure that left it.
		u.http.CloseClientConnections()
		u.http.Close()
	})
	return u
}

// url is the upstream's MCP endpoint.
func (u *upstream) url() string { return u.http.URL + "/mcp" }

// holdNextList holds the next tools/list request at the gate it returns.
func (u *upstream) holdNextList() *gate {
	g := newGate()
	u.listing.Store(g)
	return g
}

// received are the headers of every request so far.
func (u *upstream) received() []http.Header {
	u.mu.Lock()
	defer u.mu.Unlock()
	return slices.Clone(u.headers)
}

// method is the JSON-RPC method a request posts; empty for any other request. The body is left for the handler.
func method(t *testing.T, r *http.Request) string {
	if r.Method != http.MethodPost {
		return ""
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		t.Error(err)
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	var message struct {
		Method string `json:"method"`
	}
	if json.Unmarshal(body, &message) != nil {
		return ""
	}
	return message.Method
}

func newServer() *mcp.Server {
	return mcp.NewServer(&mcp.Implementation{Name: "test-upstream", Version: "v0.0.1"}, nil)
}

// objectSchema is the input schema of a tool that takes any object.
var objectSchema = json.RawMessage(`{"type": "object"}`)

// textSchema is the input schema of a tool that takes a text.
var textSchema = json.RawMessage(`{"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
	"additionalProperties": false}`)

// calls counts the calls of a tool and keeps their arguments.
type calls struct {
	mu        sync.Mutex
	arguments []json.RawMessage
}

func (c *calls) record(arguments json.RawMessage) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.arguments = append(c.arguments, arguments)
}

func (c *calls) made() []json.RawMessage {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.arguments)
}

// answering is a tool handler that records each call and answers it with result.
func answering(c *calls, result *mcp.CallToolResult) mcp.ToolHandler {
	return func(_ context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		c.record(request.Params.Arguments)
		return result, nil
	}
}

func textResult(text string) *mcp.CallToolResult {
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: text}}}
}

// stdioServerVariable makes this test binary an MCP server on its standard input and output.
const stdioServerVariable = "MCPEXEC_TEST_STDIO_SERVER"

// serveStdio serves test_environment, which answers the names of the variables of the server's environment.
func serveStdio() {
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_environment", InputSchema: objectSchema},
		func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			var names []string
			for _, variable := range os.Environ() {
				name, _, _ := bytes.Cut([]byte(variable), []byte("="))
				names = append(names, string(name))
			}
			return &mcp.CallToolResult{Content: []mcp.Content{}, StructuredContent: map[string]any{"names": names}}, nil
		})
	// Run ends when the executor closes the session, which is all a test needs of it.
	_ = server.Run(context.Background(), &mcp.StdioTransport{})
}
