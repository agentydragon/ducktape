package providerrequest

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"google.golang.org/protobuf/proto"
)

// Content is the transaction-local codec access; it cannot commit or dispatch.
type Content interface {
	content.ContentReader
	content.ContentWriter
}

// Snapshot contains only values read under the conversation lock. No mutable
// conversation, native context, projection cache or transaction escapes to the builder.
type Snapshot struct {
	API               protocol.APIStyle
	Instructions      *string
	PreviousProfileID content.ContentID
	PreviousFirstID   content.ContentID
	PreviousMessageID string
}

type ProjectionKey string

const (
	anthropicProjectionKey     ProjectionKey = "anthropic_messages/v1"
	chatProjectionKey          ProjectionKey = "chat_completions/v1"
	responsesProjectionVersion ProjectionKey = "responses/v1/"
)

// History is either an incremental projection of the locked native context or
// a complete compaction context. Store resolves and persists it before dispatch.
type (
	History    interface{ requestHistory() }
	Projection struct{ Key ProjectionKey }
	Complete   struct{ IDs []content.ContentID }
)

func (Projection) requestHistory() {}
func (Complete) requestHistory()   {}

type Recipe struct {
	ProfileID content.ContentID
	PrefixIDs []content.ContentID
	SuffixIDs []content.ContentID
	History   History
}

// Preparation is the immutable input needed to prepare a native request after the
// Store has acquired its transaction locks and checked the current owner, interrupt, and pending
// intent state. Snapshot carries the locked values used by native assembly.
type Preparation struct {
	ModelConfig                samplersettings.ModelConfig
	SessionEffort              string
	SessionConfiguration       []byte
	Attribution                protocol.SessionAttribution
	Compaction                 bool
	AnthropicCompactionRequest *anthropic.Request
	ChatCompactionPlan         *chatcompletions.CompactionPlan
}

func Prepare(store Content, snapshot Snapshot, preparation Preparation) (Recipe, error) {
	if snapshot.API != preparation.ModelConfig.API {
		return Recipe{}, protocol.ErrInvalid
	}
	if preparation.Compaction && snapshot.API == protocol.APIStyleAnthropicMessages {
		if preparation.AnthropicCompactionRequest == nil {
			return Recipe{}, protocol.ErrInvalid
		}
		body := *preparation.AnthropicCompactionRequest
		var err error
		if snapshot.PreviousProfileID != "" {
			body, err = anthropic.FreezeRequest(store, snapshot.PreviousProfileID, body)
			if err != nil {
				return Recipe{}, err
			}
		}
		profile, ids, err := anthropic.StoreRequest(store, body)
		if err != nil {
			return Recipe{}, err
		}
		return Recipe{ProfileID: profile, History: Complete{IDs: ids}}, nil
	}
	switch snapshot.API {
	case protocol.APIStyleChatCompletions:
		if preparation.Compaction {
			if preparation.ChatCompactionPlan == nil || preparation.ModelConfig.ChatCompaction == nil {
				return Recipe{}, protocol.ErrPrecondition
			}
			profile, ids, err := preparation.ChatCompactionPlan.StoreRequest(store, preparation.ModelConfig.ProviderModel, preparation.ModelConfig.ChatCompaction.MaxTokens)
			if err != nil {
				return Recipe{}, err
			}
			return Recipe{ProfileID: profile, History: Complete{IDs: ids}}, nil
		}
		profile, system := snapshot.PreviousProfileID, snapshot.PreviousFirstID
		if profile == "" {
			tools, err := toolcontract.ModelSurface(preparation.ModelConfig.ExecEncoding)
			if err != nil {
				return Recipe{}, err
			}
			profile, system, err = chatcompletions.PrepareRequest(store, preparation.ModelConfig.ProviderModel, snapshot.Instructions, tools)
			if err != nil {
				return Recipe{}, err
			}
		} else {
			var err error
			profile, err = chatcompletions.WithStreamUsage(store, profile)
			if err != nil {
				return Recipe{}, err
			}
		}
		if system == "" {
			return Recipe{}, protocol.ErrInvalid
		}
		return Recipe{ProfileID: profile, PrefixIDs: []content.ContentID{system}, History: Projection{Key: chatProjectionKey}}, nil
	case protocol.APIStyleResponses:
		tools, err := toolcontract.ModelSurface(preparation.ModelConfig.ExecEncoding)
		if err != nil {
			return Recipe{}, err
		}
		params := responses.RequestParams{
			Model: preparation.ModelConfig.ProviderModel, Effort: preparation.SessionEffort,
			Attribution: preparation.Attribution, Instructions: snapshot.Instructions, Tools: tools,
		}
		var body responses.Request
		if preparation.Compaction {
			body, err = responses.BuildCompactionRequest(params, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding)
		} else {
			body, err = responses.BuildRequest(params, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding)
		}
		if err != nil {
			return Recipe{}, err
		}
		return prepareResponses(store, snapshot, body, preparation.ModelConfig.Responses, preparation.ModelConfig.ExecEncoding, preparation.Compaction)

	case protocol.APIStyleAnthropicMessages:
		config := preparation.ModelConfig.AnthropicMessages
		if config == nil {
			return Recipe{}, protocol.ErrInvalid
		}
		effort := preparation.SessionEffort
		if config.PerMessageEffort {
			launch := &sessionconfig.Configuration{}
			if err := proto.Unmarshal(preparation.SessionConfiguration, launch); err != nil {
				return Recipe{}, err
			}
			effort = launch.GetSpec().GetReasoningEffort()
		}
		tools, err := toolcontract.ModelSurface(preparation.ModelConfig.ExecEncoding)
		if err != nil {
			return Recipe{}, err
		}
		params := anthropic.RequestParams{
			Model: preparation.ModelConfig.ProviderModel, Effort: effort,
			Instructions: snapshot.Instructions, Tools: tools,
		}
		body, err := anthropic.BuildRequest(params, *config, snapshot.PreviousMessageID)
		if err != nil {
			return Recipe{}, err
		}
		return prepareAnthropic(store, snapshot, body)

	default:
		return Recipe{}, protocol.ErrInvalid
	}
}

func prepareResponses(s Content, snapshot Snapshot, body responses.Request, config responses.Config, encoding protocol.ExecEncoding, compact bool) (Recipe, error) {
	var metadata Recipe
	var err error
	if snapshot.PreviousProfileID != "" {
		body, err = responses.FreezeRequest(s, snapshot.PreviousProfileID, body)
		if err != nil {
			return Recipe{}, err
		}
		if snapshot.PreviousFirstID != "" {
			body, err = responses.FreezeToolDeclaration(s, snapshot.PreviousFirstID, body)
			if err != nil {
				return Recipe{}, err
			}
		}
	}
	if compact {
		if len(body.Input) == 0 {
			return Recipe{}, protocol.ErrInvalid
		}
		raw, err := json.Marshal(body.Input[len(body.Input)-1])
		if err != nil {
			return Recipe{}, err
		}
		id, err := responses.StoreRecord(s, raw)
		if err != nil {
			return Recipe{}, err
		}
		metadata.SuffixIDs = []content.ContentID{id}
		body.Input = body.Input[:len(body.Input)-1]
	}
	for _, input := range body.Input {
		raw, err := json.Marshal(input)
		if err != nil {
			return Recipe{}, err
		}
		id, err := responses.StoreRecord(s, raw)
		if err != nil {
			return Recipe{}, err
		}
		metadata.PrefixIDs = append(metadata.PrefixIDs, id)
	}
	body.Input = nil
	metadata.ProfileID, _, err = responses.StoreRequest(s, body)
	if err != nil {
		return Recipe{}, err
	}
	metadata.History = Projection{Key: responsesProjectionVersion + ProjectionKey(config.Dialect) + "/" + ProjectionKey(encoding)}
	return metadata, nil
}

func prepareAnthropic(s Content, snapshot Snapshot, body anthropic.Request) (Recipe, error) {
	var metadata Recipe
	var err error
	if snapshot.PreviousProfileID != "" {
		body, err = anthropic.FreezeRequest(s, snapshot.PreviousProfileID, body)
		if err != nil {
			return Recipe{}, err
		}
	}
	body.Messages = []json.RawMessage{}
	metadata.ProfileID, _, err = anthropic.StoreRequest(s, body)
	if err != nil {
		return Recipe{}, err
	}
	metadata.History = Projection{Key: anthropicProjectionKey}
	return metadata, nil
}

func ProjectionFor(config samplersettings.ModelConfig) ProjectionKey {
	if config.API == protocol.APIStyleResponses {
		return responsesProjectionVersion + ProjectionKey(config.Responses.Dialect) + "/" + ProjectionKey(config.ExecEncoding)
	}
	if config.API == protocol.APIStyleChatCompletions {
		return chatProjectionKey
	}
	return anthropicProjectionKey
}
