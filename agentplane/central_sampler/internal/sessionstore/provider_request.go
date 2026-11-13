package sessionstore

import (
	"fmt"
	"net/http"
	"strings"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	"google.golang.org/protobuf/proto"
)

// ProviderRequestPreparation is the immutable input needed to prepare a native request after the
// Store has acquired its transaction locks and checked the current owner, interrupt, and pending
// intent state. The Store uses the locked Conversation for history and instructions.
type ProviderRequestPreparation struct {
	ModelConfig                samplersettings.ModelConfig
	SessionEffort              string
	SessionConfiguration       []byte
	Attribution                protocol.SessionAttribution
	Compaction                 bool
	AnthropicCompactionRequest *anthropic.Request
}

func requestPreparationFor(preparation *ProviderRequestPreparation) requestPreparation {
	if preparation == nil {
		return nil
	}
	return func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		return prepareProviderRequest(store, conversation, metadata, *preparation)
	}
}

func prepareProviderRequest(store *contentStore, conversation *Conversation, metadata ProviderRequest, preparation ProviderRequestPreparation) (ProviderRequest, []byte, error) {
	if metadata.API != preparation.ModelConfig.API || metadata.API != conversation.API {
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
	if preparation.Compaction && metadata.API == protocol.APIStyleAnthropicMessages {
		if preparation.AnthropicCompactionRequest == nil {
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		previous, err := store.previousRequest(conversation.ID)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		body := *preparation.AnthropicCompactionRequest
		if previous.ProfileID != "" {
			body, err = anthropic.FreezeRequest(store, previous.ProfileID, body)
			if err != nil {
				return ProviderRequest{}, nil, err
			}
		}
		profile, ids, err := anthropic.StoreRequest(store, body)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		return finishStoredRequest(store, metadata, profile, ids)
	}
	switch metadata.API {
	case protocol.APIStyleResponses:
		tools, err := toolcontract.ModelSurface(preparation.ModelConfig.ExecEncoding)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		params := responses.RequestParams{
			Model: preparation.ModelConfig.ProviderModel, Effort: preparation.SessionEffort,
			Attribution: preparation.Attribution, Instructions: conversation.Instructions, Tools: tools,
		}
		var body responses.Request
		if preparation.Compaction {
			body, err = responses.BuildCompactionRequest(params, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding)
		} else {
			body, err = responses.BuildRequest(params, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding)
		}
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		return prepareResponsesRequest(store, conversation, metadata, body, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding, preparation.Compaction)

	case protocol.APIStyleAnthropicMessages:
		config := preparation.ModelConfig.AnthropicMessages
		if config == nil {
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		effort := preparation.SessionEffort
		if config.PerMessageEffort {
			launch := &samplerpb.CreateSessionRequest{}
			if err := proto.Unmarshal(preparation.SessionConfiguration, launch); err != nil {
				return ProviderRequest{}, nil, err
			}
			effort = launch.GetSpec().GetReasoningEffort()
		}
		tools, err := toolcontract.ModelSurface(preparation.ModelConfig.ExecEncoding)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		context, ok := conversation.Context().(*anthropic.Context)
		if !ok {
			return ProviderRequest{}, nil, fmt.Errorf("Anthropic conversation context has type %T: %w", conversation.Context(), protocol.ErrInvalid)
		}
		params := anthropic.RequestParams{
			Context: context, Model: preparation.ModelConfig.ProviderModel, Effort: effort,
			Instructions: conversation.Instructions, Tools: tools,
		}
		params.Context = nil // Metadata only; the codec projects the stable history incrementally.
		body, err := anthropic.BuildRequest(params, *config, anthropic.PreviousMessageID(context))
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		return prepareAnthropicRequest(store, conversation, metadata, body)

	default:
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
}

func providerProjectionKey(config samplersettings.ModelConfig) requestProjectionKey {
	if config.API == protocol.APIStyleResponses {
		return requestProjectionKey(responsesProjectionVersion + string(config.Responses.Dialect) + "/" + string(config.ExecEncoding))
	}
	return anthropicProjectionKey
}

// ProviderRequestProjectionMatches reports whether a retained provider request uses the current
// route's history projection. The projection key remains owned by Store's durable model.
func ProviderRequestProjectionMatches(config samplersettings.ModelConfig, request *ProviderRequest) bool {
	return request != nil && providerProjectionKey(config) == request.ProjectionKey
}

// ProviderRequestHeadersWithoutAuthentication returns provider request headers safe to expose
// through the request-inspection RPC and dispatch event.
func ProviderRequestHeadersWithoutAuthentication(headers map[string]string) map[string]string {
	result := make(map[string]string, len(headers))
	for name, value := range headers {
		switch strings.ToLower(name) {
		case "authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token":
			continue
		}
		result[name] = value
	}
	return result
}

// ProviderRequestIntent builds only durable provider metadata. Authentication and network
// dispatch remain in the runtime adapter after this request is committed.
func ProviderRequestIntent(kind PendingIntentKind, connection samplersettings.ConnectionConfig, config samplersettings.ModelConfig) IntentPayload {
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
