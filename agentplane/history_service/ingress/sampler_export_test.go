package ingress

import (
	"context"
	"database/sql"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	runneringresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	centralsampler "github.com/agentydragon/ducktape/agentplane/x/central_sampler"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	samplerOwnerUID = "test-central-sampler-owner-uid"
)

type samplerExportPlan struct {
	holdAckCursor       uint64
	holdInitialAck      bool
	holdIncomingCursor  uint64
	failAfterCleanClose bool

	ackHeld          chan struct{}
	releaseAck       chan struct{}
	incomingHeld     chan struct{}
	releaseInput     chan struct{}
	ackSignalOnce    sync.Once
	ackReleaseOnce   sync.Once
	inputSignalOnce  sync.Once
	inputReleaseOnce sync.Once

	mu          sync.Mutex
	entryFrames int
}

func newSamplerExportPlan(holdAckCursor, holdIncomingCursor uint64, failAfterCleanClose bool) *samplerExportPlan {
	return &samplerExportPlan{
		holdAckCursor:       holdAckCursor,
		holdIncomingCursor:  holdIncomingCursor,
		failAfterCleanClose: failAfterCleanClose,
		ackHeld:             make(chan struct{}),
		releaseAck:          make(chan struct{}),
		incomingHeld:        make(chan struct{}),
		releaseInput:        make(chan struct{}),
	}
}

func (p *samplerExportPlan) releaseAcknowledgement() {
	p.ackReleaseOnce.Do(func() { close(p.releaseAck) })
}

func (p *samplerExportPlan) releaseIncoming() {
	p.inputReleaseOnce.Do(func() { close(p.releaseInput) })
}

func (p *samplerExportPlan) entriesReceived() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.entryFrames
}

type samplerExportIngressServer struct {
	runneringresspb.UnimplementedRunnerIngressServer
	service *Service
	plans   []*samplerExportPlan
	pushes  atomic.Int32
}

func (s *samplerExportIngressServer) Push(stream grpc.BidiStreamingServer[runneringresspb.PushFrame, runneringresspb.Acknowledgement]) error {
	index := int(s.pushes.Add(1)) - 1
	if index >= len(s.plans) {
		return status.Error(codes.Internal, "unexpected sampler export call")
	}
	plan := s.plans[index]
	err := s.service.Push(&samplerExportStream{BidiStreamingServer: stream, plan: plan})
	if err == nil && plan.failAfterCleanClose {
		return status.Error(codes.Unavailable, "test interrupted completion after History accepted the snapshot")
	}
	return err
}

type samplerExportStream struct {
	grpc.BidiStreamingServer[runneringresspb.PushFrame, runneringresspb.Acknowledgement]
	plan *samplerExportPlan
}

func (s *samplerExportStream) Send(ack *runneringresspb.Acknowledgement) error {
	if (s.plan.holdInitialAck && ack.GetCursor() == 0) || (s.plan.holdAckCursor > 0 && ack.GetCursor() == s.plan.holdAckCursor) {
		s.plan.ackSignalOnce.Do(func() { close(s.plan.ackHeld) })
		select {
		case <-s.plan.releaseAck:
		case <-s.Context().Done():
			return s.Context().Err()
		}
	}
	return s.BidiStreamingServer.Send(ack)
}

func (s *samplerExportStream) Recv() (*runneringresspb.PushFrame, error) {
	frame, err := s.BidiStreamingServer.Recv()
	if err != nil || frame == nil {
		return frame, err
	}
	if frame.GetEntry() != nil {
		s.plan.mu.Lock()
		s.plan.entryFrames++
		s.plan.mu.Unlock()
		if s.plan.holdIncomingCursor > 0 && frame.GetEntry().GetCursor() == s.plan.holdIncomingCursor {
			s.plan.inputSignalOnce.Do(func() { close(s.plan.incomingHeld) })
			select {
			case <-s.plan.releaseInput:
			case <-s.Context().Done():
				return nil, s.Context().Err()
			}
		}
	}
	return frame, err
}

type samplerExportFixture struct {
	history *storage.Store
	sampler *centralsampler.Store
	target  string
	token   string
}

func newSamplerExportFixture(t *testing.T, plans ...*samplerExportPlan) samplerExportFixture {
	t.Helper()
	ctx := t.Context()

	historyURL := integrationDatabase.Clone(t)
	historyDB, err := sql.Open("postgres", historyURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = historyDB.Close() })
	history := storage.NewStore(historyDB, true)

	// The package fixture is migrated for History. Give Sampler its own clone and remove that clone's
	// public schema so its clean-database migration does not mistake History relations for old Sampler data.
	samplerURL := integrationDatabase.Clone(t)
	samplerDB, err := sql.Open("postgres", samplerURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = samplerDB.Close() })
	if _, err := samplerDB.ExecContext(ctx, "DROP SCHEMA public CASCADE"); err != nil {
		t.Fatal(err)
	}
	if _, err := samplerDB.ExecContext(ctx, "CREATE SCHEMA public"); err != nil {
		t.Fatal(err)
	}
	if err := centralsampler.Migrate(ctx, samplerDB); err != nil {
		t.Fatal(err)
	}
	samplerStore := centralsampler.New(samplerDB)

	account := auth.Account{Namespace: testNamespace, Name: "test-central-sampler"}
	principals, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Principals:      principals,
		History:         history,
		Timing:          Timing{IdleAck: 10 * time.Second, SilenceLimit: 30 * time.Second},
		SamplerAccounts: []auth.Account{account},
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runneringresspb.RegisterRunnerIngressServer(server, &samplerExportIngressServer{service: service, plans: plans})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })

	token := workloadToken(t, account)
	tokenFile := filepath.Join(t.TempDir(), "central-sampler-workload-token")
	if err := os.WriteFile(tokenFile, []byte(token), 0600); err != nil {
		t.Fatal(err)
	}
	return samplerExportFixture{history: history, sampler: samplerStore, target: listener.Addr().String(), token: tokenFile}
}

func (f samplerExportFixture) openSession(t *testing.T) *centralsampler.Session {
	t.Helper()
	session, err := f.sampler.OpenSession(t.Context(), &samplerpb.OpenSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &runnerpb.SessionSpec{
			Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER,
			Model:   "test-model-initial",
			Cwd:     "/test-workspace",
		},
		Executor: &sandboxespb.SandboxDestination{
			Sandbox:    "test-execution-sandbox",
			SandboxUid: uuid.NewString(),
			Owner:      &sandboxespb.ServiceAccount{Namespace: testNamespace, Name: "test-execution-sandbox"},
		},
		ExpectedIncarnationUid: uuid.NewString(),
	}, samplerOwnerUID, protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func updateSamplerModel(t *testing.T, store *centralsampler.Store, session *centralsampler.Session, count int, final string) {
	t.Helper()
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := store.Release(t.Context(), owner); err != nil {
			t.Errorf("release sampler owner: %v", err)
		}
	}()
	for index := 0; index < count; index++ {
		model := fmt.Sprintf("test-model-update-%d", index)
		if index == count-1 {
			model = final
		}
		commandID := uuid.NewString()
		if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
			CommandId: commandID,
			Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: model}},
		}, samplerOwnerUID, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
			t.Fatal(err)
		}
	}
}

func startSamplerExport(ctx context.Context, f samplerExportFixture, sessionID string) <-chan error {
	done := make(chan error, 1)
	go func() { done <- centralsampler.ExportSession(ctx, f.sampler, sessionID, f.target, f.token) }()
	return done
}

func waitSamplerExportSignal(t *testing.T, signal <-chan struct{}, name string) {
	t.Helper()
	timer := time.NewTimer(callDeadline)
	defer timer.Stop()
	select {
	case <-signal:
	case <-timer.C:
		t.Fatalf("timed out waiting for %s", name)
	}
}

func samplerExportResult(t *testing.T, done <-chan error) error {
	t.Helper()
	timer := time.NewTimer(callDeadline)
	defer timer.Stop()
	select {
	case err := <-done:
		return err
	case <-timer.C:
		t.Fatal("sampler export did not finish before its deadline")
		return nil
	}
}

func samplerExportContext(t *testing.T) (context.Context, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return ctx, cancel
}

func assertSamplerHistoryMatchesSource(t *testing.T, f samplerExportFixture, sessionID string, model string) uint64 {
	t.Helper()
	id, err := uuid.Parse(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	sourceSession, err := f.sampler.GetSession(t.Context(), sessionID, "")
	if err != nil {
		t.Fatal(err)
	}
	source, err := f.sampler.Events(t.Context(), sessionID, 0, 4096)
	if err != nil {
		t.Fatal(err)
	}
	page, err := f.history.ReadPage(t.Context(), id, 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.GetEntries()) != len(source) {
		t.Fatalf("History has %d events, Sampler has %d", len(page.GetEntries()), len(source))
	}
	for index, entry := range source {
		if entry.GetCursor() != uint64(index+1) || !proto.Equal(entry, page.GetEntries()[index]) {
			t.Fatalf("History event %d = %v, want exact Sampler event %v", index+1, page.GetEntries()[index], entry)
		}
	}
	attached := page.GetFeedState().GetAttached()
	if attached == nil || attached.GetLastCursor() != sourceSession.Cursor || attached.GetSpec().GetModel() != model {
		t.Fatalf("History attachment = %v, want cursor %d and model %q", attached, sourceSession.Cursor, model)
	}
	return sourceSession.Cursor
}

func assertSamplerExportCheckpoint(t *testing.T, f samplerExportFixture, sessionID string, want *uint64) {
	t.Helper()
	got, err := f.sampler.Checkpoint(t.Context(), sessionID, f.target)
	if err != nil {
		t.Fatal(err)
	}
	if (got == nil) != (want == nil) || (got != nil && *got != *want) {
		t.Fatalf("export checkpoint = %v, want %v", got, want)
	}
}

func assertSamplerPending(t *testing.T, f samplerExportFixture, sessionID string, want bool) {
	t.Helper()
	ids, err := f.sampler.PendingSessionIDs(t.Context(), f.target, "", 10)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, id := range ids {
		found = found || id == sessionID
	}
	if found != want {
		t.Fatalf("session pending=%t, want %t; pending IDs %q", found, want, ids)
	}
}

func TestConcurrentSamplerExportsKeepNewestSnapshotAndAcceptCumulativeAcknowledgement(t *testing.T) {
	first := newSamplerExportPlan(0, 0, false)
	first.holdInitialAck = true
	fixture := newSamplerExportFixture(t, first, newSamplerExportPlan(0, 0, false), newSamplerExportPlan(0, 0, false))
	defer first.releaseAcknowledgement()
	session := fixture.openSession(t)

	firstContext, _ := samplerExportContext(t)
	firstDone := startSamplerExport(firstContext, fixture, session.ID)
	waitSamplerExportSignal(t, first.ackHeld, "the old export's initial History acknowledgement")
	updateSamplerModel(t, fixture.sampler, session, 10, "test-model-latest")
	secondContext, _ := samplerExportContext(t)
	if err := samplerExportResult(t, startSamplerExport(secondContext, fixture, session.ID)); err != nil {
		t.Fatalf("new export: %v", err)
	}
	first.releaseAcknowledgement()
	if err := samplerExportResult(t, firstDone); err != nil {
		t.Fatalf("old export after the new export committed: %v", err)
	}

	cursor := assertSamplerHistoryMatchesSource(t, fixture, session.ID, "test-model-latest")
	assertSamplerExportCheckpoint(t, fixture, session.ID, &cursor)
	assertSamplerPending(t, fixture, session.ID, false)
}

func TestSamplerExporterContinuesConsecutivelyAfterAnAheadOfPageAcknowledgement(t *testing.T) {
	bootstrap := newSamplerExportPlan(0, 0, false)
	first := newSamplerExportPlan(0, 17, false)
	peer := newSamplerExportPlan(36, 37, false)
	fixture := newSamplerExportFixture(t, bootstrap, first, peer)
	session := fixture.openSession(t)

	updateSamplerModel(t, fixture.sampler, session, 7, "test-model-bootstrap")
	bootstrapContext, _ := samplerExportContext(t)
	if err := samplerExportResult(t, startSamplerExport(bootstrapContext, fixture, session.ID)); err != nil {
		t.Fatalf("bootstrap export: %v", err)
	}
	checkpoint := uint64(16)
	assertSamplerExportCheckpoint(t, fixture, session.ID, &checkpoint)
	updateSamplerModel(t, fixture.sampler, session, 16, "test-model-latest")

	firstContext, _ := samplerExportContext(t)
	firstDone := startSamplerExport(firstContext, fixture, session.ID)
	waitSamplerExportSignal(t, first.incomingHeld, "the old export's first entry after the bootstrap cursor")
	peerContext, cancelPeer := samplerExportContext(t)
	peerDone := startSamplerExport(peerContext, fixture, session.ID)
	waitSamplerExportSignal(t, peer.ackHeld, "the peer's committed cursor 36")
	if got := committedCursor(t, fixture.history, session.ID); got != 36 {
		t.Fatalf("peer committed through %d while its acknowledgement was gated, want 36", got)
	}
	cancelPeer()
	if err := samplerExportResult(t, peerDone); status.Code(err) != codes.Canceled {
		t.Fatalf("interrupted peer export error = %v, want Canceled", err)
	}
	assertSamplerExportCheckpoint(t, fixture, session.ID, &checkpoint)

	first.releaseIncoming()
	if err := samplerExportResult(t, firstDone); err != nil {
		t.Fatalf("export continuing after cumulative cursor 36: %v", err)
	}
	cursor := assertSamplerHistoryMatchesSource(t, fixture, session.ID, "test-model-latest")
	if cursor != 48 || first.entriesReceived() != 32 {
		t.Fatalf("source cursor=%d, old stream sent %d entries; want 48 and two consecutive 16-entry pages", cursor, first.entriesReceived())
	}
	assertSamplerExportCheckpoint(t, fixture, session.ID, &cursor)
	assertSamplerPending(t, fixture, session.ID, false)
}

func TestSamplerExportRequiresCleanFinalStatusAndRetriesAfterAllEventsWereCommitted(t *testing.T) {
	failed := newSamplerExportPlan(0, 0, true)
	retry := newSamplerExportPlan(0, 0, false)
	fixture := newSamplerExportFixture(t, failed, retry)
	session := fixture.openSession(t)
	updateSamplerModel(t, fixture.sampler, session, 2, "test-model-final")

	failedContext, _ := samplerExportContext(t)
	err := samplerExportResult(t, startSamplerExport(failedContext, fixture, session.ID))
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("export after the server's accepted snapshot returned %v, want Unavailable", err)
	}
	cursor := assertSamplerHistoryMatchesSource(t, fixture, session.ID, "test-model-final")
	if failed.entriesReceived() == 0 {
		t.Fatal("the failed export sent no event data")
	}
	assertSamplerExportCheckpoint(t, fixture, session.ID, nil)
	assertSamplerPending(t, fixture, session.ID, true)

	retryContext, _ := samplerExportContext(t)
	if err := samplerExportResult(t, startSamplerExport(retryContext, fixture, session.ID)); err != nil {
		t.Fatalf("retry after all event data was committed: %v", err)
	}
	if retry.entriesReceived() != 0 {
		t.Fatalf("retry resent %d committed events, want metadata-only reconciliation", retry.entriesReceived())
	}
	assertSamplerExportCheckpoint(t, fixture, session.ID, &cursor)
	assertSamplerPending(t, fixture, session.ID, false)
}

func TestCancelingOneSamplerExporterDoesNotStopItsPeer(t *testing.T) {
	canceled := newSamplerExportPlan(0, 0, false)
	canceled.holdInitialAck = true
	peer := newSamplerExportPlan(16, 0, false)
	fixture := newSamplerExportFixture(t, canceled, peer)
	defer canceled.releaseAcknowledgement()
	session := fixture.openSession(t)
	updateSamplerModel(t, fixture.sampler, session, 9, "test-model-final")

	canceledContext, cancelCanceled := samplerExportContext(t)
	canceledDone := startSamplerExport(canceledContext, fixture, session.ID)
	waitSamplerExportSignal(t, canceled.ackHeld, "the canceled export's initial History acknowledgement")
	peerContext, _ := samplerExportContext(t)
	peerDone := startSamplerExport(peerContext, fixture, session.ID)
	waitSamplerExportSignal(t, peer.ackHeld, "the peer's committed first page")
	cancelCanceled()
	if err := samplerExportResult(t, canceledDone); status.Code(err) != codes.Canceled {
		t.Fatalf("canceled export error = %v, want Canceled", err)
	}
	peer.releaseAcknowledgement()
	if err := samplerExportResult(t, peerDone); err != nil {
		t.Fatalf("peer export after the other exporter was canceled: %v", err)
	}
	cursor := assertSamplerHistoryMatchesSource(t, fixture, session.ID, "test-model-final")
	if cursor != 20 {
		t.Fatalf("source cursor %d, want 20", cursor)
	}
	assertSamplerExportCheckpoint(t, fixture, session.ID, &cursor)
	assertSamplerPending(t, fixture, session.ID, false)
}

func committedCursor(t *testing.T, history *storage.Store, sessionID string) uint64 {
	t.Helper()
	id, err := uuid.Parse(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	page, err := history.ReadPage(t.Context(), id, 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	return page.GetLastCursor()
}
