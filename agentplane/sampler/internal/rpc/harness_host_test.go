package rpc

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sampleradminpb "github.com/agentydragon/ducktape/agentplane/sampler/api/sampleradminpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func newHostFixture(t *testing.T) (*HarnessHost, string) {
	t.Helper()
	dsn := rpcIntegrationDatabase.Clone(t)
	store := sessionstore.New(postgres.Open(t, dsn), 30*time.Second)
	hub, err := wakeups.Listen(t.Context(), dsn, sessionstore.OutboxChannel, sessionstore.WorkChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	settings := fixtureSettings()
	settings.ServiceCallers = append(settings.ServiceCallers, peertls.ActionsURI)
	return &HarnessHost{Sampler: &Sampler{Store: store, Runtime: &residentruntime.Runtime{}, Settings: settings, Wakeups: hub}}, dsn
}

func hostCreateRequest() *harnesshostpb.CreateSessionRequest {
	r := &sessionconfig.Configuration{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "test-model"}}
	testsupport.FillSessionConfigurationDefaults(r)
	return &harnesshostpb.CreateSessionRequest{SessionId: r.SessionId, Spec: r.Spec, ExecutionIdentity: r.ExecutionIdentity}
}

func TestHarnessHostUsesSamplerAdmissionAndFencing(t *testing.T) {
	h, _ := newHostFixture(t)
	transport := &hostTransport{}
	ctx := grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.SessionsURI), transport)
	request := hostCreateRequest()
	if _, err := h.CreateSession(ctx, request); err != nil {
		t.Fatal(err)
	}
	// An exact retry confirms the immutable admission row.
	again, err := h.CreateSession(ctx, request)
	if err != nil || again.GetAttached() == nil {
		t.Fatalf("retry=%v err=%v", again, err)
	}
	changed := proto.Clone(request).(*harnesshostpb.CreateSessionRequest)
	changed.Spec.Cwd = "/changed"
	if _, err := h.CreateSession(ctx, changed); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("changed creation=%v", err)
	}
	input := &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input", Text: "hello"}
	admitted, err := h.SubmitInput(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input", Text: "hello"})
	if err != nil || !proto.Equal(admitted.GetAdmission(), retry.GetAdmission()) {
		t.Fatalf("shared admission=%v err=%v", retry, err)
	}
	input.Text = "different"
	if _, err := h.SubmitInput(ctx, input); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("command collision=%v", err)
	}
	if _, err := h.SubmitInput(serviceContext(t, peertls.ActionsURI), input); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("cross-owner admission=%v", err)
	}
	if _, err := h.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{SessionId: request.SessionId, CommandId: "model", Model: "test-model"}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{SessionId: request.SessionId, CommandId: "effort", Effort: "high"}); err != nil {
		t.Fatal(err)
	}
	session, err := h.Sampler.Store.GetSession(t.Context(), request.SessionId, peertls.SessionsURI)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := h.Sampler.Store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := h.Sampler.Store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	_, staleErr := h.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{SessionId: session.ID, CommandId: "stale", TurnId: "earlier-turn"})
	if status.Code(staleErr) != codes.FailedPrecondition {
		t.Fatalf("stale interrupt=%v", staleErr)
	}
	if _, err := h.Sampler.Store.GetCommand(ctx, session.ID, "stale"); !errors.Is(err, protocol.ErrMissing) {
		t.Fatalf("stale interrupt admission=%v, want missing", err)
	}
	if _, err := h.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{SessionId: session.ID, CommandId: "interrupt", TurnId: "input"}); err != nil {
		t.Fatal(err)
	}
	listed, err := h.ListSessions(ctx, &harnesshostpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.Sessions[0].GetActiveTurnId() != "input" || listed.Sessions[0].GetPendingCommands() != 3 {
		t.Fatalf("list=%v err=%v", listed, err)
	}
	if err := harnesshost.RequireSessionVersion(transport.header); err != nil {
		t.Fatal(err)
	}
	hidden, err := h.ListSessions(grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.ActionsURI), &hostTransport{}), &harnesshostpb.ListSessionsRequest{})
	if err != nil || len(hidden.GetSessions()) != 0 {
		t.Fatalf("cross-owner list=%v err=%v", hidden, err)
	}
	if err := h.Sampler.Store.Release(ctx, owner); err != nil {
		t.Fatal(err)
	}
	_, unownedErr := h.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{SessionId: session.ID, CommandId: "unowned", TurnId: "input"})
	if status.Code(unownedErr) != codes.FailedPrecondition || status.Convert(unownedErr).Message() == status.Convert(staleErr).Message() {
		t.Fatalf("unowned interrupt=%v, want FailedPrecondition with a different reason from %v", unownedErr, staleErr)
	}
}

func TestHarnessHostBlockedAdmissionPreservesRetryAndKeepsRecoveryEvidencePrivate(t *testing.T) {
	h, _ := newHostFixture(t)
	ctx := serviceContext(t, peertls.SessionsURI)
	request := hostCreateRequest()
	if _, err := h.CreateSession(ctx, request); err != nil {
		t.Fatal(err)
	}
	input := &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "first", Text: "hello"}
	admitted, err := h.SubmitInput(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	session, err := h.Sampler.Store.GetSession(ctx, request.SessionId, peertls.SessionsURI)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := h.Sampler.Store.Claim(ctx, session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := h.Sampler.Store.NextCommand(ctx, owner, session.ID); err != nil {
		t.Fatal(err)
	}
	const privateEvidence = "private test executor output"
	if err := h.Sampler.Store.FailExecution(ctx, owner, session.ID, &input.CommandId, errors.New(privateEvidence)); err != nil {
		t.Fatal(err)
	}
	blocked, err := h.Sampler.Store.GetSession(ctx, session.ID, peertls.SessionsURI)
	if err != nil || blocked == nil || !blocked.Blocked || blocked.BlockedReason == nil || !strings.Contains(*blocked.BlockedReason, privateEvidence) {
		t.Fatalf("blocked session=%+v err=%v, want retained evidence", blocked, err)
	}
	_, err = h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: session.ID, CommandId: "second", Text: "continue"})
	if status.Code(err) != codes.FailedPrecondition || strings.Contains(status.Convert(err).Message(), privateEvidence) {
		t.Fatalf("blocked session admission=%v, want FailedPrecondition without retained evidence", err)
	}
	if _, err := h.Sampler.Store.GetCommand(ctx, session.ID, "second"); !errors.Is(err, protocol.ErrMissing) {
		t.Fatalf("new command admission=%v, want missing", err)
	}
	retry, err := h.SubmitInput(ctx, input)
	if err != nil || !proto.Equal(admitted.GetAdmission(), retry.GetAdmission()) {
		t.Fatalf("exact retry while blocked=%v err=%v", retry, err)
	}
}

func TestSamplerRefusesUnsupportedEffortAndCompaction(t *testing.T) {
	h, _ := newHostFixture(t)
	h.Sampler.Settings.Models["fixed-effort"] = samplersettings.ModelConfig{
		Connection: "provider", ProviderModel: "fixed-effort", API: protocol.APIStyleAnthropicMessages,
		AnthropicMessages: &anthropic.Config{MaxTokens: 1024}, ExecEncoding: protocol.ExecEncodingJSON,
		ReasoningEfforts: []string{"low", "high"},
	}
	h.Sampler.Settings.AdminServiceCallers = []string{peertls.SessionsURI}
	admin := SamplerAdmin{Sampler: *h.Sampler}
	ctx := serviceContext(t, peertls.SessionsURI)
	request := hostCreateRequest()
	request.Spec.Model = "fixed-effort"
	request.Spec.ReasoningEffort = "low"
	if _, err := h.CreateSession(ctx, request); err != nil {
		t.Fatal(err)
	}
	_, unsupported := h.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{SessionId: request.SessionId, CommandId: "unsupported", Effort: "medium"})
	_, fixed := h.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{SessionId: request.SessionId, CommandId: "fixed", Effort: "high"})
	_, compact := admin.Compact(ctx, &sampleradminpb.CompactRequest{SessionId: request.SessionId, CommandId: "compact"})
	for _, refusal := range []struct {
		commandID string
		err       error
		want      codes.Code
	}{
		{"unsupported", unsupported, codes.InvalidArgument},
		{"fixed", fixed, codes.FailedPrecondition},
		{"compact", compact, codes.FailedPrecondition},
	} {
		if status.Code(refusal.err) != refusal.want {
			t.Errorf("%s=%v, want %s", refusal.commandID, refusal.err, refusal.want)
		}
		if _, err := h.Sampler.Store.GetCommand(ctx, request.SessionId, refusal.commandID); !errors.Is(err, protocol.ErrMissing) {
			t.Errorf("%s admission=%v, want missing", refusal.commandID, err)
		}
	}
	if status.Convert(fixed).Message() == status.Convert(compact).Message() {
		t.Fatalf("fixed effort and unavailable compaction share a reason: %v", fixed)
	}
}

type hostWatchStream struct {
	grpc.ServerStream
	ctx      context.Context
	messages chan *harnesshostpb.WatchSessionsResponse
	header   metadata.MD
}

func (s *hostWatchStream) SendHeader(header metadata.MD) error { s.header = header.Copy(); return nil }

func (s *hostWatchStream) Context() context.Context { return s.ctx }
func (s *hostWatchStream) Send(r *harnesshostpb.WatchSessionsResponse) error {
	select {
	case s.messages <- r:
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}

func hostWatchBatch(t *testing.T, stream *hostWatchStream) []*harnesshostpb.Session {
	t.Helper()
	timer := time.NewTimer(5 * time.Second)
	defer timer.Stop()
	var sessions []*harnesshostpb.Session
	for {
		select {
		case frame := <-stream.messages:
			if frame.GetCaughtUp() != nil {
				if err := harnesshost.RequireSessionVersion(stream.header); err != nil {
					t.Fatal(err)
				}
				return sessions
			}
			if frame.GetUpsert() == nil {
				t.Fatalf("invalid frame=%v", frame)
			}
			sessions = append(sessions, frame.GetUpsert())
		case <-timer.C:
			t.Fatal("watch did not catch up")
			return nil
		}
	}
}

func TestHarnessHostWatchSnapshotChangesAndReconnect(t *testing.T) {
	h, _ := newHostFixture(t)
	ctx := serviceContext(t, peertls.SessionsURI)
	original := hostCreateRequest()
	if _, err := h.CreateSession(ctx, original); err != nil {
		t.Fatal(err)
	}
	if _, err := h.CreateSession(serviceContext(t, peertls.ActionsURI), hostCreateRequest()); err != nil {
		t.Fatal(err)
	}
	watchCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream := &hostWatchStream{ctx: watchCtx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 32)}
	done := make(chan error, 1)
	go func() { done <- h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream) }()
	if batch := hostWatchBatch(t, stream); len(batch) != 1 || batch[0].SessionId != original.SessionId {
		t.Fatalf("snapshot=%v", batch)
	}
	created := hostCreateRequest()
	if _, err := h.CreateSession(ctx, created); err != nil {
		t.Fatal(err)
	}
	for {
		batch := hostWatchBatch(t, stream)
		if len(batch) == 0 {
			continue
		} // coalesced wakeups may include an unchanged batch
		if len(batch) != 1 || batch[0].SessionId != created.SessionId {
			t.Fatalf("new session batch=%v", batch)
		}
		break
	}
	if _, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: original.SessionId, CommandId: "queued", Text: "hello"}); err != nil {
		t.Fatal(err)
	}
	for {
		batch := hostWatchBatch(t, stream)
		if len(batch) == 0 {
			continue
		}
		if len(batch) != 1 || batch[0].SessionId != original.SessionId || batch[0].PendingCommands != 1 {
			t.Fatalf("changed batch=%v", batch)
		}
		break
	}
	cancel()
	if err := <-done; status.Code(err) != codes.Canceled {
		t.Fatalf("watch end=%v", err)
	}
	// A new stream starts with every currently visible Session, including unchanged ones.
	reconnectCtx, reconnectCancel := context.WithCancel(ctx)
	reconnect := &hostWatchStream{ctx: reconnectCtx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 32)}
	go func() { done <- h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, reconnect) }()
	if batch := hostWatchBatch(t, reconnect); len(batch) != 2 {
		t.Fatalf("reconnect snapshot=%v", batch)
	}
	reconnectCancel()
	<-done
}

func TestHarnessHostRejectsUnadmittedService(t *testing.T) {
	h, _ := newHostFixture(t)
	ctx := serviceContext(t, peertls.HistoryIngressURI)
	if _, err := h.CreateSession(ctx, hostCreateRequest()); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("creation=%v", err)
	}
	if _, err := h.ListSessions(ctx, &harnesshostpb.ListSessionsRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("list=%v", err)
	}
	stream := &hostWatchStream{ctx: ctx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 1)}
	if err := h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("watch=%v", err)
	}
	if len(stream.messages) != 0 {
		t.Fatal("unadmitted watch exposed status")
	}
}

func TestHarnessHostWatchEndsOnWakeupConnectionLoss(t *testing.T) {
	h, dsn := newHostFixture(t)
	ctx, cancel := context.WithCancel(serviceContext(t, peertls.SessionsURI))
	defer cancel()
	stream := &hostWatchStream{ctx: ctx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 32)}
	done := make(chan error, 1)
	go func() { done <- h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream) }()
	if batch := hostWatchBatch(t, stream); len(batch) != 0 {
		t.Fatalf("initial snapshot=%v", batch)
	}
	// Losing LISTEN may lose committed changes. End the stream even if the
	// listener reconnects before its buffered wakeup is consumed.
	db := postgres.Open(t, dsn)
	var terminated bool
	if err := db.QueryRowContext(t.Context(), `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname=current_database() AND pid<>pg_backend_pid() AND query LIKE 'LISTEN %'`).Scan(&terminated); err != nil {
		t.Fatal(err)
	}
	if !terminated {
		t.Fatal("listener was not terminated")
	}
	select {
	case err := <-done:
		if status.Code(err) != codes.Unavailable {
			t.Fatalf("disconnected watch=%v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("disconnected watch did not end")
	}
}

type hostTransport struct{ header metadata.MD }

func (s *hostTransport) Method() string { return "/agentplane.harness_host.HarnessHost/ListSessions" }

func (s *hostTransport) SetHeader(header metadata.MD) error {
	s.header = metadata.Join(s.header, header)
	return nil
}
func (s *hostTransport) SendHeader(header metadata.MD) error { return s.SetHeader(header) }
func (s *hostTransport) SetTrailer(metadata.MD) error        { return nil }

func TestModelChangeExplainsRejectedResponsesSession(t *testing.T) {
	h, _ := newHostFixture(t)
	ctx := grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.SessionsURI), &hostTransport{})
	request := hostCreateRequest()
	if _, err := h.CreateSession(ctx, request); err != nil {
		t.Fatal(err)
	}
	replacement := h.Sampler.Settings.Models["test-model"]
	replacement.ProviderModel = "another-model"
	h.Sampler.Settings.Models["replacement"] = replacement
	change := &harnesshostpb.ChangeModelRequest{SessionId: request.SessionId, CommandId: "change", Model: "replacement"}
	_, err := h.ChangeModel(ctx, change)
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(status.Convert(err).Message(), "sampler policy") {
		t.Fatalf("incompatible change: %v", err)
	}
	delete(h.Sampler.Settings.Models, "test-model")
	_, err = h.ChangeModel(ctx, change)
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(status.Convert(err).Message(), "current model configuration is no longer available") {
		t.Fatalf("missing current model: %v", err)
	}
	_, err = h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input", Text: "continue"})
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(status.Convert(err).Message(), "selected model is no longer configured") {
		t.Fatalf("input with missing model: %v", err)
	}
}

func TestChatGPTResponsesModelChangeContinuesRetainedConversation(t *testing.T) {
	for _, scenario := range []string{"configured", "removed", "compacted", "foreign-history", "foreign-compaction"} {
		t.Run(scenario, func(t *testing.T) {
			h, _ := newHostFixture(t)
			settings, store := &h.Sampler.Settings, h.Sampler.Store
			ctx := grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.SessionsURI), &hostTransport{})
			const oldModel = "chatgpt-sub/oai-responses/fixture-old"
			const newModel = "chatgpt-sub/oai-responses/fixture-new"
			connection := samplersettings.ConnectionConfig{
				BaseURL: "https://ingress.example.test",
				Auth:    samplersettings.AuthConfig{Type: samplersettings.AuthTypeAgentplaneIngress},
			}
			old := settings.Models["test-model"]
			old.Connection, old.ProviderModel = "fixture-ingress", oldModel
			replacement := old
			replacement.ProviderModel = newModel
			settings.Connections[old.Connection] = connection
			settings.Models[oldModel], settings.Models[newModel] = old, replacement
			chat := samplersettings.ModelConfig{Connection: old.Connection, ProviderModel: "fixture-chat", API: protocol.APIStyleChatCompletions, ExecEncoding: protocol.ExecEncodingJSON, ReasoningEfforts: []string{""}}
			settings.Models["fixture-chat"] = chat
			standard := replacement
			standard.Responses.Dialect = responses.DialectStandard
			settings.Models["fixture-standard"] = standard
			otherProvider := replacement
			otherProvider.ProviderModel = "fixture-account/oai-responses/fixture-new"
			settings.Models["fixture-other-provider"] = otherProvider
			otherConnection := replacement
			otherConnection.Connection = "other-ingress"
			settings.Connections[otherConnection.Connection] = connection
			settings.Models["fixture-other-connection"] = otherConnection

			request := hostCreateRequest()
			request.Spec.Model = oldModel
			if _, err := h.CreateSession(ctx, request); err != nil {
				t.Fatal(err)
			}
			session, err := store.GetSession(ctx, request.SessionId, peertls.SessionsURI)
			if err != nil {
				t.Fatal(err)
			}
			owner, err := store.Claim(ctx, session.ConversationID, time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			submit := func(id, message string) error {
				_, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: session.ID, CommandId: id, Text: message})
				return err
			}
			switchTo := func(id, model string) error {
				_, err := h.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{SessionId: session.ID, CommandId: id, Model: model})
				return err
			}
			next := func(id string) {
				t.Helper()
				command, err := store.NextCommand(ctx, owner, session.ID)
				if err != nil || command == nil || command.ID != id {
					t.Fatalf("next command=%v err=%v, want %s", command, err, id)
				}
			}
			if err := submit("initial", "Remember the fixture's earlier request."); err != nil {
				t.Fatal(err)
			}
			if err := switchTo("queued", newModel); status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "unfinished work") {
				t.Fatalf("change with earlier queued input: %v", err)
			}
			next("initial")
			if err := switchTo("active", newModel); status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "unfinished work") {
				t.Fatalf("change during an active turn: %v", err)
			}
			source := old
			if scenario == "foreign-history" {
				source.ProviderModel = otherProvider.ProviderModel
			}
			prep := providerrequest.Preparation{ModelConfig: source, SessionConfiguration: session.Configuration}
			initial, _, err := store.Begin(ctx, owner, sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, connection, source), &prep, providerAttemptTestPolicyPointer())
			if err != nil {
				t.Fatal(err)
			}
			records := []json.RawMessage{
				json.RawMessage(`{"type":"reasoning","id":"rs_fixture","summary":[],"encrypted_content":"fixture-opaque-reasoning"}`),
				json.RawMessage(`{"type":"message","id":"msg_fixture","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"Earlier answer"}]}`),
			}
			if err := store.Finish(ctx, owner, initial, map[string]any{"status": "completed"}, records, nil); err != nil {
				t.Fatal(err)
			}
			if err := store.FinishCommand(ctx, owner, session.ID, "initial", sessionstore.TurnDone); err != nil {
				t.Fatal(err)
			}

			compactionItem := json.RawMessage(`{"type":"compaction","id":"cmp_fixture","encrypted_content":"fixture-summary","created_by":"fixture-provider"}`)
			if scenario == "compacted" || scenario == "foreign-compaction" {
				conversation, err := store.Read(ctx, owner)
				if err != nil {
					t.Fatal(err)
				}
				prep.Compaction = true
				if scenario == "foreign-compaction" {
					prep.ModelConfig = otherProvider
				}
				request := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentCompaction, connection, prep.ModelConfig)
				compaction, _, err := store.BeginCompaction(ctx, owner, request, conversation.Context(), &prep, *providerAttemptTestPolicyPointer())
				if err != nil {
					t.Fatal(err)
				}
				compacted, err := responses.ValidateCompaction(conversation.Context().(*responses.Context), []json.RawMessage{compactionItem}, nil, nil, "Fixture environment context")
				if err != nil {
					t.Fatal(err)
				}
				if err := store.FinishCompaction(ctx, owner, compaction, map[string]any{"status": "completed"}, conversation.Context(), compacted); err != nil {
					t.Fatal(err)
				}
			}
			if scenario != "configured" {
				delete(settings.Models, oldModel)
				if err := submit("before-switch", "Cannot submit on the retired model."); status.Code(err) != codes.FailedPrecondition {
					t.Fatalf("input on missing model: %v", err)
				}
			}
			for _, model := range []string{"fixture-chat", "fixture-standard", "fixture-other-provider", "fixture-other-connection"} {
				if err := switchTo("reject-"+model, model); status.Code(err) != codes.FailedPrecondition {
					t.Fatalf("change to %q: %v", model, err)
				}
			}
			if err := switchTo("switch", newModel); scenario == "foreign-history" || scenario == "foreign-compaction" {
				if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "current model configuration is no longer available") {
					t.Fatalf("change with a different retained provider account: %v", err)
				}
				return
			} else if err != nil {
				t.Fatal(err)
			}
			if err := submit("continue", "Continue on the replacement."); err != nil {
				t.Fatalf("input immediately after queued switch: %v", err)
			}
			next("switch")
			next("continue")
			session, err = store.GetSession(ctx, session.ID, peertls.SessionsURI)
			if err != nil || session.Model != newModel || session.Blocked {
				t.Fatalf("session after change=%+v err=%v", session, err)
			}
			prep.ModelConfig = replacement
			prep.Compaction = false
			_, body, err := store.Begin(ctx, owner, sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, connection, replacement), &prep, providerAttemptTestPolicyPointer())
			if err != nil {
				t.Fatal(err)
			}
			var continued struct {
				Model string
				Input []json.RawMessage
			}
			if err := json.Unmarshal(body, &continued); err != nil {
				t.Fatal(err)
			}
			if continued.Model != newModel {
				t.Fatalf("continued provider model = %q, want %q", continued.Model, newModel)
			}
			var input strings.Builder
			for _, item := range continued.Input {
				input.Write(item)
			}
			fragments := []string{"Remember the fixture's earlier request.", "fixture-opaque-reasoning", "Earlier answer", "Continue on the replacement."}
			if scenario == "compacted" {
				sameItem := func(actual, expected json.RawMessage) bool {
					var actualFields, expectedFields map[string]any
					if err := json.Unmarshal(actual, &actualFields); err != nil {
						t.Fatal(err)
					}
					if err := json.Unmarshal(expected, &expectedFields); err != nil {
						t.Fatal(err)
					}
					return reflect.DeepEqual(actualFields, expectedFields)
				}
				if !slices.ContainsFunc(continued.Input, func(item json.RawMessage) bool { return sameItem(item, compactionItem) }) {
					t.Fatalf("continuation omitted or changed the opaque compaction item: %s", body)
				}
				if strings.Contains(input.String(), "fixture-opaque-reasoning") {
					t.Fatalf("continuation sent the full transcript instead of the active context: %s", body)
				}
				fragments = []string{"Fixture environment context", "fixture-summary", "Continue on the replacement."}
				conversation, err := store.Read(ctx, owner)
				if err != nil {
					t.Fatal(err)
				}
				transcript, err := conversation.Transcript.Records()
				if err != nil || !slices.ContainsFunc(transcript, func(item json.RawMessage) bool { return sameItem(item, records[0]) }) {
					t.Fatalf("retained reasoning changed after switch: %s, %v", transcript, err)
				}
			}
			at := -1
			for _, fragment := range fragments {
				index := strings.Index(input.String(), fragment)
				if index <= at {
					t.Fatalf("missing or out-of-order %q in continued request: %s", fragment, body)
				}
				at = index
			}
		})
	}
}

func TestRemovedChatRouteCanSwitchToConfiguredChatRoute(t *testing.T) {
	h, _ := newHostFixture(t)
	chat := samplersettings.ModelConfig{Connection: "provider", ProviderModel: "old-provider-model", API: protocol.APIStyleChatCompletions, ExecEncoding: protocol.ExecEncodingJSON, ReasoningEfforts: []string{""}}
	h.Sampler.Settings.Models["old-chat"] = chat
	chat.ProviderModel = "replacement-provider-model"
	h.Sampler.Settings.Models["new-chat"] = chat
	ctx := grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.SessionsURI), &hostTransport{})
	request := hostCreateRequest()
	request.Spec.Model = "old-chat"
	if _, err := h.CreateSession(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "old-input", Text: "remember this"}); err != nil {
		t.Fatal(err)
	}
	session, err := h.Sampler.Store.GetSession(t.Context(), request.SessionId, peertls.SessionsURI)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := h.Sampler.Store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	command, err := h.Sampler.Store.NextCommand(t.Context(), owner, session.ID)
	if err != nil || command == nil || command.ID != "old-input" {
		t.Fatalf("old input=%v err=%v", command, err)
	}
	delete(h.Sampler.Settings.Models, "old-chat")
	if _, err := h.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{SessionId: request.SessionId, CommandId: "while-active", Model: "new-chat"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("active turn: %v", err)
	}
	if err := h.Sampler.Store.FailExecution(t.Context(), owner, session.ID, &command.ID, &protocol.KnownFailureError{Message: "The selected model route is no longer configured."}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input-before-switch", Text: "hello"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("input with missing route: %v", err)
	}
	if _, err := h.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{SessionId: request.SessionId, CommandId: "wrong-protocol", Model: "test-model"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("incompatible protocol: %v", err)
	}
	if _, err := h.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{SessionId: request.SessionId, CommandId: "change", Model: "new-chat"}); err != nil {
		t.Fatal(err)
	}
	// Queued input is validated against the replacement, before the model change drains.
	if _, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input-after-switch", Text: "continue"}); err != nil {
		t.Fatal(err)
	}
	command, err = h.Sampler.Store.NextCommand(t.Context(), owner, session.ID)
	if err != nil || command == nil || command.ID != "change" {
		t.Fatalf("model change=%v err=%v", command, err)
	}
	session, err = h.Sampler.Store.GetSession(t.Context(), session.ID, peertls.SessionsURI)
	if err != nil || session.Model != "new-chat" || session.Blocked {
		t.Fatalf("session=%+v err=%v", session, err)
	}
	command, err = h.Sampler.Store.NextCommand(t.Context(), owner, session.ID)
	if err != nil || command == nil || command.ID != "input-after-switch" {
		t.Fatalf("continuation=%v err=%v", command, err)
	}
	conversation, err := h.Sampler.Store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	records, err := conversation.Context().Records()
	if err != nil {
		t.Fatal(err)
	}
	var history strings.Builder
	for _, record := range records {
		history.Write(record)
	}
	if !strings.Contains(history.String(), "remember this") || !strings.Contains(history.String(), "continue") {
		t.Fatalf("conversation records=%s", history.String())
	}
}
