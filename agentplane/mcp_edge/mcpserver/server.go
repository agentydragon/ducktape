// Package mcpserver serves the standalone edge's /mcp endpoint to callers on an external Connection: the Action
// tools and the caller's direct tools over stateless Streamable HTTP. It reaches the Action Service only through its
// workload API.
package mcpserver

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// toolsJSON is the tool definitions: names, descriptions, input schemas and annotations.
// The Go MCP contract tests hold the listed generic tools to this file.
//
//go:embed tools.json
var toolsJSON []byte

type toolDefinition struct {
	Name        string               `json:"name"`
	Title       string               `json:"title"`
	Description string               `json:"description"`
	InputSchema json.RawMessage      `json:"inputSchema"`
	Annotations *mcp.ToolAnnotations `json:"annotations"`
}

// Options configures the /mcp handler.
type Options struct {
	// MaxWaitSeconds bounds wait.wait_seconds.
	MaxWaitSeconds float64
	// DirectWaitSeconds is how long a direct tool call waits for its Action to finish before answering with the
	// request to wait on; at most MaxWaitSeconds.
	DirectWaitSeconds float64
	// ResourceMetadataURL is the protected resource metadata a refused request's challenge names; empty names none.
	ResourceMetadataURL string
	Logger              *slog.Logger
}

// NewHandler serves /mcp. A request needs a bearer the verifier accepts; the tools call core with that request's
// context, which carries the verified auth.TokenInfo, and attach no credential of their own. Cross-origin browser
// requests are refused, and go-sdk's DNS-rebinding protection guards a loopback listener.
func NewHandler(core pb.ActionServiceClient, verifier auth.TokenVerifier, options Options) http.Handler {
	if options.Logger == nil {
		options.Logger = slog.Default()
	}
	t := &tools{
		core:              core,
		maxWaitSeconds:    options.MaxWaitSeconds,
		directWaitSeconds: min(options.DirectWaitSeconds, options.MaxWaitSeconds),
	}
	schemaCache := mcp.NewSchemaCache()
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Stateless HTTP creates a fresh SDK server for each request. Snapshot the catalog with this authenticated
		// request's context; the SDK's name-based cursor can continue on a later request without a server-side session.
		catalog, err := core.ListActionGroups(r.Context(), &pb.ListActionGroupsRequest{})
		if err != nil {
			options.Logger.WarnContext(r.Context(), "listing direct MCP tools failed; serving generic tools only", "error", err)
			catalog = nil
		}
		server, err := newServer(t, options, schemaCache, catalog)
		if err != nil {
			options.Logger.ErrorContext(r.Context(), "registering direct MCP tools failed", "error", err)
			w.Header().Set("Retry-After", "60")
			http.Error(w, "MCP tool metadata is temporarily unavailable", http.StatusServiceUnavailable)
			return
		}
		transport := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, &mcp.StreamableHTTPOptions{
			Stateless: true,
			Logger:    options.Logger,
		})
		transport.ServeHTTP(w, r)
	})
	return http.NewCrossOriginProtection().Handler(
		requireCaller(verifier, options.ResourceMetadataURL, options.Logger, withTransportContext(handler)),
	)
}

func instructions(maxWaitSeconds float64) string {
	return "Discover Action identifiers, fetch details only when needed, then submit each request once under a fresh " +
		"idempotency key. A pending receipt is not execution success. A repeated key is refused; recover a lost " +
		"response with get_action_request(idempotency_key=...), never with a replacement key. This instance allows " +
		"wait.wait_seconds up to " + seconds(maxWaitSeconds) + " seconds."
}

func newServer(t *tools, options Options, schemaCache *mcp.SchemaCache, catalog *pb.ListActionGroupsResponse) (*mcp.Server, error) {
	server := mcp.NewServer(&mcp.Implementation{Name: "Agentplane Actions"}, &mcp.ServerOptions{
		Instructions: instructions(options.MaxWaitSeconds),
		Logger:       options.Logger,
		SchemaCache:  schemaCache,
	})
	var definitions []toolDefinition
	if err := json.Unmarshal(toolsJSON, &definitions); err != nil {
		panic(fmt.Sprintf("mcpserver: tools.json: %v", err))
	}
	known := map[string]toolDefinition{}
	for _, definition := range definitions {
		known[definition.Name] = definition
	}
	logger := options.Logger
	addTool(server, known["list_actions"], logger, t.listActions)
	addTool(server, known["get_action"], logger, t.getAction)
	addTool(server, known["get_action_policy"], logger, t.getActionPolicy)
	addTool(server, known["request_action"], logger, t.requestAction)
	addTool(server, known["get_action_request"], logger, t.getActionRequest)
	addTool(server, known["get_action_result"], logger, t.getActionResult)
	addTool(server, known["cancel_action_request"], logger, t.cancelActionRequest)
	addTool(server, known["list_action_request_events"], logger, t.listActionRequestEvents)
	direct, err := t.registerDirectTools(server, catalog, logger)
	if err != nil {
		return nil, err
	}
	server.AddReceivingMiddleware(t.directTools(logger, direct))
	return server, nil
}

// addTool registers one tool. The tool runs on a context a disconnect does not cancel, so a call that changes state
// finishes: only a wait ends with its caller.
func addTool[In any](server *mcp.Server, definition toolDefinition, logger *slog.Logger,
	handle func(context.Context, In) (*mcp.CallToolResult, error),
) {
	tool := &mcp.Tool{
		Name:        definition.Name,
		Title:       definition.Title,
		Description: definition.Description,
		InputSchema: definition.InputSchema,
		Annotations: definition.Annotations,
	}
	mcp.AddTool(server, tool, func(ctx context.Context, _ *mcp.CallToolRequest, input In) (*mcp.CallToolResult, any, error) {
		result, err := handle(context.WithoutCancel(ctx), input)
		if err != nil {
			return nil, nil, toolFailure(ctx, logger, definition.Name, err)
		}
		return result, nil, nil
	})
}

// toolFailure is what the caller reads of a failed tool call: a toolError as written, and any other failure masked
// after it is logged.
func toolFailure(ctx context.Context, logger *slog.Logger, tool string, err error) error {
	var refusal toolError
	switch {
	case errors.As(err, &refusal):
		return refusal
	case errors.Is(err, errDisconnected):
		logger.InfoContext(ctx, "caller disconnected during a wait", "tool", tool)
	default:
		logger.ErrorContext(ctx, "tool call failed", "tool", tool, "error", err)
	}
	return maskedError(tool)
}
