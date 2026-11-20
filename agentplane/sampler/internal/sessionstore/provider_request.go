package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func requestPreparationFor(preparation *providerrequest.Preparation) requestPreparation {
	if preparation == nil {
		return nil
	}
	return func(ctx context.Context, tx *sql.Tx, store *contentdb.Store, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		return prepareProviderRequest(ctx, tx, store, conversation, metadata, *preparation)
	}
}

func prepareProviderRequest(ctx context.Context, tx *sql.Tx, store *contentdb.Store, conversation *Conversation, metadata ProviderRequest, preparation providerrequest.Preparation) (ProviderRequest, []byte, error) {
	if metadata.API != preparation.ModelConfig.API || metadata.API != conversation.API {
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
	var previousKind PendingIntentKind
	if metadata.API == protocol.APIStyleChatCompletions {
		previousKind = PendingIntentSampling
	}
	previous, err := previousRequest(ctx, tx, conversation.ID, previousKind)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	snapshot := providerrequest.Snapshot{API: conversation.API, PreviousProfileID: previous.ProfileID}
	if conversation.Instructions != nil {
		instructions := *conversation.Instructions
		snapshot.Instructions = &instructions
	}
	if (metadata.API == protocol.APIStyleResponses || metadata.API == protocol.APIStyleChatCompletions) && previous.ProfileID != "" {
		refs := previous.PrefixIDs
		if len(refs) == 0 {
			refs, err = store.LoadContext(previous.ContextID)
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
		case protocol.APIStyleChatCompletions:
			project = conversation.Context().(*chatcompletions.Context).ProjectRecords
		default:
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		metadata.SourceContextID, metadata.ContextID, err = conversation.projectHistory(ctx, tx, store, history.Key, project)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
	default:
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
	if err := store.Save(); err != nil {
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
	case protocol.APIStyleChatCompletions:
		path = chatcompletions.Path
		headers = chatcompletions.Headers()
	}
	request := IntentPayload{Kind: kind, ProviderRequest: &ProviderRequest{
		API: config.API, Connection: config.Connection, ProviderModel: config.ProviderModel, Method: http.MethodPost,
		URL: strings.TrimRight(connection.BaseURL, "/") + path, Headers: headers,
	}}
	if kind == PendingIntentCompaction {
		request.ProjectionKey = providerrequest.ProjectionFor(config)
		if config.API == protocol.APIStyleChatCompletions && config.ChatCompaction != nil {
			request.CompactionControl = &CompactionControl{KeepRecentTurns: config.ChatCompaction.KeepRecentTurns}
			if replacement := config.ChatCompaction.Replacement; replacement != nil {
				request.CompactionControl.ReplacementRole = replacement.Role
				request.CompactionControl.ReplacementTemplate = replacement.Template
			}
		}
		if config.API == protocol.APIStyleAnthropicMessages && config.AnthropicMessages.Compaction != nil {
			request.CompactionControl = &CompactionControl{
				KeepRecentTurns:  config.AnthropicMessages.Compaction.KeepRecentTurns,
				PerMessageEffort: config.AnthropicMessages.PerMessageEffort,
			}
		}
	}
	return request
}
