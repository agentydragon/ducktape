// Package exportfixture gives History integration tests a narrow, TESTONLY view of the real
// sessionstore outbox without making the Store package importable from History.
package exportfixture

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/historyexport"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

const ownerUID = "test-central-sampler-owner-uid"

// Source owns the real sampler Store used by History export integration scenarios.
type Source struct {
	store *sessionstore.Store
}

func New(db *sql.DB, operationTimeout time.Duration) *Source {
	return &Source{store: sessionstore.New(db, operationTimeout)}
}

func (s *Source) Outbox() historyexport.Outbox { return s.store }

// LostOwnership reports whether an error is the Store's owner-generation fence error.
func LostOwnership(err error) bool {
	var lost *sessionstore.LostOwnershipError
	return errors.As(err, &lost)
}

func (s *Source) CreateSession(ctx context.Context, request *samplerpb.CreateSessionRequest, api protocol.APIStyle, platformInstructions *string) (string, error) {
	session, err := s.store.CreateSession(ctx, request, ownerUID, api, platformInstructions)
	if err != nil {
		return "", err
	}
	return session.ID, nil
}

// ChangeModels performs the accepted command lifecycle used by export ordering and snapshot tests.
func (s *Source) ChangeModels(ctx context.Context, sessionID string, count int, final string) (err error) {
	if count <= 0 {
		return fmt.Errorf("model update count must be positive")
	}
	session, err := s.store.GetSession(ctx, sessionID, ownerUID)
	if err != nil {
		return err
	}
	owner, err := s.store.Claim(ctx, session.ConversationID, time.Minute)
	if err != nil {
		return err
	}
	defer func() {
		if releaseErr := s.store.Release(ctx, owner); err == nil {
			err = releaseErr
		}
	}()
	for index := 0; index < count; index++ {
		model := fmt.Sprintf("test-model-update-%d", index)
		if index == count-1 {
			model = final
		}
		if _, err := s.store.Admit(ctx, sessionID, &commandpb.Command{
			CommandId: uuid.NewString(),
			Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: model}},
		}, ownerUID, nil); err != nil {
			return err
		}
		if _, err := s.store.NextCommand(ctx, owner, sessionID); err != nil {
			return err
		}
	}
	return nil
}

// RecoveryScenario preserves the real Store and provider-evidence path used by History's
// cross-service recovery projection tests. Its Store and owner generation remain private.
type RecoveryScenario struct {
	source         *Source
	sessionID      string
	conversationID string
	turnID         string
	owner          sessionstore.Owner
}

type RecoveryResult struct {
	Cursor       uint64
	BeforeEvents []*eventlogpb.EventEntry
	Events       []*eventlogpb.EventEntry
}

func (s *Source) StartRecoveryScenario(ctx context.Context, sessionID, turnID string) (*RecoveryScenario, error) {
	session, err := s.store.GetSession(ctx, sessionID, ownerUID)
	if err != nil {
		return nil, err
	}
	owner, err := s.store.Claim(ctx, session.ConversationID, time.Minute)
	if err != nil {
		return nil, err
	}
	scenario := &RecoveryScenario{source: s, sessionID: sessionID, conversationID: session.ConversationID, turnID: turnID, owner: owner}
	if _, err := s.store.Admit(ctx, sessionID, &commandpb.Command{
		CommandId: turnID,
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "Start"}},
	}, ownerUID, nil); err != nil {
		_ = s.store.Release(ctx, owner)
		return nil, err
	}
	if _, err := s.store.NextCommand(ctx, owner, sessionID); err != nil {
		_ = s.store.Release(ctx, owner)
		return nil, err
	}
	return scenario, nil
}

func (s *RecoveryScenario) BeginSampling(ctx context.Context) (string, protocol.EvidenceSink, error) {
	intentID, _, err := s.source.store.Begin(ctx, s.owner, sessionstore.IntentPayload{
		Kind: sessionstore.PendingIntentSampling,
	}, nil, &sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: time.Minute})
	if err != nil {
		return "", nil, err
	}
	return intentID, &sessionstore.ResponseEvidence{Store: s.source.store, Owner: s.owner, IntentID: intentID}, nil
}

func (s *RecoveryScenario) FinishSampling(ctx context.Context, intentID string, audit any, records []json.RawMessage, contextTokens *int) error {
	return s.source.store.Finish(ctx, s.owner, intentID, audit, records, contextTokens)
}

func (s *RecoveryScenario) PendingToolCalls(ctx context.Context) ([]protocol.ToolInvocation, error) {
	conversation, err := s.source.store.Read(ctx, s.owner)
	if err != nil {
		return nil, err
	}
	return conversation.Transcript.PendingCalls()
}

func (s *RecoveryScenario) FinishToolCall(ctx context.Context, call protocol.ToolInvocation, output []protocol.ContentPart) error {
	intentID, _, err := s.source.store.Begin(ctx, s.owner, sessionstore.IntentPayload{
		Kind: sessionstore.PendingIntentCell, Call: &call,
	}, nil, nil)
	if err != nil {
		return err
	}
	return s.source.store.FinishTool(ctx, s.owner, intentID,
		map[string]any{"call_id": call.CallID, "output": output, "success": true}, call, output)
}

func (s *RecoveryScenario) TranscriptRecords(ctx context.Context) ([]json.RawMessage, error) {
	conversation, err := s.source.store.Read(ctx, s.owner)
	if err != nil {
		return nil, err
	}
	return conversation.Transcript.Records()
}

func (s *RecoveryScenario) RecoverInterruptedTurn(ctx context.Context, failure error, operatorNote string) (*RecoveryResult, error) {
	active := s.turnID
	if err := s.source.store.FailExecution(ctx, s.owner, s.sessionID, &active, failure); err != nil {
		return nil, err
	}
	blocked, err := s.source.store.GetSession(ctx, s.sessionID, ownerUID)
	if err != nil {
		return nil, err
	}
	beforeEvents, err := s.source.store.Events(ctx, s.sessionID, 0, int(blocked.Cursor))
	if err != nil {
		return nil, err
	}
	if err := s.source.store.Release(ctx, s.owner); err != nil {
		return nil, err
	}
	recovered, err := s.source.store.RecoverSession(ctx, s.sessionID, ownerUID, blocked.Cursor, operatorNote, "")
	if err != nil {
		return nil, err
	}
	entries, err := s.source.store.Events(ctx, s.sessionID, 0, int(recovered.Cursor))
	if err != nil {
		return nil, err
	}
	return &RecoveryResult{Cursor: recovered.Cursor, BeforeEvents: beforeEvents, Events: entries}, nil
}

func (s *RecoveryScenario) ReclaimAfterRecovery(ctx context.Context) error {
	var err error
	s.owner, err = s.source.store.Claim(ctx, s.conversationID, time.Minute)
	return err
}

func (s *RecoveryScenario) Release(ctx context.Context) error {
	return s.source.store.Release(ctx, s.owner)
}

type FailedCommandResult struct {
	Outcome       samplerpb.CommandOutcome
	FailureReason string
}

type FailedInputScenario struct {
	source    *Source
	sessionID string
	owner     sessionstore.Owner
	activeID  string
}

func (s *Source) StartFailedInputScenario(ctx context.Context, sessionID string) (*FailedInputScenario, error) {
	session, err := s.store.GetSession(ctx, sessionID, ownerUID)
	if err != nil {
		return nil, err
	}
	owner, err := s.store.Claim(ctx, session.ConversationID, time.Minute)
	if err != nil {
		return nil, err
	}
	scenario := &FailedInputScenario{source: s, sessionID: sessionID, owner: owner}
	for _, commandID := range []string{"active-input", "queued-input"} {
		if _, err := s.store.Admit(ctx, sessionID, &commandpb.Command{
			CommandId: commandID,
			Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: commandID}},
		}, ownerUID, nil); err != nil {
			_ = s.store.Release(ctx, owner)
			return nil, err
		}
	}
	active, err := s.store.NextCommand(ctx, owner, sessionID)
	if err != nil {
		_ = s.store.Release(ctx, owner)
		return nil, err
	}
	if active != nil {
		scenario.activeID = active.ID
	}
	return scenario, nil
}

func (s *FailedInputScenario) ActiveCommandID() string { return s.activeID }

func (s *FailedInputScenario) FailWithProviderRejection(ctx context.Context, reason string) error {
	intentID, _, err := s.source.store.Begin(ctx, s.owner, sessionstore.IntentPayload{
		Kind: sessionstore.PendingIntentSampling,
	}, nil, &sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: time.Minute})
	if err != nil {
		return err
	}
	if err := s.source.store.AbandonProviderAttempt(ctx, s.owner, intentID, sessionstore.ProviderAttemptFailure{
		Class: sessionstore.ProviderAttemptPermanent, Reason: reason,
	}); err != nil {
		return err
	}
	return s.source.store.FailProviderCommand(ctx, s.owner, s.sessionID, s.activeID, intentID, reason)
}

func (s *FailedInputScenario) FailExecution(ctx context.Context, commandID *string, failure error) error {
	return s.source.store.FailExecution(ctx, s.owner, s.sessionID, commandID, failure)
}

func (s *FailedInputScenario) CommandResult(ctx context.Context, commandID string) (*FailedCommandResult, error) {
	response, err := s.source.store.GetCommand(ctx, s.sessionID, commandID)
	if err != nil {
		return nil, err
	}
	return &FailedCommandResult{Outcome: response.GetOutcome(), FailureReason: response.GetFailureReason()}, nil
}

func (s *FailedInputScenario) BlockedReason(ctx context.Context) (*string, error) {
	session, err := s.source.store.GetSession(ctx, s.sessionID, "")
	if err != nil {
		return nil, err
	}
	return session.BlockedReason, nil
}

func (s *FailedInputScenario) Cursor(ctx context.Context) (uint64, error) {
	session, err := s.source.store.GetSession(ctx, s.sessionID, "")
	if err != nil {
		return 0, err
	}
	return session.Cursor, nil
}

func (s *FailedInputScenario) Release(ctx context.Context) error {
	return s.source.store.Release(ctx, s.owner)
}
