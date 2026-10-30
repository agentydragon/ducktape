package centralsampler

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"

	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
)

func openSessionForStore(t *testing.T, store *Store, setup string) *Session {
	t.Helper()
	request := &samplerpb.OpenSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &runnerpb.SessionSpec{
			Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model",
		},
		SetupScript: &setup,
	}
	session, err := store.OpenSession(t.Context(), request, "fixture-account", APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func TestStoreFencesEveryWriteToTheLiveLeaseGeneration(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling", "input": "first"}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
		t.Fatal(err)
	}
	if err := store.Renew(t.Context(), owner, time.Minute); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("expired owner renewed lease: %v", err)
	}
	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("generation=%d old=%d", newOwner.Generation, owner.Generation)
	}
	if err := store.AppendInput(t.Context(), owner, "stale input"); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("stale generation mutated conversation: %v", err)
	}
	if err := store.Release(t.Context(), newOwner); err != nil {
		t.Fatal(err)
	}
	if err := store.Renew(t.Context(), newOwner, time.Minute); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("released owner was not reported as stale: %v", err)
	}
}

func TestStoreIntentJournalBlocksReplayAndPersistsKnownRejection(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	first, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling", "turn": "turn-1"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling", "turn": "turn-1"}); !errors.As(err, new(*UnknownEffectError)) {
		t.Fatalf("pending intent was replayable: %v", err)
	}
	call := ResponsesItem{Type: ResponsesItemCustomToolCall, CallID: "call-1", Name: "exec", Input: "{}", ID: "item-1"}
	if err := store.Finish(t.Context(), owner, first, map[string]any{"id": "response-1"}, nativeResponsesRecords(t, call), nil); err != nil {
		t.Fatal(err)
	}
	second, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling", "turn": "turn-2"})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Reject(t.Context(), owner, second, &KnownFailureError{Evidence: map[string]any{"code": "invalid_request"}}); err != nil {
		t.Fatal(err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if !conversation.Stopped || conversation.Pending != nil {
		t.Fatalf("rejection left conversation live or pending: stopped=%v pending=%v", conversation.Stopped, conversation.Pending)
	}
	items := responsesItems(t, conversation.Archive)
	if len(items) != 2 || items[1].Type != ResponsesItemCustomToolOutput {
		t.Fatalf("unstarted call output was not durably settled: %#v", items)
	}
	result, err := intentResultForDB(t, store, second)
	if err != nil {
		t.Fatal(err)
	}
	if result["status"] != "rejected" || result["code"] != "invalid_request" {
		t.Fatalf("known rejection result=%v", result)
	}
}

func TestStoreRecoverSessionAbandonsBlockedCellAndFencesOldOwner(t *testing.T) {
	store := newTestStore(t)
	executor := &sandboxespb.SandboxDestination{
		Owner:   &sandboxespb.ServiceAccount{Namespace: "fixture", Name: "executor"},
		Sandbox: "fixture-sandbox", SandboxUid: "fixture-sandbox-uid",
	}
	const previousIncarnationUID = "fixture-previous-incarnation"
	const replacementIncarnationUID = "fixture-replacement-incarnation"
	session, err := store.OpenSession(t.Context(), &samplerpb.OpenSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &runnerpb.SessionSpec{
			Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model",
		},
		Executor: executor, ExpectedIncarnationUid: previousIncarnationUID,
	}, "fixture-account", APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	shell := "bash"
	if err := store.SetExecutorSession(t.Context(), owner, "fixture-old-executor-session", &shell); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(t.Context(), owner, "fixture-old-process", false); err != nil {
		t.Fatal(err)
	}
	sampling, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling", "input": "first"})
	if err != nil {
		t.Fatal(err)
	}
	call := ResponsesItem{Type: ResponsesItemCustomToolCall, CallID: "recover-call", Name: "exec", Input: "text(1)", ID: "recover-item"}
	if err := store.Finish(t.Context(), owner, sampling, map[string]any{"id": "response"}, nativeResponsesRecords(t, call), nil); err != nil {
		t.Fatal(err)
	}
	cellIntent, err := store.Begin(t.Context(), owner, map[string]any{"kind": "cell", "call": call})
	if err != nil {
		t.Fatal(err)
	}
	cellID := "code-session:cell-1"
	toolIntent, err := store.RecordNested(t.Context(), owner, map[string]any{"kind": "tool", "cell_id": cellID, "tool": "exec"})
	if err != nil {
		t.Fatal(err)
	}
	executorIntent, err := store.RecordNested(t.Context(), owner, map[string]any{"kind": "executor", "method": "process/start"})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, cellID, "live"); err != nil {
		t.Fatal(err)
	}
	if err := store.FailExecution(t.Context(), owner, session.ID, nil, errors.New("code host detached")); err != nil {
		t.Fatal(err)
	}
	blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !blocked.Blocked {
		t.Fatal("unknown cell result did not block the session")
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	contextItems, err := json.Marshal(conversation.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET context=$2::jsonb WHERE id=$1`,
		session.ConversationID, contextItems); err != nil {
		t.Fatal(err)
	}

	cursor := blocked.Cursor
	if _, err := store.RecoverSession(t.Context(), session.ID, "other-account", cursor, "reviewed evidence", ""); !errors.Is(err, ErrForbidden) {
		t.Fatalf("wrong account recovered session: %v", err)
	}
	if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", cursor-1, "reviewed evidence", ""); !errors.Is(err, ErrPrecondition) {
		t.Fatalf("stale cursor recovered session: %v", err)
	}
	if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", cursor, "reviewed evidence", ""); !errors.Is(err, ErrPrecondition) {
		t.Fatalf("active owner lease was ignored: %v", err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
		t.Fatal(err)
	}

	recovered, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", cursor, "reviewed evidence", replacementIncarnationUID)
	if err != nil {
		t.Fatal(err)
	}
	if recovered.Blocked || recovered.BlockedReason != nil || recovered.Cursor <= cursor {
		t.Fatalf("recovered session state=%+v", recovered)
	}
	persisted, err := store.GetSession(t.Context(), session.ID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	configuration := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(persisted.Configuration, configuration); err != nil {
		t.Fatal(err)
	}
	if configuration.GetExpectedIncarnationUid() != replacementIncarnationUID || !proto.Equal(configuration.GetExecutor(), executor) {
		t.Fatalf("recovered configuration changed binding unexpectedly: %v", configuration)
	}
	described, err := Describe(recovered)
	if err != nil {
		t.Fatal(err)
	}
	if described.GetExpectedIncarnationUid() != replacementIncarnationUID || described.GetAttached().GetIncarnationUid() != replacementIncarnationUID ||
		!proto.Equal(described.GetExecutor(), executor) {
		t.Fatalf("recovered description lost replacement binding: %v", described)
	}
	if err := store.Finish(t.Context(), owner, cellIntent, map[string]any{"late": true}, nil, nil); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("old owner wrote after recovery: %v", err)
	}
	if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", cursor, "reviewed evidence", replacementIncarnationUID); !errors.Is(err, ErrPrecondition) {
		t.Fatalf("stale retry recovered twice: %v", err)
	}

	for _, intentID := range []string{cellIntent, toolIntent, executorIntent} {
		result, err := intentResultForDB(t, store, intentID)
		if err != nil {
			t.Fatal(err)
		}
		if result["status"] != "abandoned" || result["remote_outcome"] != "unknown" || result["automatic_replay"] != false {
			t.Fatalf("abandoned intent %s result=%v", intentID, result)
		}
	}
	var generation, epoch int
	var ownerToken sql.NullString
	var executorSession, executorShell sql.NullString
	if err := store.DB().QueryRowContext(t.Context(), `SELECT generation,code_session_epoch,owner::text,executor_session,executor_shell FROM sampler_conversation WHERE id=$1`,
		session.ConversationID).Scan(&generation, &epoch, &ownerToken, &executorSession, &executorShell); err != nil {
		t.Fatal(err)
	}
	if generation != owner.Generation+1 || epoch != 1 || ownerToken.Valid || executorSession.Valid || executorShell.Valid {
		t.Fatalf("recovery fence generation=%d epoch=%d owner=%v executor_session=%v executor_shell=%v",
			generation, epoch, ownerToken, executorSession, executorShell)
	}
	var cellStatus string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT status FROM sampler_cell WHERE id=$1 AND conversation_id=$2`,
		cellID, session.ConversationID).Scan(&cellStatus); err != nil {
		t.Fatal(err)
	}
	if cellStatus != "abandoned" {
		t.Fatalf("cell status=%q", cellStatus)
	}
	var processStatus string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT status FROM sampler_process WHERE id='fixture-old-process'`).Scan(&processStatus); err != nil {
		t.Fatal(err)
	}
	if processStatus != "abandoned" {
		t.Fatalf("process status=%q", processStatus)
	}
	events, err := store.Events(t.Context(), session.ID, cursor, 32)
	if err != nil {
		t.Fatal(err)
	}
	foundRecoveryEvidence := false
	for _, entry := range events {
		boundary := entry.GetEvent().GetBoundaryRecord()
		if boundary == nil || boundary.GetBoundary() != "recovery" || boundary.GetDirection() != "committed" {
			continue
		}
		var evidence map[string]any
		if err := json.Unmarshal(boundary.GetPayload(), &evidence); err != nil {
			t.Fatal(err)
		}
		if evidence["previous_incarnation_uid"] != previousIncarnationUID || evidence["replacement_incarnation_uid"] != replacementIncarnationUID {
			t.Fatalf("recovery incarnation evidence=%v", evidence)
		}
		foundRecoveryEvidence = true
	}
	if !foundRecoveryEvidence {
		t.Fatal("recovery emitted no incarnation evidence")
	}

	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	retained, err := store.Read(t.Context(), newOwner)
	if err != nil {
		t.Fatal(err)
	}
	archive := responsesItems(t, retained.Archive)
	if len(archive) != 3 || archive[1].Type != ResponsesItemCustomToolOutput || archive[1].CallID != call.CallID || archive[2].Role != "developer" {
		t.Fatalf("provider history was not completed with a matching aborted output: %#v", archive)
	}
	active := responsesItems(t, retained.Context())
	if len(active) != 3 || active[1].CallID != call.CallID || active[2].Role != "developer" {
		t.Fatalf("model context did not retain the aborted output: %#v", active)
	}
}

func TestStoreRecoverSessionPreservesKnownRejectedCellResult(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	sampling, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
	if err != nil {
		t.Fatal(err)
	}
	call := ResponsesItem{Type: ResponsesItemFunctionCall, CallID: "invalid-cell", Name: "exec", Arguments: `{"cell_id":"native-session","terminate":true}`}
	if err := store.Finish(t.Context(), owner, sampling, map[string]any{"id": "response"}, nativeResponsesRecords(t, call), nil); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(t.Context(), owner, "other-process", false); err != nil {
		t.Fatal(err)
	}
	cellIntent, err := store.Begin(t.Context(), owner, map[string]any{"kind": "cell", "call": call})
	if err != nil {
		t.Fatal(err)
	}
	const reason = "wait requires a known live cell_id"
	// Reproduce the old failure path: Reject saves the known result but retains
	// Pending because an unrelated process is still live.
	if err := store.Reject(t.Context(), owner, cellIntent, &KnownFailureError{Evidence: map[string]any{"reason": reason, "dispatched": false}}); err != nil {
		t.Fatal(err)
	}
	blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !blocked.Blocked {
		t.Fatal("fixture did not reproduce the blocked rejection")
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	recovered, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "reviewed unrelated process", "")
	if err != nil {
		t.Fatal(err)
	}
	if recovered.Blocked {
		t.Fatal("known rejected cell remained blocked")
	}
	result, err := intentResultForDB(t, store, cellIntent)
	if err != nil {
		t.Fatal(err)
	}
	if result["status"] != "rejected" || result["dispatched"] != false || result["reason"] != reason {
		t.Fatalf("known result changed: %v", result)
	}
	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	conversation, err := store.Read(t.Context(), newOwner)
	if err != nil {
		t.Fatal(err)
	}
	items := responsesItems(t, conversation.Archive)
	if conversation.Pending != nil || len(items) != 3 || items[1].CallID != call.CallID {
		t.Fatalf("unsettled call: %+v", conversation)
	}
	output, err := json.Marshal(items[1].Output)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(output), reason) || !strings.Contains(string(output), "No runtime or executor operation was dispatched") {
		t.Fatalf("misleading output: %s", output)
	}
	var status string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT status FROM sampler_process WHERE id='other-process'`).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "abandoned" {
		t.Fatalf("other process status=%s", status)
	}
}

func TestStoreRecoverSessionSupportsLostRuntimeWithoutPendingIntent(t *testing.T) {
	for _, kind := range []string{"cell", "process"} {
		t.Run(kind, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			switch kind {
			case "cell":
				if err := store.CellResult(t.Context(), owner, "code-session:yielded-cell", "live"); err != nil {
					t.Fatal(err)
				}
			case "process":
				if err := store.RegisterProcess(t.Context(), owner, "unknown-process", false); err != nil {
					t.Fatal(err)
				}
			}
			if err := store.FailExecution(t.Context(), owner, session.ID, nil, errors.New("runtime lost")); err != nil {
				t.Fatal(err)
			}
			blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
			if err != nil {
				t.Fatal(err)
			}
			if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
				t.Fatal(err)
			}
			if recovered, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "runtime was lost", ""); err != nil {
				t.Fatal(err)
			} else if recovered.Blocked {
				t.Fatal("recovered session remains blocked")
			}
			if kind == "process" {
				var status string
				if err := store.DB().QueryRowContext(t.Context(), `SELECT status FROM sampler_process WHERE id='unknown-process'`).Scan(&status); err != nil {
					t.Fatal(err)
				}
				if status != "abandoned" {
					t.Fatalf("remote process status=%q", status)
				}
			}
		})
	}
}

func TestStoreRecoverSessionRejectsProviderAndSetupBoundaries(t *testing.T) {
	for _, kind := range []string{"sampling", "compaction", "setup"} {
		t.Run(kind, func(t *testing.T) {
			setup := ""
			if kind == "setup" {
				setup = "exit 1"
			}
			store := newTestStore(t)
			session := openSessionForStore(t, store, setup)
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if kind == "setup" {
				exitCode := 1
				if err := store.FinishSetup(t.Context(), owner, session.ID, &exitCode); err != nil {
					t.Fatal(err)
				}
			} else {
				if _, err := store.Begin(t.Context(), owner, map[string]any{"kind": kind}); err != nil {
					t.Fatal(err)
				}
				if err := store.FailExecution(t.Context(), owner, session.ID, nil, errors.New("unknown outcome")); err != nil {
					t.Fatal(err)
				}
			}
			blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
			if err != nil {
				t.Fatal(err)
			}
			if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
				t.Fatal(err)
			}
			if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "not recoverable", ""); !errors.Is(err, ErrPrecondition) {
				t.Fatalf("unsupported %s boundary recovered: %v", kind, err)
			}
		})
	}
}

func TestStoreLargeEvidenceIsFragmentedAndReconstructed(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	before, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{"type": "diagnostic", "body": strings.Repeat("界", BoundaryBytes/3+1)}
	if err := store.ProtocolEvent(t.Context(), owner, "operation-large", "executor", "received", payload); err != nil {
		t.Fatal(err)
	}
	entries, err := store.Events(t.Context(), session.ID, before.Cursor, 8)
	if err != nil {
		t.Fatal(err)
	}
	var fragments []*eventpb.BoundaryRecord
	for _, entry := range entries {
		if boundary := entry.GetEvent().GetBoundaryRecord(); boundary != nil {
			fragments = append(fragments, boundary)
		}
	}
	if len(fragments) < 2 {
		t.Fatalf("large payload was not fragmented: %d records", len(fragments))
	}
	for index, fragment := range fragments {
		if fragment.GetRecordId() != fragments[0].GetRecordId() || int(fragment.GetFragmentIndex()) != index || int(fragment.GetFragmentCount()) != len(fragments) {
			t.Fatalf("fragment %d metadata=%v", index, fragment)
		}
	}
	reconstructed := bytes.Join(func() [][]byte {
		parts := make([][]byte, len(fragments))
		for i, fragment := range fragments {
			parts[i] = fragment.GetPayload()
		}
		return parts
	}(), nil)
	want, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(reconstructed, want) {
		t.Fatalf("reconstructed boundary has %d bytes, expected %d", len(reconstructed), len(want))
	}
}

func TestStoreUnreadProcessOutputKeepsHeadAndTail(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(t.Context(), owner, "process-output", false); err != nil {
		t.Fatal(err)
	}
	head := bytes.Repeat([]byte("h"), unifiedExecOutputMaxBytes)
	if err := store.RetainProcessOutput(t.Context(), owner, "process-output", 7, head, false, nil); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "process-output", 8, []byte("tail"), false, nil); err != nil {
		t.Fatal(err)
	}
	process, err := store.Process(t.Context(), owner, "process-output")
	if err != nil {
		t.Fatal(err)
	}
	if process.AfterSequence == nil || *process.AfterSequence != 8 || len(process.BufferedOutput) != unifiedExecOutputMaxBytes ||
		process.OmittedBytes != 4 || !bytes.HasSuffix(process.BufferedOutput, []byte("tail")) || process.BufferedOutput[0] != 'h' {
		t.Fatalf("unread output: cursor=%v bytes=%d omitted=%d", process.AfterSequence, len(process.BufferedOutput), process.OmittedBytes)
	}
}

func TestStoreAbandonedProcessPreservesEvidenceAndLeavesScheduler(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "1234-unknown", 1234, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "1234-unknown", 9, []byte("observed output"), true, ptrInt(17)); err != nil {
		t.Fatal(err)
	}
	if err := store.AbandonProcess(t.Context(), owner, "1234-unknown", nil, nil, "unknown", "retained-executor"); err != nil {
		t.Fatal(err)
	}
	process, err := store.ProcessByModelSession(t.Context(), owner, 1234)
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != "abandoned" || process.AfterSequence == nil || *process.AfterSequence != 9 ||
		process.ExitCode == nil || *process.ExitCode != 17 || string(process.BufferedOutput) != "observed output" {
		t.Fatalf("abandoned evidence: %+v", process)
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Processes) != 0 {
		t.Fatalf("abandoned process remains in scheduler: %+v", state.Processes)
	}
}

func TestStoreReleasesModelSessionNumberOnExit(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "1234-first", 1234, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "1234-second", 1234, false); !errors.Is(err, ErrConflict) {
		t.Fatalf("a live session number was reissued: %v", err)
	}
	process, err := store.ProcessByModelSession(t.Context(), owner, 1234)
	if err != nil || process.ID != "1234-first" {
		t.Fatalf("session 1234 resolved to %+v err=%v", process, err)
	}
	if err := store.ProcessResult(t.Context(), owner, "1234-first", nil, "exited", ptrInt(0), true); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ProcessByModelSession(t.Context(), owner, 1234); !errors.Is(err, ErrMissing) {
		t.Fatalf("exited session still resolves: %v", err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "1234-second", 1234, false); err != nil {
		t.Fatalf("released session number was not reusable: %v", err)
	}
}

func TestCompletedCompactionReleasesCommandBeforeNextInput(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: "compact", Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	intent, err := store.BeginCompaction(t.Context(), owner, map[string]any{"kind": "compaction"}, conversation.Context())
	if err != nil {
		t.Fatal(err)
	}
	replacement := &ResponsesContext{Items: []ResponsesItem{{Type: ResponsesItemCompaction, EncryptedContent: "test-opaque"}}}
	if err := store.FinishCompaction(t.Context(), owner, intent, map[string]any{"response": "completed"}, conversation.Context(), replacement); err != nil {
		t.Fatal(err)
	}
	// No FinishCommand call: the context and command outcome must commit together.
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	owner, err = store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: "after", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "Continue"}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	command, err := store.NextCommand(t.Context(), owner, session.ID)
	if err != nil || command == nil || command.ID != "after" {
		t.Fatalf("next command=%+v err=%v", command, err)
	}
}

func TestRetainedTerminalOutputCanBeConsumedAfterTakeover(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "retained", 4242, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "retained", 7, []byte("retained-output"), true, ptrInt(0)); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	successor, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), successor, "retained", nil, "running", nil, false); !errors.As(err, new(*LostOwnershipError)) {
		t.Fatalf("successor changed old remote process state: %v", err)
	}
	executor := &Executor{store: store, owner: successor, processLocks: map[string]*sync.Mutex{}}
	invoked, err := executor.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":4242}`))
	result, _ := invoked.(*unifiedExecResult)
	if err != nil || result == nil || result.Output != "retained-output" || result.ExitCode == nil || *result.ExitCode != 0 || result.SessionID != nil {
		t.Fatalf("retained process result=%v err=%v", result, err)
	}
	process, err := store.Process(t.Context(), successor, "retained")
	if err != nil || process.Status != "exited" || len(process.BufferedOutput) != 0 {
		t.Fatalf("consumed process=%+v err=%v", process, err)
	}
}

func TestStoreInterruptIsFencedToItsTurnAndSettledAtomically(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: "turn-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	command, err := store.NextCommand(t.Context(), owner, session.ID)
	if err != nil || command.ID != "turn-1" {
		t.Fatalf("turn command=%v err=%v", command, err)
	}
	interrupt := &commandpb.Command{CommandId: "interrupt-1", Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn-1"}}}
	if _, err := store.Admit(t.Context(), session.ID, interrupt, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	requests, err := store.PendingInterrupts(t.Context(), owner)
	if err != nil || len(requests) != 1 {
		t.Fatalf("pending interrupts=%v err=%v", requests, err)
	}
	if err := store.FinishCommand(t.Context(), owner, session.ID, "turn-1", "done"); err != nil {
		t.Fatal(err)
	}
	response, err := store.GetCommand(t.Context(), session.ID, "interrupt-1")
	if err != nil {
		t.Fatal(err)
	}
	if response.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED {
		t.Fatalf("interrupt outcome=%v", response.GetOutcome())
	}
	if pending, err := store.PendingInterrupts(t.Context(), owner); err != nil || len(pending) != 0 {
		t.Fatalf("turn completion left interrupt pending: %v err=%v", pending, err)
	}
	entries, err := store.Events(t.Context(), session.ID, 0, 32)
	if err != nil {
		t.Fatal(err)
	}
	var completed bool
	for _, entry := range entries {
		if turn := entry.GetEvent().GetTurnCompleted(); turn != nil && turn.GetTurnId() == "turn-1" && turn.GetStatus() == eventpb.TurnStatus_TURN_STATUS_COMPLETED {
			completed = true
		}
	}
	if !completed {
		t.Fatal("turn completion event was not committed with interrupt settlement")
	}
}

func TestAcknowledgeCompletedInterruptDoesNotLeaveStalePendingIntent(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	requestID, err := store.RequestInterrupt(t.Context(), owner.ConversationID, owner.Generation)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AcknowledgeInterrupt(t.Context(), owner, []string{requestID}, map[string]any{
		"cleanup_complete": true,
		"status":           "interrupted",
	}); err != nil {
		t.Fatal(err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.Pending != nil {
		t.Fatalf("completed interrupt left a stale pending intent: %v", conversation.Pending)
	}
	if pending, err := store.PendingInterrupts(t.Context(), owner); err != nil || len(pending) != 0 {
		t.Fatalf("completed interrupt remained pending: %v err=%v", pending, err)
	}
}

func TestAcknowledgeIncompleteInterruptPersistsBlockingIntent(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	requestID, err := store.RequestInterrupt(t.Context(), owner.ConversationID, owner.Generation)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AcknowledgeInterrupt(t.Context(), owner, []string{requestID}, map[string]any{
		"cleanup_complete": false,
		"status":           "cleanup_uncertain",
	}); err != nil {
		t.Fatal(err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.Pending == nil || conversation.Pending.Kind != PendingIntentInterrupt {
		t.Fatalf("incomplete interrupt did not persist a blocking intent: %v", conversation.Pending)
	}
	if _, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"}); !errors.As(err, new(*UnknownEffectError)) {
		t.Fatalf("incomplete interrupt allowed a new effect: %v", err)
	}
}

func TestLateInterruptIsSettledWithItsTurn(t *testing.T) {
	for _, takeover := range []bool{false, true} {
		for _, failure := range []bool{false, true} {
			t.Run(fmt.Sprintf("takeover_%t/failure_%t", takeover, failure), func(t *testing.T) {
				store := newTestStore(t)
				session := openSessionForStore(t, store, "")
				owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
				if err != nil {
					t.Fatal(err)
				}
				for _, commandID := range []string{"first", "second"} {
					_, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
						CommandId: commandID,
						Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: commandID}},
					}, "fixture-account", nil)
					if err != nil {
						t.Fatal(err)
					}
				}
				command, err := store.NextCommand(t.Context(), owner, session.ID)
				if err != nil || command == nil || command.ID != "first" {
					t.Fatalf("first command=%v err=%v", command, err)
				}
				for _, commandID := range []string{"interrupt-one", "interrupt-two"} {
					_, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
						CommandId: commandID,
						Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "first"}},
					}, "fixture-account", nil)
					if err != nil {
						t.Fatal(err)
					}
				}
				if takeover {
					if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '5 minutes' WHERE id=$1`, session.ConversationID); err != nil {
						t.Fatal(err)
					}
					owner, err = store.Claim(t.Context(), session.ConversationID, time.Minute)
					if err != nil {
						t.Fatal(err)
					}
					command, err = store.NextCommand(t.Context(), owner, session.ID)
					if err != nil || command == nil || command.ID != "first" {
						t.Fatalf("recovered command=%v err=%v", command, err)
					}
				}
				pending, err := store.PendingInterrupts(t.Context(), owner)
				if err != nil || len(pending) == 0 {
					t.Fatalf("active turn lost its interrupt request: %v err=%v", pending, err)
				}
				if failure {
					commandID := "first"
					err = store.FailExecution(t.Context(), owner, session.ID, &commandID, &KnownFailureError{Evidence: map[string]any{"reason": "rejected before dispatch"}})
				} else {
					err = store.FinishCommand(t.Context(), owner, session.ID, "first", "done")
				}
				if err != nil {
					t.Fatal(err)
				}
				command, err = store.NextCommand(t.Context(), owner, session.ID)
				if err != nil || command == nil || command.ID != "second" {
					t.Fatalf("queued command after turn settlement=%v err=%v", command, err)
				}
				if pending, err := store.PendingInterrupts(t.Context(), owner); err != nil || len(pending) != 0 {
					t.Fatalf("turn settlement left current-generation interrupts pending: %v err=%v", pending, err)
				}
				var unresolved int
				if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_interrupt WHERE conversation_id=$1 AND result IS NULL`, session.ConversationID).Scan(&unresolved); err != nil || unresolved != 0 {
					t.Fatalf("turn settlement left interrupt rows unresolved: count=%d err=%v", unresolved, err)
				}
				for _, commandID := range []string{"interrupt-one", "interrupt-two"} {
					response, err := store.GetCommand(t.Context(), session.ID, commandID)
					if err != nil || response.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED {
						t.Fatalf("interrupt command %q outcome=%v err=%v", commandID, response.GetOutcome(), err)
					}
				}
				_, err = store.Admit(t.Context(), session.ID, &commandpb.Command{
					CommandId: "too-late",
					Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "first"}},
				}, "fixture-account", nil)
				if !errors.Is(err, ErrPrecondition) {
					t.Fatalf("ended turn accepted an interrupt: %v", err)
				}
			})
		}
	}
}

func TestKnownRejectionPreservesOtherEffectUncertainty(t *testing.T) {
	for _, uncertainty := range []string{"nested", "cell", "process"} {
		t.Run(uncertainty, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			_, err = store.Admit(t.Context(), session.ID, &commandpb.Command{
				CommandId: "first",
				Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "Start"}},
			}, "fixture-account", nil)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
				t.Fatal(err)
			}
			switch uncertainty {
			case "nested":
				if _, err := store.RecordNested(t.Context(), owner, map[string]any{"kind": "tool", "name": "exec_command"}); err != nil {
					t.Fatal(err)
				}
			case "cell":
				if err := store.CellResult(t.Context(), owner, "live-cell", "live"); err != nil {
					t.Fatal(err)
				}
			case "process":
				if err := store.RegisterProcess(t.Context(), owner, "live-process", false); err != nil {
					t.Fatal(err)
				}
			}
			intentID, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
			if err != nil {
				t.Fatal(err)
			}
			if err := store.Reject(t.Context(), owner, intentID, &KnownFailureError{Evidence: map[string]any{"http_status": 429}}); err != nil {
				t.Fatal(err)
			}
			observed, err := store.GetSession(t.Context(), session.ID, "")
			if err != nil || !observed.Blocked {
				t.Fatalf("other effect uncertainty was cleared: blocked=%v err=%v", observed.Blocked, err)
			}
			command, err := store.GetCommand(t.Context(), session.ID, "first")
			if err != nil || command.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN {
				t.Fatalf("in-flight command outcome=%v err=%v", command.GetOutcome(), err)
			}
			_, err = store.Admit(t.Context(), session.ID, &commandpb.Command{
				CommandId: "second",
				Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "Continue"}},
			}, "fixture-account", nil)
			if !errors.Is(err, ErrPrecondition) {
				t.Fatalf("blocked session accepted new input: %v", err)
			}
		})
	}
}

func TestRecoverableSessionsUsesBoundedRotatingPages(t *testing.T) {
	store := newTestStore(t)
	var sessions []*Session
	for range 3 {
		sessions = append(sessions, openSessionForStore(t, store, "prepare workspace"))
	}
	excluded := []string{sessions[0].ID}
	first, err := store.RecoverableSessions(t.Context(), excluded, 1)
	if err != nil || len(first) != 1 || first[0] == sessions[0].ID {
		t.Fatalf("bounded recovery page=%v err=%v", first, err)
	}
	second, err := store.RecoverableSessions(t.Context(), append(excluded, first[0]), 1)
	if err != nil || len(second) != 1 || second[0] == first[0] || second[0] == sessions[0].ID {
		t.Fatalf("recovery did not advance within bound: %v err=%v", second, err)
	}
	wrapped, err := store.RecoverableSessions(t.Context(), nil, 1)
	if err != nil || len(wrapped) != 1 {
		t.Fatalf("recovery did not yield bounded wraparound page: %v err=%v", wrapped, err)
	}
}

func TestGooseInitializationPreservesRetainedState(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: "stored-command", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "stored input"}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `INSERT INTO sampler_conversation_item(conversation_id,ordinal,record) VALUES($1,1,$2::jsonb)`, session.ConversationID,
		`{"type":"reasoning","provider_id":9007199254740993}`); err != nil {
		t.Fatal(err)
	}
	intentID, err := store.RecordNested(t.Context(), owner, map[string]any{"kind": "tool", "opaque_id": json.Number("9007199254740993"), "call": map[string]any{"call_id": "stored-call"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(t.Context(), owner, "stored-process", true); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "stored-process", 4, []byte("unread"), true, ptrInt(2)); err != nil {
		t.Fatal(err)
	}
	beforeEvents, err := store.Events(t.Context(), session.ID, 0, 16)
	if err != nil {
		t.Fatal(err)
	}
	if err := Migrate(t.Context(), store.DB(), map[string]APIStyle{"fixture-model": APIStyleResponses}); err != nil {
		t.Fatal(err)
	}
	afterSession, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil || afterSession.ID != session.ID || afterSession.ConversationID != session.ConversationID {
		t.Fatalf("session identity lost: %+v err=%v", afterSession, err)
	}
	afterConversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	afterItems := responsesItems(t, afterConversation.Archive)
	retainedItem, err := decodeJSONValue(afterItems[0].Raw)
	if err != nil {
		t.Fatal(err)
	}
	retainedObject, ok := retainedItem.(map[string]any)
	if !ok {
		t.Fatalf("provider continuation is not an object: %T", retainedItem)
	}
	providerID, ok := retainedObject["provider_id"].(json.Number)
	if !ok || providerID.String() != "9007199254740993" {
		t.Fatalf("provider continuation number changed: %#v (%T)", retainedObject["provider_id"], retainedObject["provider_id"])
	}
	afterEvents, err := store.Events(t.Context(), session.ID, 0, 16)
	if err != nil {
		t.Fatal(err)
	}
	if len(afterEvents) != len(beforeEvents) {
		t.Fatalf("event history changed: before=%d after=%d", len(beforeEvents), len(afterEvents))
	}
	for i := range beforeEvents {
		if !proto.Equal(beforeEvents[i], afterEvents[i]) {
			t.Fatalf("event %d changed during migration", i)
		}
	}
	intent, err := intentRequestForDB(t, store, intentID)
	if err != nil || intent["kind"] != "tool" {
		t.Fatalf("intent lost: %v err=%v", intent, err)
	}
	opaqueID, ok := intent["opaque_id"].(json.Number)
	if !ok || opaqueID.String() != "9007199254740993" {
		t.Fatalf("provider request number changed: %#v (%T)", intent["opaque_id"], intent["opaque_id"])
	}
	process, err := store.Process(t.Context(), owner, "stored-process")
	if err != nil || !bytes.Equal(process.BufferedOutput, []byte("unread")) || process.AfterSequence == nil || *process.AfterSequence != 4 {
		t.Fatalf("process state lost: %+v err=%v", process, err)
	}
	command, err := store.GetCommand(t.Context(), session.ID, "stored-command")
	if err != nil || command.GetAdmission().GetEvent().GetCommandAdmitted().GetCommand().GetCommandId() != "stored-command" {
		t.Fatalf("command identity lost: %v err=%v", command, err)
	}
}

func intentResultForDB(t *testing.T, store *Store, id string) (map[string]any, error) {
	t.Helper()
	var raw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE id=$1`, id).Scan(&raw); err != nil {
		return nil, err
	}
	var result map[string]any
	var err error
	if result, err = decodeMap(raw); err != nil {
		return nil, err
	}
	return result, nil
}

func intentRequestForDB(t *testing.T, store *Store, id string) (map[string]any, error) {
	t.Helper()
	var raw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT request FROM sampler_intent WHERE id=$1`, id).Scan(&raw); err != nil {
		return nil, err
	}
	var request map[string]any
	var err error
	if request, err = decodeMap(raw); err != nil {
		return nil, err
	}
	return request, nil
}

func ptrInt(value int) *int { return &value }

func TestDescribePreservesSessionConfigurationAndUpdatesModel(t *testing.T) {
	store := newTestStore(t)
	spec := &runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model",
		ReasoningEffort: "high", Instructions: "stored instructions",
	}
	executor := &sandboxespb.SandboxDestination{
		Owner:   &sandboxespb.ServiceAccount{Namespace: "fixture", Name: "executor"},
		Sandbox: "fixture-sandbox", SandboxUid: "fixture-sandbox-uid",
	}
	session, err := store.OpenSession(t.Context(), &samplerpb.OpenSessionRequest{
		SessionId: uuid.NewString(), Spec: spec, Executor: executor, ExpectedIncarnationUid: "fixture-incarnation-uid",
	}, "fixture-account", APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	session.Model = "updated-model"
	response, err := Describe(session)
	if err != nil {
		t.Fatal(err)
	}
	wantSpec := proto.Clone(spec).(*runnerpb.SessionSpec)
	wantSpec.Model = "updated-model"
	if !proto.Equal(response.GetAttached().GetSpec(), wantSpec) ||
		!proto.Equal(response.GetExecutor(), executor) ||
		response.GetExpectedIncarnationUid() != "fixture-incarnation-uid" {
		t.Fatalf("description lost persisted config: %v", response)
	}
}

func TestSessionEventAppendNotifiesTransactionally(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	before := session.Cursor
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_session SET cursor=cursor WHERE id=$1`, session.ID); err != nil {
		t.Fatal(err)
	}
	entries, err := store.Events(t.Context(), session.ID, before, 1)
	if err != nil || len(entries) != 0 {
		t.Fatalf("no-op update created event: %v err=%v", entries, err)
	}
	_, err = store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: fmt.Sprintf("event-%d", before), Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "wake"}}}, "fixture-account", nil)
	if err != nil {
		t.Fatal(err)
	}
	entries, err = store.Events(t.Context(), session.ID, before, 1)
	if err != nil || len(entries) != 1 || entries[0].GetCursor() != before+1 {
		t.Fatalf("admission event not committed: %v err=%v", entries, err)
	}
	var count int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_event WHERE session_id=$1 AND sequence=$2`, session.ID, entries[0].GetCursor()).Scan(&count); err != nil || count != 1 {
		t.Fatalf("event and cursor disagree: count=%d err=%v", count, err)
	}
}

func TestFailedActiveInputFoldsAsConfirmedInputAndFailedTurn(t *testing.T) {
	for _, test := range []struct {
		name               string
		failure            error
		wantActive         samplerpb.CommandOutcome
		wantQueued         samplerpb.CommandOutcome
		wantQueueError     bool
		wantReasonContains string
	}{
		{
			name:       "known failure",
			failure:    &KnownFailureError{Message: "provider rejected request"},
			wantActive: samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED,
			wantQueued: samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED,
		},
		{
			name:           "unknown loss",
			wantActive:     samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN,
			wantQueued:     samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED,
			wantQueueError: true,
		},
		{
			name:               "ordinary execution error",
			failure:            errors.New("unexpected EOF while reading provider stream"),
			wantActive:         samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN,
			wantQueued:         samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED,
			wantQueueError:     true,
			wantReasonContains: "unexpected EOF while reading provider stream",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			for _, commandID := range []string{"active-input", "queued-input"} {
				_, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
					CommandId: commandID,
					Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: commandID}},
				}, "fixture-account", nil)
				if err != nil {
					t.Fatal(err)
				}
			}
			active, err := store.NextCommand(t.Context(), owner, session.ID)
			if err != nil || active == nil || active.ID != "active-input" {
				t.Fatalf("active command=%v err=%v", active, err)
			}
			if err := store.FailExecution(t.Context(), owner, session.ID, &active.ID, test.failure); err != nil {
				t.Fatal(err)
			}

			for commandID, want := range map[string]samplerpb.CommandOutcome{
				"active-input": test.wantActive,
				"queued-input": test.wantQueued,
			} {
				response, err := store.GetCommand(t.Context(), session.ID, commandID)
				if err != nil {
					t.Fatal(err)
				}
				if response.GetOutcome() != want {
					t.Fatalf("GetCommand(%q) outcome=%v, want %v", commandID, response.GetOutcome(), want)
				}
				if commandID == "active-input" && test.wantReasonContains != "" && !strings.Contains(response.GetFailureReason(), test.wantReasonContains) {
					t.Fatalf("GetCommand(%q) failure_reason=%q, want it to contain %q", commandID, response.GetFailureReason(), test.wantReasonContains)
				}
			}
			if test.wantReasonContains != "" {
				persisted, err := store.GetSession(t.Context(), session.ID, "")
				if err != nil {
					t.Fatal(err)
				}
				if persisted.BlockedReason == nil || !strings.Contains(*persisted.BlockedReason, test.wantReasonContains) {
					t.Fatalf("GetSession blocked_reason=%v, want it to contain %q", persisted.BlockedReason, test.wantReasonContains)
				}
			}

			entries, err := store.Events(t.Context(), session.ID, 0, 32)
			if err != nil {
				t.Fatal(err)
			}
			if len(entries) == 0 {
				t.Fatal("store persisted no session events")
			}
			batch := threadfold.EventBatch{
				SourceID:    entries[0].GetOrigin().GetSourceId(),
				AfterCursor: 0,
				Entries:     entries,
			}
			state, err := threadfold.Initial(batch.SourceID, "fixture-projection")
			if err != nil {
				t.Fatal(err)
			}
			prior := threadfold.PriorEntities{Commands: make(map[string]*threadfold.CommandSummary)}
			for _, commandID := range threadfold.Touched(batch).CommandIDs {
				prior.Commands[commandID] = nil
			}
			folded, err := threadfold.Advance(state, batch, prior)
			if err != nil {
				t.Fatalf("fold persisted sampler events: %v", err)
			}
			outcomes := make(map[string]threadfold.CommandOutcome, len(folded.CommandUpserts))
			for _, command := range folded.CommandUpserts {
				outcomes[command.CommandID] = command.Outcome
			}
			if outcomes["active-input"] != threadfold.OutcomeEffected {
				t.Fatalf("confirmed input history outcome=%q, want %q", outcomes["active-input"], threadfold.OutcomeEffected)
			}
			if test.wantQueueError && outcomes["queued-input"] != threadfold.OutcomeFailed {
				t.Fatalf("queued input history outcome=%q, want %q", outcomes["queued-input"], threadfold.OutcomeFailed)
			}
			if !test.wantQueueError && outcomes["queued-input"] != threadfold.OutcomePending {
				t.Fatalf("queued input history outcome=%q, want %q", outcomes["queued-input"], threadfold.OutcomePending)
			}
			var failedTurn, failedTurnReason bool
			for _, lifecycle := range folded.LifecycleUpserts {
				turn := lifecycle.Event.GetTurnCompleted()
				if turn != nil && turn.GetTurnId() == "active-input" && turn.GetStatus() == eventpb.TurnStatus_TURN_STATUS_FAILED {
					failedTurn = true
					failedTurnReason = test.wantReasonContains == "" || strings.Contains(turn.GetError(), test.wantReasonContains)
				}
			}
			if !failedTurn {
				t.Fatal("History fold did not retain the failed active turn")
			}
			if !failedTurnReason {
				t.Fatal("History fold did not retain the original driver error in the failed turn")
			}
		})
	}
}

func admitInput(t *testing.T, store *Store, sessionID, commandID, text string) {
	t.Helper()
	if _, err := store.Admit(t.Context(), sessionID, &commandpb.Command{CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
}

func TestSteeringInputJoinsTheActiveTurnOnceInAdmissionOrder(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	admitInput(t, store, session.ID, "turn-1", "start")
	if command, err := store.NextCommand(t.Context(), owner, session.ID); err != nil || command.ID != "turn-1" {
		t.Fatalf("turn command=%v err=%v", command, err)
	}
	admitInput(t, store, session.ID, "steer-a", "steering text")
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: "model", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "fixture-model-2"}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	admitInput(t, store, session.ID, "after-model", "next turn text")
	// A running cell's unresolved intent still reports the input, so its observation can yield, but
	// the input only joins once the intent has an outcome.
	intent, err := store.Begin(t.Context(), owner, map[string]any{"kind": "cell"})
	if err != nil {
		t.Fatal(err)
	}
	if found, err := store.HasSteeringInput(t.Context(), owner, session.ID); err != nil || !found {
		t.Fatalf("steering input during a running cell found=%v err=%v", found, err)
	}
	if drained, err := store.DrainSteeringInput(t.Context(), owner, session.ID); err != nil || drained {
		t.Fatalf("drain during a running cell=%v err=%v", drained, err)
	}
	if err := store.Finish(t.Context(), owner, intent, map[string]any{"output": "fixture"}, nil, nil); err != nil {
		t.Fatal(err)
	}
	if drained, err := store.DrainSteeringInput(t.Context(), owner, session.ID); err != nil || !drained {
		t.Fatalf("first drain=%v err=%v", drained, err)
	}
	// Input admitted behind a model change waits for a later turn, and nothing drains twice.
	if drained, err := store.DrainSteeringInput(t.Context(), owner, session.ID); err != nil || drained {
		t.Fatalf("second drain=%v err=%v", drained, err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	expected, err := newResponsesMessage("user", "steering text")
	if err != nil {
		t.Fatal(err)
	}
	// Stored and freshly built items serialize their keys in different orders.
	normalized := func(item ResponsesItem) string {
		var fields map[string]any
		if err := json.Unmarshal([]byte(canonicalJSON(item)), &fields); err != nil {
			t.Fatal(err)
		}
		encoded, err := json.Marshal(fields)
		if err != nil {
			t.Fatal(err)
		}
		return string(encoded)
	}
	items := responsesItems(t, conversation.Archive)
	if got, want := normalized(items[len(items)-1]), normalized(expected); got != want {
		t.Fatalf("last conversation item=%s, want %s", got, want)
	}
	for id, want := range map[string]samplerpb.CommandOutcome{
		"steer-a":     samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED,
		"after-model": samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED,
	} {
		response, err := store.GetCommand(t.Context(), session.ID, id)
		if err != nil || response.GetOutcome() != want {
			t.Fatalf("%s outcome=%v err=%v, want %v", id, response.GetOutcome(), err, want)
		}
	}
	entries, err := store.Events(t.Context(), session.ID, 0, 64)
	if err != nil {
		t.Fatal(err)
	}
	joined := false
	for _, entry := range entries {
		if message := entry.GetEvent().GetHarnessUserMessageConfirmed(); message != nil && message.GetHarnessMessageId() == "steer-a" {
			joined = message.GetTurnId() == "turn-1" && message.GetText() == "steering text"
		}
		if turn := entry.GetEvent().GetTurnStarted(); turn != nil && turn.GetTurnId() == "steer-a" {
			t.Fatal("steering input started its own turn")
		}
	}
	if !joined {
		t.Fatal("steering input was not confirmed into the active turn")
	}
	if err := store.FinishCommand(t.Context(), owner, session.ID, "turn-1", "done"); err != nil {
		t.Fatal(err)
	}
	if command, err := store.NextCommand(t.Context(), owner, session.ID); err != nil || command.ID != "model" {
		t.Fatalf("next command=%v err=%v, want model", command, err)
	}
	if command, err := store.NextCommand(t.Context(), owner, session.ID); err != nil || command.ID != "after-model" {
		t.Fatalf("next command=%v err=%v, want after-model as a new turn", command, err)
	}
}

func TestPendingInterruptTakesPrecedenceOverSteering(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	admitInput(t, store, session.ID, "turn-1", "start")
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{CommandId: "interrupt-1", Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn-1"}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	admitInput(t, store, session.ID, "steer", "too late")
	if drained, err := store.DrainSteeringInput(t.Context(), owner, session.ID); err != nil || drained {
		t.Fatalf("drain during pending interrupt=%v err=%v", drained, err)
	}
}

func TestStoreReopeningIgnoresRetiredSessionRouting(t *testing.T) {
	store := newTestStore(t)
	request := &samplerpb.OpenSessionRequest{SessionId: uuid.NewString(), Spec: &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model"}}
	legacy := proto.Clone(request).(*samplerpb.OpenSessionRequest)
	legacy.Spec.ProtoReflect().SetUnknown([]byte{0x32, 0x00})
	if _, err := store.OpenSession(t.Context(), legacy, "fixture-account", APIStyleResponses); err != nil {
		t.Fatal(err)
	}
	if _, err := store.OpenSession(t.Context(), request, "fixture-account", APIStyleResponses); err != nil {
		t.Fatalf("reopening with retired metadata: %v", err)
	}
	request.Spec.Model = "different"
	if _, err := store.OpenSession(t.Context(), request, "fixture-account", APIStyleResponses); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed session spec accepted: %v", err)
	}
}
