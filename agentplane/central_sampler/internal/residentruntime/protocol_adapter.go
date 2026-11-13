package residentruntime

import (
	"context"
	"encoding/json"
	"net/http"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
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
func executionIdentity(session *sessionstore.Session) (*identitypb.Identity, error) {
	return session.ExecutionIdentity()
}

// providerAuthorization is the Authorization value and the attribution headers of one provider
// call. Agentplane's LLM ingress is called as a delegating actor for a Session executing as an
// Identity, which the ingress then attributes the call to. A Session without an Identity, on any
// connection, is called as the sampler itself with the Session claimed by header, exactly as before
// Identities existed; a bearer connection is never delegated to.
func providerAuthorization(connection samplersettings.ConnectionConfig, attribution providerAttribution) (string, map[string]string, error) {
	credential, err := readProviderCredential(connection)
	if err != nil {
		return "", nil, err
	}
	if connection.Auth.Type != samplersettings.AuthTypeAgentplaneIngress || attribution.Identity == nil {
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
	Audit           any
	ResponsesEvents []responses.Event
	AnthropicEvents []json.RawMessage
	ContextTokens   *int
}

type compactionPlan struct {
	Preparation *sessionstore.ProviderRequestPreparation
	Apply       func(*sampleResult) (protocol.Context, error)
}

// These adapters compose native packages with sampler-owned session and route policy.
// Native packages consume snapshots and an evidence sink, never a Store or Runtime.
type protocolAdapter interface {
	API() protocol.APIStyle
	Prepare(*sessionstore.Session, protocol.SessionAttribution) *sessionstore.ProviderRequestPreparation
	Sample(context.Context, *http.Client, samplersettings.ConnectionConfig, []byte, protocol.EvidenceSink, providerAttribution, bool, protocol.ProviderHooks) (*sampleResult, error)
	AutoCompactTokens(*int) *int
	PrepareCompaction(*sessionstore.Conversation, *sessionstore.Session, *sessionstore.ExecutionState, protocol.SessionAttribution, string) (*compactionPlan, error)
}

type (
	responsesAdapter struct{ config samplersettings.ModelConfig }
	anthropicAdapter struct{ config samplersettings.ModelConfig }
)

func adapterFor(config samplersettings.ModelConfig) (protocolAdapter, error) {
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

func (a responsesAdapter) Prepare(session *sessionstore.Session, attribution protocol.SessionAttribution) *sessionstore.ProviderRequestPreparation {
	return &sessionstore.ProviderRequestPreparation{
		ModelConfig: a.config, SessionEffort: session.Effort,
		SessionConfiguration: append([]byte(nil), session.Configuration...), Attribution: attribution,
	}
}

func (a anthropicAdapter) Prepare(session *sessionstore.Session, attribution protocol.SessionAttribution) *sessionstore.ProviderRequestPreparation {
	return &sessionstore.ProviderRequestPreparation{
		ModelConfig: a.config, SessionEffort: session.Effort,
		SessionConfiguration: append([]byte(nil), session.Configuration...), Attribution: attribution,
	}
}

func (a responsesAdapter) Sample(ctx context.Context, client *http.Client, connection samplersettings.ConnectionConfig, wire []byte, evidence protocol.EvidenceSink, attribution providerAttribution, compaction bool, hooks protocol.ProviderHooks) (*sampleResult, error) {
	authorization, headers, err := providerAuthorization(connection, attribution)
	if err != nil {
		return nil, err
	}
	response, err := responses.Sample(ctx, client, connection.BaseURL, authorization, wire, a.config.Responses, evidence, headers, compaction, hooks)
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
	return &sampleResult{Records: records, Audit: response.Audit(), ResponsesEvents: response.Events, ContextTokens: response.Terminal.TotalTokens()}, nil
}

// The direct Anthropic API is called with its own key; it attributes nothing to the Session.
func (a anthropicAdapter) Sample(ctx context.Context, client *http.Client, connection samplersettings.ConnectionConfig, wire []byte, evidence protocol.EvidenceSink, _ providerAttribution, compaction bool, hooks protocol.ProviderHooks) (*sampleResult, error) {
	credential, err := readProviderCredential(connection)
	if err != nil {
		return nil, err
	}
	response, err := anthropic.Sample(ctx, client, connection.BaseURL, credential, *a.config.AnthropicMessages, wire, evidence, compaction, hooks)
	if err != nil {
		return nil, err
	}
	return &sampleResult{Records: []json.RawMessage{response.Message}, Audit: response.Audit(), AnthropicEvents: response.Events, ContextTokens: response.ContextTokens}, nil
}

func readProviderCredential(connection samplersettings.ConnectionConfig) (string, error) {
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

func (a responsesAdapter) PrepareCompaction(c *sessionstore.Conversation, s *sessionstore.Session, state *sessionstore.ExecutionState, attribution protocol.SessionAttribution, environment string) (*compactionPlan, error) {
	return &compactionPlan{Preparation: &sessionstore.ProviderRequestPreparation{
		ModelConfig: a.config, SessionEffort: s.Effort,
		SessionConfiguration: append([]byte(nil), s.Configuration...), Attribution: attribution, Compaction: true,
	}, Apply: func(result *sampleResult) (protocol.Context, error) {
		return responses.ValidateCompaction(c.Context().(*responses.Context), result.Records, result.Audit.(*responses.Audit), result.ResponsesEvents, environment)
	}}, nil
}

func (a anthropicAdapter) PrepareCompaction(c *sessionstore.Conversation, s *sessionstore.Session, _ *sessionstore.ExecutionState, _ protocol.SessionAttribution, _ string) (*compactionPlan, error) {
	effort := s.Effort
	if a.config.AnthropicMessages.PerMessageEffort {
		launch := &samplerpb.CreateSessionRequest{}
		if err := proto.Unmarshal(s.Configuration, launch); err != nil {
			return nil, err
		}
		effort = launch.GetSpec().GetReasoningEffort()
	}
	tools, err := toolcontract.ModelSurface(a.config.ExecEncoding)
	if err != nil {
		return nil, err
	}
	params := anthropic.RequestParams{
		Context: c.Context().(*anthropic.Context), Model: a.config.ProviderModel, Effort: effort,
		Instructions: c.Instructions, Tools: tools,
	}
	plan, err := anthropic.PrepareCompaction(params, *a.config.AnthropicMessages, anthropic.PreviousMessageID(c.Transcript.(*anthropic.Context)), s.Effort)
	if err != nil {
		return nil, err
	}
	return &compactionPlan{Preparation: &sessionstore.ProviderRequestPreparation{
		ModelConfig: a.config, SessionEffort: s.Effort,
		SessionConfiguration: append([]byte(nil), s.Configuration...), Compaction: true,
		AnthropicCompactionRequest: &plan.Body,
	}, Apply: func(result *sampleResult) (protocol.Context, error) {
		return plan.Apply(result.Records, result.Audit.(*anthropic.Audit), result.AnthropicEvents)
	}}, nil
}
