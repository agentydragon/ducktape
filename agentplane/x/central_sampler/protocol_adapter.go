package centralsampler

import (
	"context"
	"encoding/json"
	"net/http"

	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"google.golang.org/protobuf/proto"
)

type sampleResult struct {
	Records       []json.RawMessage
	Audit         map[string]any
	ContextTokens *int
}

type protocolAdapter interface {
	API() APIStyle
	Prepare(*Conversation, *Session, *ExecutionState, SessionAttribution) (any, error)
	Sample(context.Context, *http.Client, ConnectionConfig, any, []byte, *ResponseEvidence) (*sampleResult, error)
	AutoCompactTokens(*int) *int
	Compact(context.Context, *Runtime, Owner, string, *Conversation, SessionAttribution, *ExecutionState) error
}

type (
	responsesAdapter struct{ config ModelConfig }
	anthropicAdapter struct{ config ModelConfig }
)

func adapterFor(config ModelConfig) (protocolAdapter, error) {
	switch config.API {
	case APIStyleResponses:
		return responsesAdapter{config}, nil
	case APIStyleAnthropicMessages:
		return anthropicAdapter{config}, nil
	default:
		return nil, ErrInvalid
	}
}

func (a responsesAdapter) API() APIStyle { return APIStyleResponses }
func (a anthropicAdapter) API() APIStyle { return APIStyleAnthropicMessages }

func (a responsesAdapter) Prepare(c *Conversation, s *Session, state *ExecutionState, attribution SessionAttribution) (any, error) {
	return BuildResponsesRequest(ResponsesRequestParams{
		Model: a.config.ProviderModel, Effort: s.Effort, Attribution: attribution,
		Items: c.Context().(*ResponsesContext).Items, Instructions: c.Instructions, ExecutionState: state,
	}, a.config)
}

func (a anthropicAdapter) Prepare(c *Conversation, s *Session, state *ExecutionState, attribution SessionAttribution) (any, error) {
	effort := s.Effort
	if a.config.AnthropicMessages.PerMessageEffort {
		launch := &samplerpb.OpenSessionRequest{}
		if err := proto.Unmarshal(s.Configuration, launch); err != nil {
			return nil, err
		}
		effort = launch.GetSpec().GetReasoningEffort()
	}
	return BuildAnthropicRequest(AnthropicRequestParams{
		Context: c.Context().(*AnthropicContext), Model: a.config.ProviderModel, Effort: effort,
		Instructions: c.Instructions,
	}, a.config, anthropicPreviousMessageID(c.Archive.(*AnthropicContext)))
}

func (a responsesAdapter) Sample(ctx context.Context, client *http.Client, connection ConnectionConfig, body any, _ []byte, evidence *ResponseEvidence) (*sampleResult, error) {
	credential, err := connection.Auth.ReadCredential()
	if err != nil {
		return nil, &KnownFailureError{Evidence: map[string]any{"reason": "provider credential unavailable", "error": err.Error(), "dispatched": false}}
	}
	headers, err := evidence.Store.ProviderHeaders(ctx, evidence.Owner)
	if err != nil {
		return nil, err
	}
	response, err := Sample(ctx, client, connection.BaseURL, credential, body.(ResponsesRequest), a.config.Responses, evidence, headers, false)
	if err != nil {
		return nil, err
	}
	records, err := (&ResponsesContext{Items: response.Output}).Records()
	if err != nil {
		return nil, err
	}
	return &sampleResult{Records: records, Audit: response.Audit(), ContextTokens: response.Terminal.TotalTokens()}, nil
}

func (a anthropicAdapter) Sample(ctx context.Context, client *http.Client, connection ConnectionConfig, _ any, wire []byte, evidence *ResponseEvidence) (*sampleResult, error) {
	response, err := SampleAnthropic(ctx, client, connection, a.config, wire, evidence, false)
	if err != nil {
		return nil, err
	}
	return &sampleResult{Records: []json.RawMessage{response.Message}, Audit: response.Audit(), ContextTokens: response.ContextTokens}, nil
}

func (a responsesAdapter) AutoCompactTokens(fallback *int) *int { return fallback }
func (a anthropicAdapter) AutoCompactTokens(_ *int) *int {
	if a.config.AnthropicMessages.Compaction == nil {
		return nil
	}
	return a.config.AnthropicMessages.Compaction.AutoCompactTokens
}

func (a responsesAdapter) Compact(ctx context.Context, runtime *Runtime, owner Owner, model string, conversation *Conversation, metadata SessionAttribution, state *ExecutionState) error {
	return runtime.compactResponses(ctx, owner, model, conversation, metadata, state)
}

func (a anthropicAdapter) Compact(ctx context.Context, runtime *Runtime, owner Owner, model string, conversation *Conversation, metadata SessionAttribution, state *ExecutionState) error {
	return runtime.compactAnthropic(ctx, owner, model, conversation, metadata, state)
}
