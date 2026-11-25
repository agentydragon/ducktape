package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

func providerAttemptTestPolicy() ProviderAttemptPolicy {
	return ProviderAttemptPolicy{Budget: 3, Lease: time.Minute}
}

func providerAttemptTestPolicyPointer() *ProviderAttemptPolicy {
	policy := providerAttemptTestPolicy()
	return &policy
}

func providerAttemptTestRequest(kind PendingIntentKind) (IntentPayload, requestPreparation) {
	request, preparation := providerAttemptTestPreparation(kind)
	return request, requestPreparationFor(preparation)
}

func providerAttemptTestPreparation(kind PendingIntentKind) (IntentPayload, *providerrequest.Preparation) {
	connection := samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}
	model := samplersettings.ModelConfig{
		Connection: "fixture-connection", ProviderModel: "fixture-provider-model", API: protocol.APIStyleResponses,
		ExecEncoding: protocol.ExecEncodingFreeform, Responses: responses.Config{Dialect: responses.DialectLite},
	}
	return ProviderRequestIntent(kind, connection, model), &providerrequest.Preparation{ModelConfig: model, Compaction: kind == PendingIntentCompaction}
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
	if _, err := store.PrepareSamplingContext(t.Context(), owner); err != nil {
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
	db := postgres.Open(t, dsn)
	return New(db, 30*time.Second, nil)
}

func TestProviderAttemptLeaseAndOwnerFences(t *testing.T) {
	store, session, owner := claimedSession(t)
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
	expiredLease, _ := readProviderAttemptLease(t, store, intentID)
	evidence := ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: time.Minute}
	frame := protocol.WireBytes{Bytes: []byte("late frame")}
	for _, progress := range []bool{false, true} {
		if err := evidence.Record(t.Context(), frame, progress); !errors.Is(err, protocol.ErrPrecondition) {
			t.Fatalf("expired attempt accepted evidence with progress=%t: %v", progress, err)
		}
	}
	if got, _ := readProviderAttemptLease(t, store, intentID); !got.Equal(expiredLease) {
		t.Fatalf("expired lease changed from %s to %s", expiredLease, got)
	}
	expireLease(t, store, session)
	if err := evidence.Record(t.Context(), frame, true); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("expired owner recorded provider progress: %v", err)
	}
	newOwner := claim(t, store, session)
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("new generation=%d, old=%d", newOwner.Generation, owner.Generation)
	}
	if err := store.AbandonProviderAttempt(t.Context(), newOwner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptPermanent, Reason: "recovered after owner expiry",
	}); err != nil {
		t.Fatalf("current owner could not abandon unresolved old-generation provider attempt: %v", err)
	}
}

func readProviderAttemptLease(t *testing.T, store *Store, intentID string) (deadline, now time.Time) {
	t.Helper()
	if err := store.DB().QueryRowContext(t.Context(), `SELECT provider_attempt_lease_until,clock_timestamp()
		FROM sampler_intent WHERE id=$1`, intentID).Scan(&deadline, &now); err != nil {
		t.Fatal(err)
	}
	return deadline, now
}

func TestResponseEvidenceRenewsLeaseOnlyOnSemanticProgress(t *testing.T) {
	store, session, owner := claimedSession(t)
	intentID, _, err := store.Begin(t.Context(), owner, IntentPayload{Kind: PendingIntentSampling}, nil, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	// Put the first renewal in the middle of a 30s bucket so a second progress
	// event can exercise the same deadline without sleeping or racing its boundary.
	_, now := readProviderAttemptLease(t, store, intentID)
	wantDeadline := now.Truncate(30 * time.Second).Add(2 * time.Minute)
	progressLease := wantDeadline.Add(-15 * time.Second).Sub(now)
	evidence := ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: progressLease}
	frame := protocol.WireBytes{Bytes: []byte("response bytes")}
	before, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	initialLease, _ := readProviderAttemptLease(t, store, intentID)
	if err := evidence.Record(t.Context(), frame, false); err != nil {
		t.Fatal(err)
	}
	unchanged, _ := readProviderAttemptLease(t, store, intentID)
	if !unchanged.Equal(initialLease) {
		t.Fatalf("transport evidence changed lease from %s to %s", initialLease, unchanged)
	}
	if err := evidence.Record(t.Context(), frame, true); err != nil {
		t.Fatal(err)
	}
	renewed, _ := readProviderAttemptLease(t, store, intentID)
	if !renewed.Equal(wantDeadline) {
		t.Fatalf("renewed lease=%s, want rounded DB deadline %s", renewed, wantDeadline)
	}
	rowVersion := func() string {
		t.Helper()
		var xmin string
		if err := store.DB().QueryRowContext(t.Context(), `SELECT xmin::text FROM sampler_intent WHERE id=$1`, intentID).Scan(&xmin); err != nil {
			t.Fatal(err)
		}
		return xmin
	}
	version := rowVersion()
	if err := evidence.Record(t.Context(), frame, true); err != nil {
		t.Fatal(err)
	}
	if rowVersion() != version {
		t.Fatal("another chunk with the same deadline rewrote the attempt row")
	}
	// Shift the requested expiry by one bucket to exercise a subsequent renewal.
	evidence.ProgressLease += 30 * time.Second
	if err := evidence.Record(t.Context(), frame, true); err != nil {
		t.Fatal(err)
	}
	next, _ := readProviderAttemptLease(t, store, intentID)
	if !next.Equal(wantDeadline.Add(30*time.Second)) || rowVersion() == version {
		t.Fatalf("next renewal deadline=%s, want next bucket after %s and a new row version", next, wantDeadline)
	}
	// Silence after that last semantic event (including a transport-only read)
	// leaves its full remaining idle and operation budgets on the durable attempt.
	version = rowVersion()
	if err := evidence.Record(t.Context(), frame, false); err != nil {
		t.Fatal(err)
	}
	if rowVersion() != version {
		t.Fatal("transport activity after the last progress event rewrote the attempt")
	}
	current, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	if current.Cursor != before.Cursor+10 {
		t.Fatalf("want two events for each evidence record, cursor before=%d after=%d", before.Cursor, current.Cursor)
	}
	expireLease(t, store, session)
	_ = claim(t, store, session)
	if err := evidence.Record(t.Context(), frame, true); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("previous owner appended progress after handoff: %v", err)
	}
	if rowVersion() != version {
		t.Fatal("previous owner rewrote the attempt after handoff")
	}
}

func TestResponseEvidenceRollsBackLeaseWhenObservationAppendFails(t *testing.T) {
	store, session, owner := claimedSession(t)
	intentID, _, err := store.Begin(t.Context(), owner, IntentPayload{Kind: PendingIntentSampling}, nil, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	before, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	initialLease, _ := readProviderAttemptLease(t, store, intentID)
	evidence := ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: 2 * time.Minute}
	// The invalid UTF-8 fails protobuf encoding after the raw record has been appended.
	observation := &eventpb.Event{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{
		ItemId: "provider-item", Text: "\xff",
	}}}
	if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("response bytes")}, true, observation); err == nil || len(observation.GetSourceSequences()) == 0 {
		t.Fatalf("want observation append failure after raw append, err=%v sources=%v", err, observation.GetSourceSequences())
	}
	if got, _ := readProviderAttemptLease(t, store, intentID); !got.Equal(initialLease) {
		t.Fatalf("failed evidence changed lease from %s to %s", initialLease, got)
	}
	after, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	events, err := store.Events(t.Context(), session.ID, before.Cursor, 32)
	if err != nil {
		t.Fatal(err)
	}
	if after.Cursor != before.Cursor || len(events) != 0 {
		t.Fatalf("failed evidence committed events=%d, cursor before=%d after=%d", len(events), before.Cursor, after.Cursor)
	}
}

func TestProviderAttemptAppliesConfiguredCoalescingAndRetryCap(t *testing.T) {
	store, session, owner := claimedSession(t)
	const coalescingSeconds, retrySeconds = 61, 45
	store = New(store.DB(), 30*time.Second, &samplersettings.Settings{
		ProviderProgressLeaseCoalescingSeconds: new(coalescingSeconds),
		ProviderRetryMaxBackoffSeconds:         new(retrySeconds),
	})
	admitProviderCommand(t, store, owner, session.ID, "configured-provider-timing", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	const progressLease = 2 * time.Minute
	evidence := ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: progressLease}
	_, before := readProviderAttemptLease(t, store, intentID)
	if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("semantic evidence")}, true); err != nil {
		t.Fatal(err)
	}
	deadline, after := readProviderAttemptLease(t, store, intentID)
	if deadline.Unix()%coalescingSeconds != 0 || deadline.Before(before.Add(progressLease)) ||
		!deadline.Before(after.Add(progressLease+coalescingSeconds*time.Second)) {
		t.Fatalf("deadline=%s, want a %ds epoch boundary covering the progress lease", deadline, coalescingSeconds)
	}
	failure := ProviderAttemptFailure{Class: ProviderAttemptTransient, Reason: "retry configured fixture", RetryAfter: (retrySeconds + 1) * time.Second}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, failure); !errors.Is(err, protocol.ErrInvalid) {
		t.Fatalf("retry above configured cap: %v", err)
	}
	failure.RetryAfter = retrySeconds * time.Second
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, failure); err != nil {
		t.Fatalf("retry at configured cap: %v", err)
	}
	frontier, err := store.ProviderRecoveryFrontier(t.Context(), owner, session.ID)
	if err != nil || frontier == nil || !frontier.Retryable || frontier.Ready || frontier.RetryIn <= 0 || frontier.RetryIn > failure.RetryAfter {
		t.Fatalf("configured retry frontier=%+v err=%v", frontier, err)
	}
}

func TestProviderAttemptMigrationBackfillsUnresolvedIntentWithoutRetryAllowance(t *testing.T) {
	store, session, owner := claimedSession(t)
	const commandID = "pre-attempt-schema-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}

	removeServiceOwnerMigration(t, store.DB())
	// Recreate the committed v15 baseline shape and reapply all later migrations to the live row.
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
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id>=16 AND is_applied`)
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
	store, session, owner := claimedSession(t)
	const commandID = "provider-finish-readback"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
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
	expireLease(t, store, session)
	newOwner := claim(t, store, session)
	if _, err := store.ReadProviderFinish(t.Context(), newOwner, intentID); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("readback accepted a prior owner generation: %v", err)
	}
}

func TestProviderFinishDoesNotCommitAfterDurableInterrupt(t *testing.T) {
	for _, kind := range []PendingIntentKind{PendingIntentSampling, PendingIntentCompaction} {
		t.Run(string(kind), func(t *testing.T) {
			store, session, owner := claimedSession(t)
			commandID := "provider-interrupt-" + string(kind)
			admitProviderCommand(t, store, owner, session.ID, commandID, kind == PendingIntentCompaction)
			request, prepare := providerAttemptTestRequest(kind)
			var intentID string
			var original protocol.Context
			var err error
			if kind == PendingIntentSampling {
				intentID, _, err = store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
			} else {
				conversation, readErr := store.Read(t.Context(), owner)
				if readErr != nil {
					t.Fatal(readErr)
				}
				original = conversation.Context()
				intentID, _, err = store.beginCompaction(t.Context(), owner, request, original, prepare, providerAttemptTestPolicy())
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
				err = store.FinishCompaction(t.Context(), owner, intentID, map[string]any{"response": map[string]any{"status": "completed"}}, original, original, nil)
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
	providerOwner := claim(t, store, providerSession)
	admitProviderCommand(t, store, providerOwner, providerSession.ID, "old-provider-command", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	providerIntent, _, err := store.begin(t.Context(), providerOwner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	expireLease(t, store, providerSession)
	newProviderOwner := claim(t, store, providerSession)
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
	nativeOwner := claim(t, store, nativeSession)
	admitProviderCommand(t, store, nativeOwner, nativeSession.ID, "old-native-command", false)
	nativeIntent, _, err := store.Begin(t.Context(), nativeOwner, IntentPayload{Kind: PendingIntentCell}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	expireLease(t, store, nativeSession)
	newNativeOwner := claim(t, store, nativeSession)
	var unknown *protocol.UnknownEffectError
	if err := store.CheckRecovery(t.Context(), newNativeOwner); !errors.As(err, &unknown) {
		t.Fatalf("new owner was allowed through old native effect %s: %v", nativeIntent, err)
	}
}

func TestFailProviderCommandPreservesUnrelatedLiveCell(t *testing.T) {
	store, session, owner := claimedSession(t)
	const commandID = "provider-permanent-failure"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "first response was unavailable", RetryAfter: time.Second,
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, "unrelated-live-cell", executionstate.CellLive); err != nil {
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

func TestFailedCompactionPreservesInFlightCellEffect(t *testing.T) {
	store, session, owner := claimedSession(t)
	ctx := t.Context()
	const commandID = "failed-compaction"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	original, err := store.Read(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(ctx, owner, "live-cell", executionstate.CellLive); err != nil {
		t.Fatal(err)
	}
	request, prepare := providerAttemptTestRequest(PendingIntentCompaction)
	compaction, _, err := store.beginCompaction(ctx, owner, request, original.Context(), prepare, providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	nested, err := store.RecordNested(ctx, owner, IntentTool, map[string]any{"cell_id": "live-cell"})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProviderAttempt(ctx, owner, compaction, ProviderAttemptFailure{
		Class: ProviderAttemptPermanent, Reason: "provider rejected compaction",
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.FailProviderCommand(ctx, owner, session.ID, commandID, compaction, "provider rejected compaction"); err != nil {
		t.Fatal(err)
	}
	if err := store.FinishNested(ctx, owner, nested, map[string]any{"complete": true}); err != nil {
		t.Fatal(err)
	}
	current, err := store.Read(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	if current.Pending != nil || current.Stopped || !sameNativeContext(original.Context(), current.Context()) {
		t.Fatalf("failed compaction changed the prior provider context: %+v", current)
	}
	state, err := store.ExecutionState(ctx, owner)
	if err != nil || len(state.Cells) != 1 || state.Cells[0].ID != "live-cell" {
		t.Fatalf("failed compaction lost a live cell: state=%+v err=%v", state, err)
	}
}

func TestProviderAttemptRetryPersistsFrontierAndEvidenceAcrossStoreReopen(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	store.eventBlockWrites = true
	session := openSessionForStore(t, store, "")
	owner := claim(t, store, session)
	const commandID = "durable-provider-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, originalWire, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	partialID := "partial-provider-item"
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: time.Minute}).Record(t.Context(),
		protocol.WireBytes{Bytes: []byte("partial native provider frame")}, true,
		&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: partialID}}}); err != nil {
		t.Fatal(err)
	}
	for range 16 {
		if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(),
			protocol.WireBytes{Bytes: []byte("additional provider frame")}, false); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.AbandonProviderAttempt(t.Context(), owner, intentID, ProviderAttemptFailure{
		Class: ProviderAttemptTransient, Reason: "provider stream stalled", RetryAfter: 5 * time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	if err := (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}).Record(t.Context(), protocol.WireBytes{Bytes: []byte("late frame")}, false); !errors.Is(err, protocol.ErrPrecondition) {
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
	owner := claim(t, store, session)
	const commandID = "exhausted-provider-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	policy := ProviderAttemptPolicy{Budget: 1, Lease: time.Minute}
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, &policy)
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
	store, session, owner := claimedSession(t)
	admitProviderCommand(t, store, owner, session.ID, "provider-evidence-race", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
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
		recordResult <- (&ResponseEvidence{Store: store, Owner: owner, IntentID: intentID, ProgressLease: time.Minute}).Record(t.Context(),
			protocol.WireBytes{Bytes: []byte("frame racing abandonment")}, true,
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
	store, session, owner := claimedSession(t)
	const commandID = "durable-compaction-command"
	admitProviderCommand(t, store, owner, session.ID, commandID, true)
	original, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	request, prepare := providerAttemptTestRequest(PendingIntentCompaction)
	intentID, originalWire, err := store.beginCompaction(t.Context(), owner, request, original.Context(), prepare, providerAttemptTestPolicy())
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

func TestProviderRetryDistinguishesSourceAndNativeChanges(t *testing.T) {
	for _, tc := range []struct {
		name       string
		kind       PendingIntentKind
		changeWire bool
		wantRetry  bool
	}{
		{name: "sampling accepts origin change", kind: PendingIntentSampling, wantRetry: true},
		{name: "sampling rejects native change", kind: PendingIntentSampling, changeWire: true},
		{name: "compaction rejects origin change", kind: PendingIntentCompaction},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := t.Context()
			store, session, owner := claimedSession(t)
			compaction := tc.kind == PendingIntentCompaction
			admitProviderCommand(t, store, owner, session.ID, "source-change-command", compaction)
			before := snapshotFixtureRead(t, store, owner)
			request, prepare := providerAttemptTestRequest(tc.kind)
			policy := providerAttemptTestPolicy()
			var id string
			var wire []byte
			var err error
			if compaction {
				id, wire, err = store.beginCompaction(ctx, owner, request, before.Context(), prepare, policy)
			} else {
				id, wire, err = store.begin(ctx, owner, request, prepare, &policy)
			}
			if err != nil {
				t.Fatal(err)
			}
			if err := store.AbandonProviderAttempt(ctx, owner, id, ProviderAttemptFailure{
				Class: ProviderAttemptTransient, Reason: "provider unavailable", RetryAfter: time.Millisecond,
			}); err != nil {
				t.Fatal(err)
			}
			setProviderRetryDueNow(t, store, id)
			if err := store.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
				current, err := lockOwner(ctx, tx, owner)
				if err != nil {
					return err
				}
				native, err := current.Context().Records()
				if err != nil {
					return err
				}
				replacement := &responses.Context{}
				if err := replacement.AppendRecords(native); err != nil {
					return err
				}
				if tc.changeWire {
					if err := replacement.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "changed native context"}}, protocol.RecordOrigin{}); err != nil {
						return err
					}
				}
				current.executionSnapshots[replacement] = current.executionSnapshots[current.Context()]
				current.Transcript = replacement
				return store.updateConversation(ctx, tx, current)
			}); err != nil {
				t.Fatal(err)
			}
			after := snapshotFixtureRead(t, store, owner)
			if after.nativeContextID() == before.nativeContextID() {
				t.Fatal("fixture did not change the context root")
			}
			var replay []byte
			if compaction {
				// Supply the updated caller view to exercise the persisted source fence.
				_, replay, err = store.BeginCompactionRetry(ctx, owner, id, after.Context(), policy)
			} else {
				_, replay, err = store.BeginSamplingRetry(ctx, owner, id, policy)
			}
			if tc.wantRetry {
				if err != nil || !bytes.Equal(replay, wire) {
					t.Fatalf("retry err=%v, frozen request preserved=%t", err, bytes.Equal(replay, wire))
				}
			} else if !errors.Is(err, protocol.ErrPrecondition) {
				t.Fatalf("retry error = %v, want changed-context precondition", err)
			}
		})
	}
}

func TestProviderRetryDueIsClearedOnHostLossWithoutErasingAncestry(t *testing.T) {
	store, session, owner := claimedSession(t)
	if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, owner, session.ID, "host-loss-provider-command", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
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
	store, session, owner := claimedSession(t)
	if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
		t.Fatal(err)
	}
	admitProviderCommand(t, store, owner, session.ID, "unresolved-host-loss-provider-command", false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
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
			store, session, owner := claimedSession(t)
			if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
				t.Fatal(err)
			}
			commandID := "host-loss-successor-" + string(tc.kind)
			admitProviderCommand(t, store, owner, session.ID, commandID, tc.kind == PendingIntentCompaction)
			request, prepare := providerAttemptTestRequest(tc.kind)
			var initialID string
			var initialWire []byte
			var original protocol.Context
			var err error
			if tc.kind == PendingIntentSampling {
				initialID, initialWire, err = store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
			} else {
				read, readErr := store.Read(t.Context(), owner)
				if readErr != nil {
					t.Fatal(readErr)
				}
				original = read.Context()
				initialID, initialWire, err = store.beginCompaction(t.Context(), owner, request, original, prepare, providerAttemptTestPolicy())
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
				successorID, freshWire, err = store.beginProviderAfterHostLoss(t.Context(), owner, session.ID, initialID, request, nil, prepare, providerAttemptTestPolicy())
			} else {
				successorID, freshWire, err = store.beginProviderAfterHostLoss(t.Context(), owner, session.ID, initialID, request, current.Context(), prepare, providerAttemptTestPolicy())
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
	store, session, owner := claimedSession(t)
	if err := store.StampCodeHostInstance(t.Context(), owner, "00000000-0000-4000-8000-000000000001"); err != nil {
		t.Fatal(err)
	}
	const commandID = "host-loss-exhausted-budget"
	admitProviderCommand(t, store, owner, session.ID, commandID, false)
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	policy := ProviderAttemptPolicy{Budget: 1, Lease: time.Minute}
	initialID, _, err := store.begin(t.Context(), owner, request, prepare, &policy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.ReconcileCodeHostLoss(t.Context(), owner, "00000000-0000-4000-8000-000000000002", protocol.ExecEncodingFreeform); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.beginProviderAfterHostLoss(t.Context(), owner, session.ID, initialID, request, nil, prepare, policy); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("host loss reset an exhausted provider budget: %v", err)
	}
}
