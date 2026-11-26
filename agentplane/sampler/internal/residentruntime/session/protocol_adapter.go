package session

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

// providerAttribution is what a provider call is attributed to: the Session and the Identity the
// Dispatcher recorded it as executing as.
type providerAttribution struct {
	SessionID string
	Identity  *identitypb.Identity
}

// providerAuthorization is the Authorization value and the attribution headers of one provider
// call. Agentplane's LLM ingress is called as a delegating actor for the Session's execution
// Identity. A bearer connection is never delegated to.
func providerAuthorization(connection samplersettings.ConnectionConfig, attribution providerAttribution) (string, map[string]string, error) {
	serviceSVID := connection.Auth.Type == samplersettings.AuthTypeAgentplaneIngress && connection.TLS.UsesSVID()
	credential := ""
	if !serviceSVID {
		var err error
		credential, err = readProviderCredential(connection)
		if err != nil {
			return "", nil, err
		}
	}
	if connection.Auth.Type != samplersettings.AuthTypeAgentplaneIngress {
		return "Bearer " + credential, map[string]string{"x-agentplane-session-id": attribution.SessionID}, nil
	}
	if attribution.Identity == nil || attribution.Identity.GetName() == "" || attribution.Identity.GetUid() == "" {
		return "", nil, protocol.ErrInvalid
	}
	encoded, err := auth.EncodeDelegationHeader(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: attribution.Identity},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: attribution.SessionID}},
	})
	if err != nil {
		return "", nil, &protocol.KnownFailureError{Evidence: map[string]any{"reason": "delegation envelope invalid", "error": err.Error(), "dispatched": false}}
	}
	if serviceSVID {
		return "", map[string]string{auth.DelegationMetadataKey: encoded}, nil
	}
	return auth.DelegatedScheme + " " + credential, map[string]string{auth.DelegationMetadataKey: encoded}, nil
}

type sampleResult struct {
	Records []json.RawMessage
	// Audit is the adapter's native stored intent result.
	Audit           any
	ResponsesEvents []responses.Event
	AnthropicEvents []json.RawMessage
	ContextTokens   *int
}

type compactionPlan struct {
	Preparation *providerrequest.Preparation
	Apply       func(*sampleResult, string) (protocol.Context, error)
	// ValidateReplacement runs after the environment refresh and before new notifications.
	ValidateReplacement func(protocol.Context) error
}

// These adapters compose native packages with sampler-owned session and route policy.
// Native packages consume snapshots and an evidence sink, never a Store or Runtime.
type protocolAdapter interface {
	Sample(context.Context, *http.Client, samplersettings.ConnectionConfig, []byte, protocol.EvidenceSink, providerAttribution, bool, protocol.ProviderHooks) (*sampleResult, error)
	CanAutoCompact(protocol.Context) bool
	PrepareCompaction(*sessionstore.Conversation, *sessionstore.Session, protocol.SessionAttribution) (*compactionPlan, error)
}

type (
	responsesAdapter struct{ config samplersettings.ModelConfig }
	anthropicAdapter struct{ config samplersettings.ModelConfig }
	chatAdapter      struct{ config samplersettings.ModelConfig }
)

func adapterFor(config samplersettings.ModelConfig) (protocolAdapter, error) {
	switch config.API {
	case protocol.APIStyleResponses:
		return responsesAdapter{config}, nil
	case protocol.APIStyleAnthropicMessages:
		return anthropicAdapter{config}, nil
	case protocol.APIStyleChatCompletions:
		return chatAdapter{config}, nil
	default:
		return nil, protocol.ErrInvalid
	}
}

// requestPreparation captures Session inputs before the Store builds its native request under lock.
func requestPreparation(config samplersettings.ModelConfig, session *sessionstore.Session, attribution protocol.SessionAttribution, compaction bool) *providerrequest.Preparation {
	p := &providerrequest.Preparation{ModelConfig: config, SessionEffort: session.Effort, Compaction: compaction}
	switch config.API {
	case protocol.APIStyleResponses:
		p.Attribution = attribution
	case protocol.APIStyleAnthropicMessages:
		p.SessionConfiguration = append([]byte(nil), session.Configuration...)
	}
	return p
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

func (a responsesAdapter) CanAutoCompact(protocol.Context) bool { return true }

func (a anthropicAdapter) CanAutoCompact(protocol.Context) bool { return true }

func (a responsesAdapter) PrepareCompaction(c *sessionstore.Conversation, s *sessionstore.Session, attribution protocol.SessionAttribution) (*compactionPlan, error) {
	return &compactionPlan{Preparation: requestPreparation(a.config, s, attribution, true), Apply: func(result *sampleResult, intentID string) (protocol.Context, error) {
		return responses.ValidateCompaction(c.Context().(*responses.Context), result.Records, result.Audit.(*responses.Audit), result.ResponsesEvents, intentID)
	}}, nil
}

func (a anthropicAdapter) PrepareCompaction(c *sessionstore.Conversation, s *sessionstore.Session, attribution protocol.SessionAttribution) (*compactionPlan, error) {
	preparation := requestPreparation(a.config, s, attribution, true)
	params, err := preparation.AnthropicParams(c.Instructions)
	if err != nil {
		return nil, err
	}
	params.Context = c.Context().(*anthropic.Context)
	plan, err := anthropic.PrepareCompaction(params, *a.config.AnthropicMessages, anthropic.PreviousMessageID(c.Transcript.(*anthropic.Context)), s.Effort)
	if err != nil {
		return nil, err
	}
	preparation.AnthropicCompactionRequest = &plan.Body
	return &compactionPlan{Preparation: preparation, Apply: func(result *sampleResult, intentID string) (protocol.Context, error) {
		return plan.Apply(result.Records, result.Audit.(*anthropic.Audit), result.AnthropicEvents, intentID)
	}}, nil
}

func (a chatAdapter) Sample(ctx context.Context, client *http.Client, connection samplersettings.ConnectionConfig, wire []byte, evidence protocol.EvidenceSink, attribution providerAttribution, compaction bool, hooks protocol.ProviderHooks) (*sampleResult, error) {
	authorization, headers, err := providerAuthorization(connection, attribution)
	if err != nil {
		return nil, err
	}
	response, err := chatcompletions.Sample(ctx, client, connection.BaseURL, authorization, wire, evidence, headers, compaction, hooks)
	if err != nil {
		return nil, err
	}
	return &sampleResult{Records: []json.RawMessage{response.Record}, Audit: response.Audit, ContextTokens: response.ContextTokens}, nil
}

func (a chatAdapter) CanAutoCompact(state protocol.Context) bool {
	config := a.config.ChatCompaction
	return state.(*chatcompletions.Context).CanCompact(config.RecentTokenBudget())
}

func (a chatAdapter) PrepareCompaction(c *sessionstore.Conversation, session *sessionstore.Session, attribution protocol.SessionAttribution) (*compactionPlan, error) {
	config := a.config.ChatCompaction
	if config == nil {
		return nil, protocol.ErrPrecondition
	}
	plan, err := c.Context().(*chatcompletions.Context).PrepareCompaction(*config)
	if err != nil {
		return nil, err
	}
	return &compactionPlan{
		Preparation: requestPreparation(a.config, session, attribution, true),
		Apply: func(result *sampleResult, intentID string) (protocol.Context, error) {
			return plan.Apply(result.Records, result.Audit.(chatcompletions.Audit), intentID)
		},
		ValidateReplacement: func(replacement protocol.Context) error {
			return plan.CheckReduction(replacement.(*chatcompletions.Context))
		},
	}, nil
}
