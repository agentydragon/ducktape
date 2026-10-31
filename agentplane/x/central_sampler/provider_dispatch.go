package centralsampler

import (
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
)

func toolDefinitions() []protocol.CodeToolDefinition {
	return []protocol.CodeToolDefinition{
		execCommandToolDefinition(), writeStdinToolDefinition(),
		{Name: "apply_patch", ToolName: protocol.ToolName{Name: "apply_patch"}, Description: "Apply a Codex *** Begin Patch / *** End Patch patch through upstream apply-patch.", Kind: "freeform"},
		viewImageToolDefinition(),
		// TODO: update_plan and Action-backed (MCP) tools, which Codex also nests.
	}
}

func modelToolSurface(encoding protocol.ExecEncoding) (protocol.ToolSurface, error) {
	description, err := execToolDescription(toolDefinitions(), encoding)
	if err != nil {
		return protocol.ToolSurface{}, err
	}
	parameters, err := jsonExecToolParameters()
	if err != nil {
		return protocol.ToolSurface{}, err
	}
	return protocol.ToolSurface{Description: description, Parameters: parameters, FreeformGrammar: execFreeformGrammar, WaitDescription: waitToolDescription(), WaitParameters: waitToolParameters()}, nil
}

func providerRequestIntent(kind PendingIntentKind, connection ConnectionConfig, config ModelConfig, wire []byte) IntentRequest {
	path := "/v1/responses"
	var headers map[string]string
	switch config.API {
	case protocol.APIStyleAnthropicMessages:
		path = "/v1/messages"
		headers = anthropic.Headers(*config.AnthropicMessages)
	case protocol.APIStyleResponses:
		headers = responses.Headers(config.Responses, kind == PendingIntentCompaction)
	}
	return IntentRequest{Kind: kind, ProviderRequest: &ProviderRequest{
		API: config.API, Connection: config.Connection, ProviderModel: config.ProviderModel, Method: http.MethodPost,
		URL: strings.TrimRight(connection.BaseURL, "/") + path, Headers: headers, Body: wire, WireRequest: string(wire),
	}}
}
