package centralsampler

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func providerAttemptTestPolicy() ProviderAttemptPolicy {
	return ProviderAttemptPolicy{Budget: 3, Lease: time.Minute}
}

func providerAttemptTestPolicyPointer() *ProviderAttemptPolicy {
	policy := providerAttemptTestPolicy()
	return &policy
}

func providerAttemptTestPolicyForKind(kind PendingIntentKind) *ProviderAttemptPolicy {
	if kind == PendingIntentSampling {
		return providerAttemptTestPolicyPointer()
	}
	return nil
}

func providerAttemptTestRequest(kind PendingIntentKind) (IntentPayload, requestPreparation) {
	connection := samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}
	model := samplersettings.ModelConfig{
		Connection: "fixture-connection", ProviderModel: "fixture-provider-model", API: protocol.APIStyleResponses,
		ExecEncoding: protocol.ExecEncodingFreeform, Responses: responses.Config{Dialect: responses.DialectLite},
	}
	request := responses.Request{Model: model.ProviderModel, Stream: true}
	compact := kind == PendingIntentCompaction
	if compact {
		request.Input = []responses.RequestInput{responses.Item{Type: responses.ItemCompaction, EncryptedContent: "fixture-opaque-summary"}}
	}
	prepare := func(store *contentStore, conversation *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
		return prepareResponsesRequest(store, conversation, metadata, request, model.Responses, model.ExecEncoding, compact)
	}
	return providerRequestIntent(kind, connection, model), prepare
}

func admitProviderCommand(t *testing.T, store *Store, owner Owner, sessionID, commandID string, compact bool) {
	t.Helper()
	if compact {
		if _, err := store.Admit(t.Context(), sessionID, &commandpb.Command{
			CommandId: commandID,
			Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}},
		}, "fixture-account", nil); err != nil {
			t.Fatal(err)
		}
	} else {
		admitInput(t, store, sessionID, commandID, "provider retry fixture")
	}
	command, err := store.NextCommand(t.Context(), owner, sessionID)
	if err != nil || command == nil || command.ID != commandID {
		t.Fatalf("next provider command=%v err=%v", command, err)
	}
	if _, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{}); err != nil {
		t.Fatal(err)
	}
}

func setProviderRetryDueNow(t *testing.T, store *Store, intentID string) {
	t.Helper()
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()-interval '1 millisecond' WHERE id=$1`, intentID); err != nil {
		t.Fatal(err)
	}
}

func reopenProviderAttemptStore(t *testing.T, store *Store, dsn string) *Store {
	t.Helper()
	if err := store.DB().Close(); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return New(db, 30*time.Second)
}

func TestProviderAttemptLeaseAndOwnerFences(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	policy := ProviderAttemptPolicy{Budget: 2, Lease: time.Minute}
	intentID, _, err := store.Begin(t.Context(), owner, IntentPayload{Kind: PendingIntentSampling}, nil, &policy)
	if err != nil {
		t.Fatal(err)
	}
	attempt, err := store.CurrentProviderAttempt(t.Context(), owner, session.ID)
	if err != nil || attempt == nil || attempt.IntentID != intentID || attempt.Generation != owner.Generation || attempt.Expired {
		t.Fatalf("current attempt=%+v err=%v", attempt, err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_attempt_lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, intentID); err != nil {
		t.Fatal(err)
	}
	attempt, err = store.CurrentProviderAttempt(t.Context(), owner, session.ID)
	if err != nil || attempt == nil || !attempt.Expired {
		t.Fatalf("expired attempt=%+v err=%v", attempt, err)
	}
	if err := store.RenewProviderAttempt(t.Context(), owner, intentID, time.Minute); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("expired attempt was renewed: %v", err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
		t.Fatal(err)
	}
	if err := store.RenewProviderAttempt(t.Context(), owner, intentID, time.Minute); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("expired owner renewed provider attempt: %v", err)
	}
	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("new generation=%d, old=%d", newOwner.Generation, owner.Generation)
	}
	if err := store.AbandonProviderAttempt(t.Context(), newOwner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptPermanent, Reason: "recovered after owner expiry",
	}); err != nil {
		t.Fatalf("current owner could not abandon unresolved old-generation provider attempt: %v", err)
	}
}

func TestProviderAttemptMigrationBackfillsUnresolvedIntentWithoutRetryAllowance(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	const commandID = "pre-attempt-schema-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}

	// Recreate the committed v15 baseline shape and apply only migration 16 to the live row.
	migrationExec(t, store.DB(), `DROP INDEX sampler_intent_provider_retry_due_idx`)
	migrationExec(t, store.DB(), `DROP INDEX sampler_intent_provider_predecessor_idx`)
	migrationExec(t, store.DB(), `DROP INDEX sampler_intent_provider_attempt_step_idx`)
	migrationExec(t, store.DB(), `ALTER TABLE sampler_intent DROP CONSTRAINT sampler_intent_provider_attempt_fields_check`)
	migrationExec(t, store.DB(), `ALTER TABLE sampler_intent DROP CONSTRAINT sampler_intent_provider_previous_fk`)
	migrationExec(t, store.DB(), `ALTER TABLE sampler_intent DROP CONSTRAINT sampler_intent_provider_step_fk`)
	migrationExec(t, store.DB(), `ALTER TABLE sampler_intent
		DROP COLUMN provider_step_id, DROP COLUMN provider_attempt_no, DROP COLUMN provider_attempt_budget,
		DROP COLUMN provider_attempt_lease_until, DROP COLUMN provider_retry_not_before,
		DROP COLUMN previous_provider_intent_id, DROP COLUMN provider_command_id`)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=16 AND is_applied`)
	if err := migrations.Up(t.Context(), store.DB()); err != nil {
		t.Fatalf("apply provider-attempt migration to old unresolved intent: %v", err)
	}

	var stepID string
	var number, budget int
	var leaseUntil, ownerLease time.Time
	var migratedCommand sql.NullString
	var retryAt sql.NullTime
	if err := store.DB().QueryRowContext(t.Context(), `SELECT i.provider_step_id::text,i.provider_attempt_no,
		i.provider_attempt_budget,i.provider_attempt_lease_until,i.provider_command_id,i.provider_retry_not_before,c.lease_until
		FROM sampler_intent i JOIN sampler_conversation c ON c.id=i.conversation_id WHERE i.id=$1`, intentID).
		Scan(&stepID, &number, &budget, &leaseUntil, &migratedCommand, &retryAt, &ownerLease); err != nil {
		t.Fatal(err)
	}
	if stepID != intentID || number != 1 || budget != 1 || !migratedCommand.Valid || migratedCommand.String != commandID ||
		leaseUntil.After(ownerLease) || retryAt.Valid {
		t.Fatalf("backfilled attempt step=%s number=%d budget=%d command=%v lease=%s owner=%s retry=%v",
			stepID, number, budget, migratedCommand, leaseUntil, ownerLease, retryAt)
	}
}

func TestReadProviderFinishDistinguishesOpenAndCommittedIntent(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	const commandID = "provider-finish-readback"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	state, err := store.ReadProviderFinish(t.Context(), owner, intentID)
	if err != nil || !state.Open || state.Completed {
		t.Fatalf("open provider finish readback=%+v err=%v", state, err)
	}
	if err := store.Finish(t.Context(), owner, intentID, map[string]any{"response": map[string]any{"status": "completed"}}, nil, nil); err != nil {
		t.Fatal(err)
	}
	state, err = store.ReadProviderFinish(t.Context(), owner, intentID)
	if err != nil || state.Open || !state.Completed {
		t.Fatalf("committed provider finish readback=%+v err=%v", state, err)
	}
	for _, status := range []string{"rejected", "failed", "abandoned"} {
		if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET result=jsonb_build_object('status',$2::text) WHERE id=$1`, intentID, status); err != nil {
			t.Fatal(err)
		}
		state, err = store.ReadProviderFinish(t.Context(), owner, intentID)
		if err != nil || state.Completed || state.Open || state.ResultStatus != status {
			t.Fatalf("terminal provider result marker %q readback=%+v err=%v", status, state, err)
		}
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
		t.Fatal(err)
	}
	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.ReadProviderFinish(t.Context(), newOwner, intentID); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("readback accepted a prior owner generation: %v", err)
	}
}

func TestProviderFinishDoesNotCommitAfterDurableInterrupt(t *testing.T) {
	for _, kind := range []PendingIntentKind{PendingIntentSampling, PendingIntentCompaction} {
		t.Run(string(kind), func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			commandID := "provider-interrupt-" + string(kind)
			admitProviderCommand(t, store, owner, session.ID, commandID, kind == PendingIntentCompaction)
			request, prepare := providerAttemptTestRequest(kind)
			var intentID string
			var original protocol.Context
			if kind == PendingIntentSampling {
				intentID, _, err = store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
			} else {
				conversation, readErr := store.Read(t.Context(), owner)
				if readErr != nil {
					t.Fatal(readErr)
				}
				original = conversation.Context()
				intentID, _, err = store.BeginCompaction(t.Context(), owner, request, original, prepare, providerAttemptTestPolicy())
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err := store.RequestInterrupt(t.Context(), owner.ConversationID, owner.Generation); err != nil {
				t.Fatal(err)
			}
			if kind == PendingIntentSampling {
				err = store.Finish(t.Context(), owner, intentID, map[string]any{"response": map[string]any{"status": "completed"}}, nil, nil)
			} else {
				err = store.FinishCompaction(t.Context(), owner, intentID, map[string]any{"response": map[string]any{"status": "completed"}}, original, original)
			}
			var interrupt InterruptPendingError
			if !errors.As(err, &interrupt) {
				t.Fatalf("provider Finish after durable interrupt=%v, want interrupt fence", err)
			}
			state, err := store.ReadProviderFinish(t.Context(), owner, intentID)
			if err != nil || !state.Open || state.Completed {
				t.Fatalf("interrupted provider intent readback=%+v err=%v", state, err)
			}
		})
	}
}

func TestCheckRecoveryAllowsOldProviderIntentButBlocksOldNativeIntent(t *testing.T) {
	store := newTestStore(t)
	providerSession := openSessionForStore(t, store, "")
	providerOwner, err := store.Claim(t.Context(), providerSession.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, providerOwner, providerSession.ID, "old-provider-command", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	providerIntent, _, err := store.Begin(t.Context(), providerOwner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, providerSession.ConversationID); err != nil {
		t.Fatal(err)
	}
	newProviderOwner, err := store.Claim(t.Context(), providerSession.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.CheckRecovery(t.Context(), newProviderOwner); err != nil {
		t.Fatalf("new owner was blocked by read-only provider intent %s: %v", providerIntent, err)
	}
	current, err := store.CurrentProviderAttempt(t.Context(), newProviderOwner, providerSession.ID)
	if err != nil || current == nil || current.IntentID != providerIntent || current.Generation != providerOwner.Generation {
		t.Fatalf("old-generation provider intent=%+v err=%v", current, err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), newProviderOwner, providerIntent, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "previous owner ended before provider completion", RetryAfter: time.Second,
	}); err != nil {
		t.Fatalf("current owner could not fence and settle the old provider request: %v", err)
	}
	setProviderRetryDueNow(t, store, providerIntent)
	newIntent, _, err := store.BeginSamplingRetry(t.Context(), newProviderOwner, providerIntent, providerAttemptTestPolicy())
	if err != nil {
		t.Fatalf("current owner could not continue the same bounded provider step: %v", err)
	}
	next, err := store.CurrentProviderAttempt(t.Context(), newProviderOwner, providerSession.ID)
	if err != nil || next == nil || next.IntentID != newIntent || next.Number != 2 || next.StepID != providerIntent {
		t.Fatalf("recovered provider attempt=%+v err=%v", next, err)
	}

	nativeSession := openSessionForStore(t, store, "")
	nativeOwner, err := store.Claim(t.Context(), nativeSession.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, nativeOwner, nativeSession.ID, "old-native-command", false)
	nativeIntent, _, err := store.Begin(t.Context(), nativeOwner, IntentPayload{Kind: PendingIntentCell}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, nativeSession.ConversationID); err != nil {
		t.Fatal(err)
	}
	newNativeOwner, err := store.Claim(t.Context(), nativeSession.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	var unknown *protocol.UnknownEffectError
	if err := store.CheckRecovery(t.Context(), newNativeOwner); !errors.As(err, &unknown) {
		t.Fatalf("new owner was allowed through old native effect %s: %v", nativeIntent, err)
	}
}

func TestFailProviderCommandPreservesUnrelatedLiveCell(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	const commandID = "provider-permanent-failure"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "first response was unavailable", RetryAfter: time.Second,
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, "unrelated-live-cell", protocol.CellLive); err != nil {
		t.Fatal(err)
	}
	if err := store.FailProviderCommand(t.Context(), owner, session.ID, commandID, intentID, "provider route changed before retry"); err != nil {
		t.Fatal(err)
	}
	got, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	if got.Blocked || got.ActiveCommand != nil || got.ActiveTurn != nil {
		t.Fatalf("provider failure stranded the Session: %+v", got)
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil || len(state.Cells) != 1 || state.Cells[0].ID != "unrelated-live-cell" {
		t.Fatalf("unrelated live cell state=%+v err=%v", state, err)
	}
	var outcome int32
	var failureReason sql.NullString
	if err := store.DB().QueryRowContext(t.Context(), `SELECT outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND id=$2`, session.ID, commandID).
		Scan(&outcome, &failureReason); err != nil || samplerpb.CommandOutcome(outcome) != samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED ||
		!failureReason.Valid || failureReason.String != "provider route changed before retry" {
		t.Fatalf("failed provider command outcome=%s reason=%v err=%v", samplerpb.CommandOutcome(outcome), failureReason, err)
	}
	var retryDue bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT provider_retry_not_before IS NOT NULL FROM sampler_intent WHERE id=$1`, intentID).Scan(&retryDue); err != nil || retryDue {
		t.Fatalf("provider retry due remained after command failure: due=%v err=%v", retryDue, err)
	}
}

func TestProviderAttemptRetryPersistsFrontierAndEvidenceAcrossStoreReopen(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	const commandID = "durable-provider-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, originalWire, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	partialID := "partial-provider-item"
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(),
		protocol.WireBytes{Bytes: []byte("partial native provider frame")},
		&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: partialID}}}); err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "provider stream stalled", RetryAfter: 5 * time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(), protocol.WireBytes{Bytes: []byte("late frame")}); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("late provider evidence after abandon: %v", err)
	}
	if err := store.Finish(t.Context(), owner, intentID, map[string]any{"status": "complete"}, nil, nil); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("Finish after abandon: %v", err)
	}
	var result []byte
	var due bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result,provider_retry_not_before IS NOT NULL FROM sampler_intent WHERE id=$1`, intentID).Scan(&result, &due); err != nil {
		t.Fatal(err)
	}
	var resultValue map[string]any
	if err := json.Unmarshal(result, &resultValue); err != nil {
		t.Fatal(err)
	}
	if resultValue["failure_reason"] != "provider stream stalled" || !due || resultValue["output_committed"] != false {
		t.Fatalf("stored abandonment=%v due=%v", resultValue, due)
	}
	events, err := store.Events(t.Context(), session.ID, 0, 256)
	if err != nil {
		t.Fatal(err)
	}
	var rawEvidence, absent bool
	for _, entry := range events {
		event := entry.GetEvent()
		if boundary := event.GetBoundaryRecord(); boundary != nil && boundary.GetCorrelationId() == intentID {
			var evidence protocol.WireBytes
			if err := json.Unmarshal(boundary.GetPayload(), &evidence); err == nil && bytes.Equal(evidence.Bytes, []byte("partial native provider frame")) {
				rawEvidence = true
			}
		}
		if reconciled := event.GetConversationReconciled(); reconciled != nil {
			for _, item := range reconciled.GetItems() {
				absent = absent || (item.GetItemId() == partialID && item.GetDisposition() == eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT)
			}
		}
	}
	if !rawEvidence || !absent {
		t.Fatalf("provider raw evidence retained=%v partial item ABSENT=%v", rawEvidence, absent)
	}
	setProviderRetryDueNow(t, store, intentID)
	reopened := reopenProviderAttemptStore(t, store, dsn)
	frontier, err := reopened.ProviderRecoveryFrontier(t.Context(), owner, session.ID)
	if err != nil || frontier == nil || frontier.IntentID != intentID || frontier.Number != 1 || frontier.Budget != 3 || frontier.StepID != intentID || frontier.CommandID != commandID {
		t.Fatalf("reopened retry frontier=%+v err=%v", frontier, err)
	}
	newID, retryWire, err := reopened.BeginSamplingRetry(t.Context(), owner, intentID, providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	if newID == intentID || !bytes.Equal(originalWire, retryWire) {
		t.Fatalf("retry id reused=%v wire exact=%v", newID == intentID, bytes.Equal(originalWire, retryWire))
	}
	current, err := reopened.CurrentProviderAttempt(t.Context(), owner, session.ID)
	if err != nil || current == nil || current.IntentID != newID || current.StepID != intentID || current.PreviousID != intentID ||
		current.Number != 2 || current.Budget != 3 || current.CommandID != commandID {
		t.Fatalf("retried attempt=%+v err=%v", current, err)
	}
	var predecessor, stepID string
	var number, budget int
	if err := reopened.DB().QueryRowContext(t.Context(), `SELECT previous_provider_intent_id::text,provider_step_id::text,provider_attempt_no,provider_attempt_budget FROM sampler_intent WHERE id=$1`, newID).
		Scan(&predecessor, &stepID, &number, &budget); err != nil {
		t.Fatal(err)
	}
	if predecessor != intentID || stepID != intentID || number != 2 || budget != 3 {
		t.Fatalf("persisted chain prev=%s step=%s number=%d budget=%d", predecessor, stepID, number, budget)
	}
}

func TestProviderRecoveryFrontierPersistsExhaustedFailureAcrossStoreReopen(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	const commandID = "exhausted-provider-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	policy := ProviderAttemptPolicy{Budget: 1, Lease: time.Minute}
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, &policy)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "one-attempt budget exhausted", RetryAfter: time.Second,
	}); err != nil {
		t.Fatal(err)
	}
	reopened := reopenProviderAttemptStore(t, store, dsn)
	frontier, err := reopened.ProviderRecoveryFrontier(t.Context(), owner, session.ID)
	if err != nil || frontier == nil || frontier.IntentID != intentID || frontier.CommandID != commandID ||
		frontier.FailureClass != ProviderAttemptTransient || frontier.Retryable || frontier.Ready || frontier.Number != 1 || frontier.Budget != 1 {
		t.Fatalf("reopened exhausted provider frontier=%+v err=%v", frontier, err)
	}
	if request := frontier.ProviderRequest; request == nil || request.API != protocol.APIStyleResponses ||
		request.Connection != "fixture-connection" || request.ProviderModel != "fixture-provider-model" ||
		request.Method != "POST" || request.URL != "https://provider.example.test/v1/responses" || request.ProfileID == "" ||
		request.SourceNativeContextID == "" || request.ProjectionKey == "" {
		t.Fatalf("reopened provider route/reference metadata=%+v", request)
	}
}

func TestProviderEvidenceRaceWithAbandonmentIsFenced(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, owner, session.ID, "provider-evidence-race", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	start := make(chan struct{})
	recordResult := make(chan error, 1)
	abandonResult := make(chan error, 1)
	var racers sync.WaitGroup
	racers.Add(2)
	go func() {
		defer racers.Done()
		<-start
		recordResult <- (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(),
			protocol.WireBytes{Bytes: []byte("frame racing abandonment")},
			&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "race-item"}}})
	}()
	go func() {
		defer racers.Done()
		<-start
		abandonResult <- store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
			Class: ProviderAttemptPermanent, Reason: "provider stream abandoned during race",
		})
	}()
	close(start)
	racers.Wait()
	recordErr, abandonErr := <-recordResult, <-abandonResult
	if abandonErr != nil {
		t.Fatalf("abandon race result: %v", abandonErr)
	}
	if recordErr != nil && !errors.Is(recordErr, protocol.ErrPrecondition) {
		t.Fatalf("provider evidence race result: %v", recordErr)
	}
	events, err := store.Events(t.Context(), session.ID, 0, 256)
	if err != nil {
		t.Fatal(err)
	}
	var evidenceSequence, abandonedSequence uint64
	var absent bool
	for _, entry := range events {
		event := entry.GetEvent()
		if boundary := event.GetBoundaryRecord(); boundary != nil && boundary.GetCorrelationId() == intentID {
			var evidence protocol.WireBytes
			if json.Unmarshal(boundary.GetPayload(), &evidence) == nil && bytes.Equal(evidence.Bytes, []byte("frame racing abandonment")) {
				evidenceSequence = entry.GetCursor()
			}
		}
		if progress := event.GetExecutionProgress(); progress != nil && progress.GetOperationId() == intentID &&
			progress.GetPhase() == eventpb.ExecutionProgress_PHASE_ABANDONED {
			abandonedSequence = entry.GetCursor()
		}
		if reconciliation := event.GetConversationReconciled(); reconciliation != nil {
			for _, item := range reconciliation.GetItems() {
				absent = absent || item.GetItemId() == "race-item" && item.GetDisposition() == eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT
			}
		}
	}
	if recordErr == nil {
		if evidenceSequence == 0 || abandonedSequence <= evidenceSequence || !absent {
			t.Fatalf("accepted pre-abandon evidence was not retained/reconciled: evidence=%d abandoned=%d absent=%v", evidenceSequence, abandonedSequence, absent)
		}
	} else if evidenceSequence != 0 || abandonedSequence == 0 {
		t.Fatalf("rejected late evidence left a record or missing abandonment: evidence=%d abandoned=%d", evidenceSequence, abandonedSequence)
	}
}

func TestCompactionRetryRetainsOriginalNativeContext(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	const commandID = "durable-compaction-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, true)
	original, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	request, prepare := providerAttemptTestRequest(PendingIntentCompaction)
	intentID, originalWire, err := store.BeginCompaction(t.Context(), owner, request, original.Context(), prepare, providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "compaction provider stream stalled", RetryAfter: time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	setProviderRetryDueNow(t, store, intentID)
	frontier, err := store.ProviderRecoveryFrontier(t.Context(), owner, session.ID)
	if err != nil || frontier == nil || frontier.Kind != PendingIntentCompaction {
		t.Fatalf("compaction retry frontier=%+v err=%v", frontier, err)
	}
	current, err := store.Read(t.Context(), owner)
	if err != nil || !sameNativeContext(current.Context(), original.Context()) {
		t.Fatalf("abandonment changed original compaction context: err=%v", err)
	}
	newID, retryWire, err := store.BeginCompactionRetry(t.Context(), owner, intentID, original.Context(), providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	if newID == intentID || !bytes.Equal(originalWire, retryWire) {
		t.Fatalf("compaction retry id reused=%v wire exact=%v", newID == intentID, bytes.Equal(originalWire, retryWire))
	}
	current, err = store.Read(t.Context(), owner)
	if err != nil || !sameNativeContext(current.Context(), original.Context()) {
		t.Fatalf("compaction retry changed original context: err=%v", err)
	}
}

func TestProviderRetryDueIsClearedOnHostLossWithoutErasingAncestry(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, owner, session.ID, "host-loss-provider-command", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "provider stalled", RetryAfter: time.Second,
	}); err != nil {
		t.Fatal(err)
	}
	setProviderRetryDueNow(t, store, intentID)
	changed, err := store.ReconcileCodeHostLoss(t.Context(), owner, "00000000-0000-4000-8000-000000000002", protocol.ExecEncodingFreeform)
	if err != nil || !changed {
		t.Fatalf("host loss changed=%v err=%v", changed, err)
	}
	frontier, err := store.ProviderRecoveryFrontier(t.Context(), owner, session.ID)
	if err != nil || frontier == nil || frontier.IntentID != intentID || frontier.RetrySuppressed != ProviderRetrySuppressionHostLoss || frontier.Retryable || frontier.Ready {
		t.Fatalf("host-loss retry frontier=%+v err=%v", frontier, err)
	}
	var stepID, previous sql.NullString
	var number, budget int
	var retry sql.NullTime
	if err := store.DB().QueryRowContext(t.Context(), `SELECT provider_step_id::text,previous_provider_intent_id::text,provider_attempt_no,provider_attempt_budget,provider_retry_not_before FROM sampler_intent WHERE id=$1`, intentID).
		Scan(&stepID, &previous, &number, &budget, &retry); err != nil {
		t.Fatal(err)
	}
	if !stepID.Valid || stepID.String != intentID || number != 1 || budget != 3 || retry.Valid {
		t.Fatalf("host loss changed attempt ancestry step=%v previous=%v number=%d budget=%d due=%v", stepID, previous, number, budget, retry)
	}
}

func TestProviderAttemptAbandonedByHostLossKeepsRecoveryFrontier(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, owner, session.ID, "unresolved-host-loss-provider-command", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	changed, err := store.ReconcileCodeHostLoss(t.Context(), owner, "00000000-0000-4000-8000-000000000002", protocol.ExecEncodingFreeform)
	if err != nil || !changed {
		t.Fatalf("host loss changed=%v err=%v", changed, err)
	}
	frontier, err := store.ProviderRecoveryFrontier(t.Context(), owner, session.ID)
	if err != nil || frontier == nil || frontier.IntentID != intentID || frontier.FailureClass != "" || frontier.RetrySuppressed != ProviderRetrySuppressionHostLoss || frontier.Retryable || frontier.Ready {
		t.Fatalf("unresolved host-loss frontier=%+v err=%v", frontier, err)
	}
	var result []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE id=$1`, intentID).Scan(&result); err != nil {
		t.Fatal(err)
	}
	var resultValue map[string]any
	if err := json.Unmarshal(result, &resultValue); err != nil {
		t.Fatal(err)
	}
	if resultValue["provider_retry_suppressed"] != string(ProviderRetrySuppressionHostLoss) || resultValue["provider_completion_unknown"] != true || resultValue["output_committed"] != false {
		t.Fatalf("host-loss result=%v", resultValue)
	}
}

func TestProviderHostLossSuccessorsPreserveFiniteLineageAndUseFreshContext(t *testing.T) {
	for _, tc := range []struct {
		name    string
		kind    PendingIntentKind
		abandon bool
	}{
		{name: "sampling after transient abandonment", kind: PendingIntentSampling, abandon: true},
		{name: "compaction after unresolved attempt", kind: PendingIntentCompaction},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
				t.Fatal(err)
			}
			commandID := "host-loss-successor-" + string(tc.kind)
			admitProviderCommand(t, store, owner, session.ID, commandID, tc.kind == PendingIntentCompaction)
			request, prepare := providerAttemptTestRequest(tc.kind)
			var initialID string
			var initialWire []byte
			var original protocol.Context
			if tc.kind == PendingIntentSampling {
				initialID, initialWire, err = store.Begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
			} else {
				read, readErr := store.Read(t.Context(), owner)
				if readErr != nil {
					t.Fatal(readErr)
				}
				original = read.Context()
				initialID, initialWire, err = store.BeginCompaction(t.Context(), owner, request, original, prepare, providerAttemptTestPolicy())
			}
			if err != nil {
				t.Fatal(err)
			}
			if tc.abandon {
				if err := store.AbandonProviderAttempt(t.Context(), owner, initialID, ProviderAttemptFailure{
					Class: ProviderAttemptTransient, Reason: "provider stream stalled", RetryAfter: time.Second,
				}); err != nil {
					t.Fatal(err)
				}
				setProviderRetryDueNow(t, store, initialID)
			}
			changed, err := store.ReconcileCodeHostLoss(t.Context(), owner, "00000000-0000-4000-8000-000000000002", protocol.ExecEncodingFreeform)
			if err != nil || !changed {
				t.Fatalf("host loss changed=%v err=%v", changed, err)
			}
			current, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			var successorID string
			var freshWire []byte
			if tc.kind == PendingIntentSampling {
				successorID, freshWire, err = store.BeginSamplingAfterHostLoss(t.Context(), owner, session.ID, initialID, request, prepare, providerAttemptTestPolicy())
			} else {
				successorID, freshWire, err = store.BeginCompactionAfterHostLoss(t.Context(), owner, session.ID, initialID, request, current.Context(), prepare, providerAttemptTestPolicy())
			}
			if err != nil {
				t.Fatalf("prepare fresh-context host-loss successor: %v", err)
			}
			if successorID == initialID || len(freshWire) == 0 || bytes.Equal(initialWire, freshWire) {
				t.Fatalf("host-loss successor reused predecessor request: id_reused=%v wire_changed=%v", successorID == initialID, !bytes.Equal(initialWire, freshWire))
			}
			attempt, err := store.CurrentProviderAttempt(t.Context(), owner, session.ID)
			if err != nil || attempt == nil || attempt.IntentID != successorID || attempt.PreviousID != initialID ||
				attempt.StepID != initialID || attempt.Number != 2 || attempt.Budget != 3 || attempt.CommandID != commandID {
				t.Fatalf("host-loss successor lineage=%+v err=%v", attempt, err)
			}
			if attempt.ProviderRequest == nil || attempt.ProviderRequest.SourceNativeContextID != current.nativeContextID() {
				t.Fatalf("successor source context id=%+v, current=%s", attempt.ProviderRequest, current.nativeContextID())
			}
			control, err := current.contextControl(current.Context())
			if err != nil || !sameJSONValue(attempt.ProviderRequest.SourceControl, control) {
				t.Fatalf("successor source control differs from the post-notice context: err=%v", err)
			}
		})
	}
}

func TestProviderHostLossDoesNotResetExhaustedBudget(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
		t.Fatal(err)
	}
	const commandID = "host-loss-exhausted-budget"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	policy := ProviderAttemptPolicy{Budget: 1, Lease: time.Minute}
	initialID, _, err := store.Begin(t.Context(), owner, request, prepare, &policy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.ReconcileCodeHostLoss(t.Context(), owner, "00000000-0000-4000-8000-000000000002", protocol.ExecEncodingFreeform); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.BeginSamplingAfterHostLoss(t.Context(), owner, session.ID, initialID, request, prepare, policy); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("host loss reset an exhausted provider budget: %v", err)
	}
}
