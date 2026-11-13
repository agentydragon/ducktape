package centralsampler

import (
	"net/http"
	"strings"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
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
	description, err := toolcontract.ExecToolDescription(toolDefinitions(), encoding)
	if err != nil {
		return protocol.ToolSurface{}, err
	}
	parameters, err := toolcontract.JSONExecToolParameters()
	if err != nil {
		return protocol.ToolSurface{}, err
	}
	return protocol.ToolSurface{Description: description, Parameters: parameters, FreeformGrammar: toolcontract.ExecFreeformGrammar, WaitDescription: toolcontract.WaitToolDescription(), WaitParameters: toolcontract.WaitToolParameters()}, nil
}

func providerRequestIntent(kind PendingIntentKind, connection samplersettings.ConnectionConfig, config samplersettings.ModelConfig) IntentPayload {
	path := "/v1/responses"
	var headers map[string]string
	switch config.API {
	case protocol.APIStyleAnthropicMessages:
		path = "/v1/messages"
		headers = anthropic.Headers(*config.AnthropicMessages)
	case protocol.APIStyleResponses:
		headers = responses.Headers(config.Responses, kind == PendingIntentCompaction)
	}
	request := IntentPayload{Kind: kind, ProviderRequest: &ProviderRequest{
		API: config.API, Connection: config.Connection, ProviderModel: config.ProviderModel, Method: http.MethodPost,
		URL: strings.TrimRight(connection.BaseURL, "/") + path, Headers: headers,
	}}
	if kind == PendingIntentCompaction {
		request.ProjectionKey = providerProjectionKey(config)
		if config.API == protocol.APIStyleAnthropicMessages && config.AnthropicMessages.Compaction != nil {
			request.CompactionControl = &CompactionControl{
				KeepRecentTurns:  config.AnthropicMessages.Compaction.KeepRecentTurns,
				PerMessageEffort: config.AnthropicMessages.PerMessageEffort,
			}
		}
	}
	return request
}

func providerProjectionKey(config samplersettings.ModelConfig) requestProjectionKey {
	if config.API == protocol.APIStyleResponses {
		return requestProjectionKey(responsesProjectionVersion + string(config.Responses.Dialect) + "/" + string(config.ExecEncoding))
	}
	return anthropicProjectionKey
}
