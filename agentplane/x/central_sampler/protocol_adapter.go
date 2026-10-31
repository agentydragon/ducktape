package centralsampler

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"google.golang.org/protobuf/proto"
)

type sampleResult struct {
	Records []json.RawMessage
	// Audit is the adapter's stored intent result: *responses.Audit or *anthropic.Audit.
	Audit         any
	ContextTokens *int
}

type compactionPlan struct {
	// Body is the adapter's request: responses.Request or anthropic.Request.
	Body  any
	Apply func(*sampleResult) (protocol.Context, error)
}

// These adapters compose native packages with sampler-owned session and route policy.
// Native packages consume snapshots and an evidence sink, never a Store or Runtime.
type protocolAdapter interface {
	API() protocol.APIStyle
	Prepare(*Conversation, *Session, *ExecutionState, protocol.SessionAttribution) (any, error)
	Sample(context.Context, *http.Client, ConnectionConfig, any, []byte, protocol.EvidenceSink, map[string]string, bool) (*sampleResult, error)
	AutoCompactTokens(*int) *int
	PrepareCompaction(*Conversation, *Session, *ExecutionState, protocol.SessionAttribution, string) (*compactionPlan, error)
}

type (
	responsesAdapter struct{ config ModelConfig }
	anthropicAdapter struct{ config ModelConfig }
)

func adapterFor(config ModelConfig) (protocolAdapter, error) {
	switch config.API {
	case protocol.APIStyleResponses:
		return responsesAdapter{config}, nil
	case protocol.APIStyleAnthropicMessages:
		return anthropicAdapter{config}, nil
	default:
		return nil, protocol.ErrInvalid
	}
}
func (a responsesAdapter) API() protocol.APIStyle { return protocol.APIStyleResponses }
func (a anthropicAdapter) API() protocol.APIStyle { return protocol.APIStyleAnthropicMessages }

func (a responsesAdapter) params(c *Conversation, s *Session, state *ExecutionState, attribution protocol.SessionAttribution) (responses.RequestParams, error) {
	tools, err := modelToolSurface(a.config.ExecEncoding)
	return responses.RequestParams{Model: a.config.ProviderModel, Effort: s.Effort, Attribution: attribution, Items: c.Context().(*responses.Context).Items, Instructions: c.Instructions, ExecutionState: state.protocolState(), Tools: tools}, err
}

func (a responsesAdapter) Prepare(c *Conversation, s *Session, state *ExecutionState, attribution protocol.SessionAttribution) (any, error) {
	params, err := a.params(c, s, state, attribution)
	if err != nil {
		return nil, err
	}
	return responses.BuildRequest(params, a.config.Responses, a.config.ExecEncoding)
}

func (a anthropicAdapter) params(c *Conversation, s *Session) (anthropic.RequestParams, error) {
	effort := s.Effort
	if a.config.AnthropicMessages.PerMessageEffort {
		launch := &samplerpb.OpenSessionRequest{}
		if err := proto.Unmarshal(s.Configuration, launch); err != nil {
			return anthropic.RequestParams{}, err
		}
		effort = launch.GetSpec().GetReasoningEffort()
	}
	tools, err := modelToolSurface(a.config.ExecEncoding)
	return anthropic.RequestParams{Context: c.Context().(*anthropic.Context), Model: a.config.ProviderModel, Effort: effort, Instructions: c.Instructions, Tools: tools}, err
}

func (a anthropicAdapter) Prepare(c *Conversation, s *Session, _ *ExecutionState, _ protocol.SessionAttribution) (any, error) {
	params, err := a.params(c, s)
	if err != nil {
		return nil, err
	}
	return anthropic.BuildRequest(params, *a.config.AnthropicMessages, anthropic.PreviousMessageID(c.Archive.(*anthropic.Context)))
}

func (a responsesAdapter) Sample(ctx context.Context, client *http.Client, connection ConnectionConfig, body any, _ []byte, evidence protocol.EvidenceSink, headers map[string]string, compaction bool) (*sampleResult, error) {
	credential, err := readProviderCredential(connection)
	if err != nil {
		return nil, err
	}
	response, err := responses.Sample(ctx, client, connection.BaseURL, credential, body.(responses.Request), a.config.Responses, evidence, headers, compaction)
	if err != nil {
		return nil, err
	}
	records := make([]json.RawMessage, 0, len(response.Output))
	for _, item := range response.Output {
		record, err := json.Marshal(item)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return &sampleResult{Records: records, Audit: response.Audit(), ContextTokens: response.Terminal.TotalTokens()}, nil
}

func (a anthropicAdapter) Sample(ctx context.Context, client *http.Client, connection ConnectionConfig, _ any, wire []byte, evidence protocol.EvidenceSink, _ map[string]string, compaction bool) (*sampleResult, error) {
	credential, err := readProviderCredential(connection)
	if err != nil {
		return nil, err
	}
	response, err := anthropic.Sample(ctx, client, connection.BaseURL, credential, *a.config.AnthropicMessages, wire, evidence, compaction)
	if err != nil {
		return nil, err
	}
	return &sampleResult{Records: []json.RawMessage{response.Message}, Audit: response.Audit(), ContextTokens: response.ContextTokens}, nil
}

func readProviderCredential(connection ConnectionConfig) (string, error) {
	credential, err := connection.Auth.ReadCredential()
	if err != nil {
		return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "provider credential unavailable", "error": err.Error(), "dispatched": false}}
	}
	return credential, nil
}
func (a responsesAdapter) AutoCompactTokens(fallback *int) *int { return fallback }
func (a anthropicAdapter) AutoCompactTokens(_ *int) *int {
	if a.config.AnthropicMessages.Compaction == nil {
		return nil
	}
	return a.config.AnthropicMessages.Compaction.AutoCompactTokens
}

func (a responsesAdapter) PrepareCompaction(c *Conversation, s *Session, state *ExecutionState, attribution protocol.SessionAttribution, environment string) (*compactionPlan, error) {
	params, err := a.params(c, s, state, attribution)
	if err != nil {
		return nil, err
	}
	body, err := responses.BuildCompactionRequest(params, a.config.Responses, a.config.ExecEncoding)
	if err != nil {
		return nil, err
	}
	return &compactionPlan{Body: body, Apply: func(result *sampleResult) (protocol.Context, error) {
		return responses.ValidateCompaction(c.Context().(*responses.Context), result.Records, result.Audit.(*responses.Audit), environment)
	}}, nil
}

func (a anthropicAdapter) PrepareCompaction(c *Conversation, s *Session, _ *ExecutionState, _ protocol.SessionAttribution, _ string) (*compactionPlan, error) {
	params, err := a.params(c, s)
	if err != nil {
		return nil, err
	}
	plan, err := anthropic.PrepareCompaction(params, *a.config.AnthropicMessages, anthropic.PreviousMessageID(c.Archive.(*anthropic.Context)), s.Effort)
	if err != nil {
		return nil, err
	}
	return &compactionPlan{Body: plan.Body, Apply: func(result *sampleResult) (protocol.Context, error) {
		return plan.Apply(result.Records, result.Audit.(*anthropic.Audit))
	}}, nil
}
