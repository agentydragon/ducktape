package centralsampler

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/protobuf/proto"
)

// providerAttribution is what a provider call is attributed to: the Session, and the Identity the
// Dispatcher recorded it as executing as, when it did.
type providerAttribution struct {
	SessionID string
	Identity  *identitypb.Identity
}

// executionIdentity is the Identity the Dispatcher recorded the Session as executing as, or nil for
// a Session whose Sandbox named none.
func executionIdentity(session *Session) (*identitypb.Identity, error) {
	request := &samplerpb.CreateSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return nil, fmt.Errorf("decode stored session configuration: %w", err)
	}
	return cloneExecutionIdentity(request.GetExecutionIdentity()), nil
}

// providerAuthorization is the Authorization value and the attribution headers of one provider
// call. Agentplane's LLM ingress is called as a delegating actor for a Session executing as an
// Identity, which the ingress then attributes the call to. A Session without an Identity, on any
// connection, is called as the sampler itself with the Session claimed by header, exactly as before
// Identities existed; a bearer connection is never delegated to.
func providerAuthorization(connection ConnectionConfig, attribution providerAttribution) (string, map[string]string, error) {
	credential, err := readProviderCredential(connection)
	if err != nil {
		return "", nil, err
	}
	if connection.Auth.Type != AuthTypeAgentplaneIngress || attribution.Identity == nil {
		return "Bearer " + credential, map[string]string{"x-agentplane-session-id": attribution.SessionID}, nil
	}
	encoded, err := auth.EncodeDelegationHeader(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: attribution.Identity},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: attribution.SessionID}},
	})
	if err != nil {
		return "", nil, &protocol.KnownFailureError{Evidence: map[string]any{"reason": "delegation envelope invalid", "error": err.Error(), "dispatched": false}}
	}
	return auth.DelegatedScheme + " " + credential, map[string]string{auth.DelegationMetadataKey: encoded}, nil
}

type sampleResult struct {
	Records []json.RawMessage
	// Audit is the adapter's stored intent result: *responses.Audit or *anthropic.Audit.
	Audit         any
	ContextTokens *int
}

type compactionPlan struct {
	// Prepare builds the persisted recipe within the intent transaction.
	Prepare requestPreparation
	Apply   func(*sampleResult) (protocol.Context, error)
}

// These adapters compose native packages with sampler-owned session and route policy.
// Native packages consume snapshots and an evidence sink, never a Store or Runtime.
type protocolAdapter interface {
	API() protocol.APIStyle
	Prepare(*Session, protocol.SessionAttribution) requestPreparation
	Sample(context.Context, *http.Client, ConnectionConfig, []byte, protocol.EvidenceSink, providerAttribution, bool) (*sampleResult, error)
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

func (a responsesAdapter) params(c *Conversation, s *Session, attribution protocol.SessionAttribution) (responses.RequestParams, error) {
	tools, err := modelToolSurface(a.config.ExecEncoding)
	return responses.RequestParams{Model: a.config.ProviderModel, Effort: s.Effort, Attribution: attribution, Instructions: c.Instructions, Tools: tools}, err
}

func (a responsesAdapter) Prepare(session *Session, attribution protocol.SessionAttribution) requestPreparation {
	return a.preparation(session, attribution, false)
}

func (a responsesAdapter) preparation(session *Session, attribution protocol.SessionAttribution, compact bool) requestPreparation {
	return func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		params, err := a.params(conversation, session, attribution)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		var body responses.Request
		if compact {
			body, err = responses.BuildCompactionRequest(params, a.config.Responses, a.config.ExecEncoding)
		} else {
			body, err = responses.BuildRequest(params, a.config.Responses, a.config.ExecEncoding)
		}
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		return prepareResponsesRequest(store, conversation, metadata, body, a.config.Responses, a.config.ExecEncoding, compact)
	}
}

func (a anthropicAdapter) params(c *Conversation, s *Session) (anthropic.RequestParams, error) {
	effort := s.Effort
	if a.config.AnthropicMessages.PerMessageEffort {
		launch := &samplerpb.CreateSessionRequest{}
		if err := proto.Unmarshal(s.Configuration, launch); err != nil {
			return anthropic.RequestParams{}, err
		}
		effort = launch.GetSpec().GetReasoningEffort()
	}
	tools, err := modelToolSurface(a.config.ExecEncoding)
	return anthropic.RequestParams{Context: c.Context().(*anthropic.Context), Model: a.config.ProviderModel, Effort: effort, Instructions: c.Instructions, Tools: tools}, err
}

func (a anthropicAdapter) Prepare(session *Session, _ protocol.SessionAttribution) requestPreparation {
	return func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		params, err := a.params(conversation, session)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		params.Context = nil // Metadata only; the codec projects the stable history incrementally.
		body, err := anthropic.BuildRequest(params, *a.config.AnthropicMessages, anthropic.PreviousMessageID(conversation.Transcript.(*anthropic.Context)))
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		return prepareAnthropicRequest(store, conversation, metadata, body)
	}
}

func (a responsesAdapter) Sample(ctx context.Context, client *http.Client, connection ConnectionConfig, wire []byte, evidence protocol.EvidenceSink, attribution providerAttribution, compaction bool) (*sampleResult, error) {
	authorization, headers, err := providerAuthorization(connection, attribution)
	if err != nil {
		return nil, err
	}
	response, err := responses.Sample(ctx, client, connection.BaseURL, authorization, wire, a.config.Responses, evidence, headers, compaction)
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

// The direct Anthropic API is called with its own key; it attributes nothing to the Session.
func (a anthropicAdapter) Sample(ctx context.Context, client *http.Client, connection ConnectionConfig, wire []byte, evidence protocol.EvidenceSink, _ providerAttribution, compaction bool) (*sampleResult, error) {
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
	return &compactionPlan{Prepare: a.preparation(s, attribution, true), Apply: func(result *sampleResult) (protocol.Context, error) {
		return responses.ValidateCompaction(c.Context().(*responses.Context), result.Records, result.Audit.(*responses.Audit), environment)
	}}, nil
}

func (a anthropicAdapter) PrepareCompaction(c *Conversation, s *Session, _ *ExecutionState, _ protocol.SessionAttribution, _ string) (*compactionPlan, error) {
	params, err := a.params(c, s)
	if err != nil {
		return nil, err
	}
	plan, err := anthropic.PrepareCompaction(params, *a.config.AnthropicMessages, anthropic.PreviousMessageID(c.Transcript.(*anthropic.Context)), s.Effort)
	if err != nil {
		return nil, err
	}
	return &compactionPlan{Prepare: func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		previous, err := store.previousRequest(conversation.ID)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		body := plan.Body
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
	}, Apply: func(result *sampleResult) (protocol.Context, error) {
		return plan.Apply(result.Records, result.Audit.(*anthropic.Audit))
	}}, nil
}
