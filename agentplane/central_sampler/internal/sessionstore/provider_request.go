package sessionstore

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/providerrequest"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
)

func requestPreparationFor(preparation *providerrequest.Preparation) requestPreparation {
	if preparation == nil {
		return nil
	}
	return func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		return prepareProviderRequest(store, conversation, metadata, *preparation)
	}
}

func prepareProviderRequest(store *contentStore, conversation *Conversation, metadata ProviderRequest, preparation providerrequest.Preparation) (ProviderRequest, []byte, error) {
	if metadata.API != preparation.ModelConfig.API || metadata.API != conversation.API {
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
	previous, err := store.previousRequest(conversation.ID)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	snapshot := providerrequest.Snapshot{API: conversation.API, PreviousProfileID: previous.ProfileID}
	if conversation.Instructions != nil {
		instructions := *conversation.Instructions
		snapshot.Instructions = &instructions
	}
	if metadata.API == protocol.APIStyleResponses && previous.ProfileID != "" {
		refs := previous.PrefixIDs
		if len(refs) == 0 {
			refs, err = store.loadContext(previous.ContextID)
			if err != nil {
				return ProviderRequest{}, nil, err
			}
		}
		if len(refs) > 0 {
			snapshot.PreviousFirstID = refs[0]
		}
	}
	if metadata.API == protocol.APIStyleAnthropicMessages {
		source, ok := conversation.Context().(*anthropic.Context)
		if !ok {
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		snapshot.PreviousMessageID = anthropic.PreviousMessageID(source)
	}
	recipe, err := providerrequest.Prepare(store, snapshot, preparation)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	metadata.ProfileID = recipe.ProfileID
	metadata.PrefixIDs = append(metadata.PrefixIDs, recipe.PrefixIDs...)
	if recipe.SuffixIDs != nil {
		metadata.SuffixIDs = recipe.SuffixIDs
	}
	switch history := recipe.History.(type) {
	case providerrequest.Complete:
		return finishStoredRequest(store, metadata, recipe.ProfileID, history.IDs)
	case providerrequest.Projection:
		metadata.ProjectionKey = history.Key
		var project func(int) ([]json.RawMessage, error)
		switch metadata.API {
		case protocol.APIStyleResponses:
			source := conversation.Context().(*responses.Context)
			project = func(start int) ([]json.RawMessage, error) {
				return source.ProjectRecords(start, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding)
			}
		case protocol.APIStyleAnthropicMessages:
			project = conversation.Context().(*anthropic.Context).ProjectRecords
		default:
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		metadata.SourceContextID, metadata.ContextID, err = store.projectHistory(conversation, history.Key, project)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
	default:
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
	if err := store.save(); err != nil {
		return ProviderRequest{}, nil, err
	}
	wire, err := renderProviderRequest(store, metadata)
	return metadata, wire, err
}

// ProviderRequestProjectionMatches reports whether a retained provider request uses the current
// route's history projection. The builder owns the projection vocabulary; Store retains its committed key.
func ProviderRequestProjectionMatches(config samplersettings.ModelConfig, request *ProviderRequest) bool {
	return request != nil && providerrequest.ProjectionFor(config) == request.ProjectionKey
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
		request.ProjectionKey = providerrequest.ProjectionFor(config)
		if config.API == protocol.APIStyleAnthropicMessages && config.AnthropicMessages.Compaction != nil {
			request.CompactionControl = &CompactionControl{
				KeepRecentTurns:  config.AnthropicMessages.Compaction.KeepRecentTurns,
				PerMessageEffort: config.AnthropicMessages.PerMessageEffort,
			}
		}
	}
	return request
}
