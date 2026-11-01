package ingress

import (
	"context"
	"database/sql"
	"io"
	"net"
	"slices"
	"strings"
	"testing"
	"time"

	runneringresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	testAudience  = "test-runner-ingress"
	testNamespace = "test-sandbox-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
)

var (
	kube *kubeapiserver.Server
	// writerToken proves Sandbox A's ServiceAccount.
	writerToken string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube = server
	writerToken, err = server.WorkloadToken(context.Background(), auth.Account{Namespace: testNamespace, Name: "test-sandbox-a"}, testAudience)
	return err
}

// workloadToken is a token for testAudience bound to a new Pod running as account.
func workloadToken(t *testing.T, account auth.Account) string {
	t.Helper()
	token, err := kube.WorkloadToken(t.Context(), account, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	return token
}

// testTiming is short enough to wait out, long enough that a loaded runner does not end a live call.
var testTiming = Timing{IdleAck: 100 * time.Millisecond, SilenceLimit: time.Second}

type testIngress struct {
	history *storage.Store
	client  runneringresspb.RunnerIngressClient
}

// newIngress serves a fresh ingress over the shared database, as a restarted process would.
func newIngress(t *testing.T) testIngress { return newIngressPacedBy(t, testTiming) }

func newIngressPacedBy(t *testing.T, timing Timing) testIngress {
	t.Helper()
	service, _ := newService(t, timing)
	return serveIngress(t, service)
}

func serveIngress(t *testing.T, service *Service) testIngress {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runneringresspb.RegisterRunnerIngressServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return testIngress{history: service.History, client: runneringresspb.NewRunnerIngressClient(connection)}
}

func newService(t *testing.T, timing Timing) (*Service, *sql.DB) {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	principals, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	return &Service{Principals: principals, History: storage.NewStore(db, true), Timing: timing}, db
}

// call is a runner's Push call: frames go out as the test sends them, and acknowledgements are read back.
type call struct {
	t      *testing.T
	stream grpc.BidiStreamingClient[runneringresspb.PushFrame, runneringresspb.Acknowledgement]
	cancel context.CancelFunc
}

func (i testIngress) open(t *testing.T, token string) *call {
	t.Helper()
	ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token), callDeadline)
	t.Cleanup(cancel)
	stream, err := i.client.Push(ctx)
	if err != nil {
		t.Fatal(err)
	}
	return &call{t: t, stream: stream, cancel: cancel}
}

func (i testIngress) push(t *testing.T, frames ...*runneringresspb.PushFrame) *call {
	t.Helper()
	c := i.open(t, writerToken)
	c.send(frames...)
	return c
}

func (c *call) send(frames ...*runneringresspb.PushFrame) {
	c.t.Helper()
	for _, frame := range frames {
		if err := c.stream.Send(frame); err == io.EOF {
			// The ingress ended the call; the next receive reports why.
			return
		} else if err != nil {
			c.t.Fatal(err)
		}
	}
}

// acknowledged reads acknowledgements until one reaches cursor; none may decrease.
func (c *call) acknowledged(cursor uint64, sealed bool) {
	c.t.Helper()
	var previous uint64
	for {
		acknowledgement, err := c.stream.Recv()
		if err != nil {
			c.t.Fatalf("the call ended before acknowledging %d: %v", cursor, err)
		}
		if acknowledgement.GetCursor() < previous {
			c.t.Fatalf("acknowledgement decreased from %d to %d", previous, acknowledgement.GetCursor())
		}
		previous = acknowledgement.GetCursor()
		if previous >= cursor && acknowledgement.GetSealed() == sealed {
			if previous != cursor {
				c.t.Fatalf("acknowledged %d, want %d", previous, cursor)
			}
			return
		}
	}
}

// refused reads until the ingress ends the call with an error, and returns its code.
func (c *call) refused() codes.Code {
	c.t.Helper()
	for {
		if _, err := c.stream.Recv(); err == io.EOF {
			c.t.Fatal("the call ended without a refusal")
		} else if err != nil {
			return status.Code(err)
		}
	}
}

// acknowledgementsUntilRefused counts the acknowledgements before the ingress ends the call with an error.
func (c *call) acknowledgementsUntilRefused() (int, codes.Code) {
	c.t.Helper()
	var count int
	for {
		_, err := c.stream.Recv()
		if err == io.EOF {
			c.t.Fatal("the call ended without a refusal")
		} else if err != nil {
			return count, status.Code(err)
		}
		count++
	}
}

func (c *call) ended() {
	c.t.Helper()
	if _, err := c.stream.Recv(); err != io.EOF {
		c.t.Fatalf("want the call to end, received %v", err)
	}
}

// drained reads, and ignores, acknowledgements until the call ends cleanly.
func (c *call) drained() {
	c.t.Helper()
	for {
		if _, err := c.stream.Recv(); err == io.EOF {
			return
		} else if err != nil {
			c.t.Fatalf("want the call to end cleanly, received %v", err)
		}
	}
}

func testEntry(cursor uint64, text string) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: "test-runner-journal", Sequence: cursor},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: text}}},
	}
}

func announce(sessionID uuid.UUID, lastCursor uint64, incarnationUID string) *runneringresspb.PushFrame {
	attached := &runnerpb.Attached{SessionId: sessionID.String(), LastCursor: lastCursor}
	if incarnationUID != "" {
		attached.IncarnationUid = &incarnationUID
	}
	return &runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_Announce{Announce: attached}}
}

func entries(cursors ...uint64) []*runneringresspb.PushFrame {
	frames := make([]*runneringresspb.PushFrame, len(cursors))
	for index, cursor := range cursors {
		frames[index] = entryFrame(testEntry(cursor, "journal"))
	}
	return frames
}

func entryFrame(entry *eventlogpb.EventEntry) *runneringresspb.PushFrame {
	return &runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_Entry{Entry: entry}}
}

func sealed(cursor uint64, incarnationUID string) *runneringresspb.PushFrame {
	seal := &runnerpb.Sealed{Cursor: cursor}
	if incarnationUID != "" {
		seal.IncarnationUid = &incarnationUID
	}
	return &runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_Sealed{Sealed: seal}}
}

func heartbeat() *runneringresspb.PushFrame {
	return &runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_Heartbeat{Heartbeat: &runneringresspb.Heartbeat{}}}
}

func concat(groups ...[]*runneringresspb.PushFrame) []*runneringresspb.PushFrame {
	var frames []*runneringresspb.PushFrame
	for _, group := range groups {
		frames = append(frames, group...)
	}
	return frames
}

// committed is the text of the entries the Session retains, in cursor order.
func committed(t *testing.T, history *storage.Store, sessionID uuid.UUID) []string {
	t.Helper()
	page, err := history.ReadPage(t.Context(), sessionID, 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	var texts []string
	for _, entry := range page.GetEntries() {
		texts = append(texts, entry.GetEvent().GetHarnessStderr().GetText())
	}
	return texts
}

func journalTexts(count int) []string {
	texts := make([]string, count)
	for index := range texts {
		texts[index] = "journal"
	}
	return texts
}

func TestEntriesAreAcknowledgedAfterCommitAndTheSealEndsTheJournal(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, announce(sessionID, 2, ""))
	c.acknowledged(0, false)
	c.send(entries(1, 2)...)
	c.acknowledged(2, false)
	if got := committed(t, ingress.history, sessionID); !slices.Equal(got, journalTexts(2)) {
		t.Fatalf("committed %q when the ingress acknowledged 2", got)
	}
	c.send(append(entries(3), sealed(3, ""))...)
	c.acknowledged(3, true)
	c.ended()
	page, err := ingress.history.ReadPage(t.Context(), sessionID, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if attached, seal := page.GetFeedState().GetAttached().GetLastCursor(), page.GetFeedState().GetSealed().GetCursor(); attached != 2 || seal != 3 {
		t.Fatalf("feed state records attached %d and sealed %d, want 2 and 3", attached, seal)
	}
}

func TestResumeReplaysExactDuplicatesAndContinuesAfterARestart(t *testing.T) {
	sessionID := uuid.New()
	first := newIngress(t)
	c := first.push(t, concat([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1, 2))...)
	c.acknowledged(2, false)
	c.cancel()
	// A new ingress process, and a runner that recorded only the first acknowledgement.
	second := newIngress(t)
	c = second.push(t, announce(sessionID, 3, ""))
	c.acknowledged(2, false)
	c.send(append(entries(1, 2, 3), sealed(3, ""))...)
	c.acknowledged(3, true)
	if got := committed(t, second.history, sessionID); !slices.Equal(got, journalTexts(3)) {
		t.Fatalf("committed %q, want three entries", got)
	}
	// A sealed Session takes nothing more: the announcement is answered and the call ends.
	c = newIngress(t).push(t, announce(sessionID, 3, ""))
	c.acknowledged(3, true)
	c.ended()
}

func TestALaterIncarnationContinuesASealedJournal(t *testing.T) {
	for name, sealingIncarnation := range map[string]string{"sealed-without-incarnation": "", "sealed-by-another-incarnation": "test-incarnation-1"} {
		t.Run(name, func(t *testing.T) {
			ingress, sessionID := newIngress(t), uuid.New()
			c := ingress.push(t, concat([]*runneringresspb.PushFrame{announce(sessionID, 2, "")}, entries(1, 2), []*runneringresspb.PushFrame{sealed(2, sealingIncarnation)})...)
			c.acknowledged(2, true)
			c = ingress.push(t, announce(sessionID, 3, "test-incarnation-2"))
			c.acknowledged(2, false)
			c.send(entries(3)...)
			c.acknowledged(3, false)
			c.send(sealed(3, "test-incarnation-2"))
			c.acknowledged(3, true)
			if got := committed(t, ingress.history, sessionID); !slices.Equal(got, journalTexts(3)) {
				t.Fatalf("committed %q, want three entries", got)
			}
			page, err := ingress.history.ReadPage(t.Context(), sessionID, 0, 1)
			if err != nil {
				t.Fatal(err)
			}
			if seal := page.GetFeedState().GetSealed(); seal.GetCursor() != 3 || seal.GetIncarnationUid() != "test-incarnation-2" {
				t.Fatalf("sealed %v, want cursor 3 by incarnation 2", seal)
			}
		})
	}
}

func TestALaterIncarnationAnnouncingAtTheSealCursorReplacesTheSnapshot(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, concat([]*runneringresspb.PushFrame{announce(sessionID, 2, "")}, entries(1, 2), []*runneringresspb.PushFrame{sealed(2, "test-incarnation-1")})...)
	c.acknowledged(2, true)
	// A resumed runner has appended nothing yet, so it announces the cursor the seal ended at.
	c = ingress.push(t, announce(sessionID, 2, "test-incarnation-2"))
	c.acknowledged(2, false)
	c.send(entries(3)...)
	c.acknowledged(3, false)
	page, err := ingress.history.ReadPage(t.Context(), sessionID, 0, 1)
	if err != nil {
		t.Fatal(err)
	}
	if got := page.GetFeedState().GetAttached().GetIncarnationUid(); got != "test-incarnation-2" {
		t.Fatalf("snapshot of incarnation %q, want test-incarnation-2", got)
	}
	if page.GetFeedState().GetSealed() != nil {
		t.Fatalf("the earlier incarnation's seal outlived the continuation: %v", page.GetFeedState().GetSealed())
	}
}

func TestTheIncarnationThatSealedAJournalGetsTheSealBack(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, concat([]*runneringresspb.PushFrame{announce(sessionID, 1, "")}, entries(1), []*runneringresspb.PushFrame{sealed(1, "test-incarnation-1")})...)
	c.acknowledged(1, true)
	c = ingress.push(t, announce(sessionID, 1, "test-incarnation-1"))
	c.acknowledged(1, true)
	c.ended()
}

func TestResumeAnnounceAtStoppedCursorAcceptsTheNextJournalEntry(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	stopped, resumed := announce(sessionID, 1, ""), announce(sessionID, 1, "")
	stopped.GetAnnounce().HarnessState = runnerpb.HarnessState_HARNESS_STATE_STOPPED
	resumed.GetAnnounce().HarnessState = runnerpb.HarnessState_HARNESS_STATE_RUNNING
	c := ingress.push(t, append([]*runneringresspb.PushFrame{stopped}, entries(1)...)...)
	c.acknowledged(1, false)
	c.cancel()
	c = ingress.push(t, resumed)
	c.acknowledged(1, false)
	// The idle repeat follows the commit that records a snapshot history already covers.
	c.acknowledged(1, false)
	page, err := ingress.history.ReadPage(t.Context(), sessionID, 1, 10)
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(page.GetFeedState().GetAttached(), resumed.GetAnnounce()) {
		t.Fatalf("snapshot %v, want the resumed announcement", page.GetFeedState().GetAttached())
	}
	c.send(entries(2)...)
	c.acknowledged(2, false)
	if page, err = ingress.history.ReadPage(t.Context(), sessionID, 1, 10); err != nil {
		t.Fatal(err)
	}
	if len(page.GetEntries()) != 1 || !proto.Equal(page.GetEntries()[0], testEntry(2, "journal")) {
		t.Fatalf("entries after cursor 1: %v", page.GetEntries())
	}
}

func TestOutOfOrderAndConflictingPushesAreRefused(t *testing.T) {
	for name, refusal := range map[string]struct {
		frames []*runneringresspb.PushFrame
		code   codes.Code
	}{
		"first-entry-leaves-a-gap": {entries(3), codes.InvalidArgument},
		"first-entry-is-cursor-0":  {entries(0), codes.InvalidArgument},
		"entries-skip-a-cursor":    {entries(1, 3), codes.InvalidArgument},
		"seal-past-the-last-entry": {append(entries(1), sealed(2, "")), codes.InvalidArgument},
		"replay-differs":           {[]*runneringresspb.PushFrame{entryFrame(testEntry(1, "rewritten"))}, codes.FailedPrecondition},
		"second-announcement":      {[]*runneringresspb.PushFrame{announce(uuid.New(), 0, "")}, codes.InvalidArgument},
		"empty-frame":              {[]*runneringresspb.PushFrame{{}}, codes.InvalidArgument},
	} {
		t.Run(name, func(t *testing.T) {
			ingress, sessionID := newIngress(t), uuid.New()
			first := ingress.push(t, append([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1)...)...)
			first.acknowledged(1, false)
			first.cancel()
			c := ingress.push(t, announce(sessionID, 0, ""))
			c.acknowledged(1, false)
			c.send(refusal.frames...)
			if got := c.refused(); got != refusal.code {
				t.Fatalf("refused with %v, want %v", got, refusal.code)
			}
			if got := committed(t, ingress.history, sessionID); !slices.Equal(got, journalTexts(1)) {
				t.Fatalf("committed %q, want the one entry from before", got)
			}
		})
	}
}

func TestASessionTakesPushesOnlyFromItsFirstWriter(t *testing.T) {
	writer := auth.Account{Namespace: testNamespace, Name: "test-first-writer"}
	for name, otherWriter := range map[string]func(*testing.T) string{
		"another account": func(t *testing.T) string {
			return workloadToken(t, auth.Account{Namespace: testNamespace, Name: "test-sandbox-b"})
		},
		"the writer's account deleted and recreated under its name": func(t *testing.T) string {
			if err := kube.RecreateServiceAccount(t.Context(), writer); err != nil {
				t.Fatal(err)
			}
			return workloadToken(t, writer)
		},
	} {
		t.Run(name, func(t *testing.T) {
			ingress, sessionID := newIngress(t), uuid.New()
			first := ingress.open(t, workloadToken(t, writer))
			first.send(append([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1)...)...)
			first.acknowledged(1, false)
			other := ingress.open(t, otherWriter(t))
			other.send(append([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1, 2)...)...)
			if got := other.refused(); got != codes.PermissionDenied {
				t.Fatalf("refused with %v, want PermissionDenied", got)
			}
			if got := committed(t, ingress.history, sessionID); !slices.Equal(got, journalTexts(1)) {
				t.Fatalf("committed %q, want only the first writer's entry", got)
			}
		})
	}
}

func TestAnUnknownBearerIsUnauthenticated(t *testing.T) {
	c := newIngress(t).open(t, "test-token-unknown")
	c.send(announce(uuid.New(), 0, ""))
	if got := c.refused(); got != codes.Unauthenticated {
		t.Fatalf("refused with %v, want Unauthenticated", got)
	}
}

func TestACallThatDoesNotOpenWithAnAnnouncementIsRefused(t *testing.T) {
	upperCase := announce(uuid.New(), 0, "")
	upperCase.GetAnnounce().SessionId = "0A1B2C3D-4E5F-4A1B-8C2D-3E4F5A6B7C8D"
	notAUUID := announce(uuid.New(), 0, "")
	notAUUID.GetAnnounce().SessionId = "test-not-a-uuid"
	for name, frame := range map[string]*runneringresspb.PushFrame{
		"entry-first":          entries(1)[0],
		"heartbeat-first":      heartbeat(),
		"not-a-uuid":           notAUUID,
		"not-the-canonical-id": upperCase,
	} {
		t.Run(name, func(t *testing.T) {
			c := newIngress(t).push(t, frame)
			if got := c.refused(); got != codes.InvalidArgument {
				t.Fatalf("refused with %v, want InvalidArgument", got)
			}
		})
	}
	t.Run("call-ended-unannounced", func(t *testing.T) {
		c := newIngress(t).open(t, writerToken)
		if err := c.stream.CloseSend(); err != nil {
			t.Fatal(err)
		}
		if got := c.refused(); got != codes.InvalidArgument {
			t.Fatalf("refused with %v, want InvalidArgument", got)
		}
	})
	t.Run("silence", func(t *testing.T) {
		if got := newIngress(t).open(t, writerToken).refused(); got != codes.Unavailable {
			t.Fatalf("refused with %v, want Unavailable", got)
		}
	})
}

func TestAnIdleCallRepeatsItsAcknowledgementAndASilentOneEnds(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, append([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1)...)...)
	c.acknowledged(1, false)
	// Heartbeats keep the call open past the silence limit, which is ten idle intervals; each idle interval
	// repeats the acknowledgement.
	for range 12 {
		c.acknowledged(1, false)
		c.send(heartbeat())
	}
	if got := c.refused(); got != codes.Unavailable {
		t.Fatalf("refused with %v, want Unavailable", got)
	}
}

func TestACallSilentForTheLimitEndsAfterRepeatingItsAcknowledgementForEachIdleInterval(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, append([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1)...)...)
	c.acknowledged(1, false)
	idleIntervals := int(testTiming.SilenceLimit / testTiming.IdleAck)
	if repeats, code := c.acknowledgementsUntilRefused(); code != codes.Unavailable || repeats != idleIntervals-1 {
		t.Fatalf("%d repeated acknowledgements and then %v, want %d and Unavailable", repeats, code, idleIntervals-1)
	}
}

func TestEntriesAreAcknowledgedWithoutWaitingForAnIdleInterval(t *testing.T) {
	ingress, sessionID := newIngressPacedBy(t, Timing{IdleAck: time.Hour, SilenceLimit: 3 * time.Hour}), uuid.New()
	c := ingress.push(t, append([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1)...)...)
	c.acknowledged(1, false)
	c.send(heartbeat())
	c.send(entries(2)...)
	c.acknowledged(2, false)
}

func TestTheRunnerClosingItsSideKeepsWhatArrived(t *testing.T) {
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, concat([]*runneringresspb.PushFrame{announce(sessionID, 0, "")}, entries(1, 2))...)
	if err := c.stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	// What arrives together with the end of the call is committed, but no longer acknowledged.
	c.drained()
	if got := committed(t, ingress.history, sessionID); !slices.Equal(got, journalTexts(2)) {
		t.Fatalf("committed %q, want both entries", got)
	}
}

func TestALongBurstIsCommittedInOrderAndAcknowledgedThroughItsEnd(t *testing.T) {
	const count = 300
	// 300 entries of 20 KB overrun both the entry and the byte limit of one commit.
	text := strings.Repeat("x", 20*1024)
	ingress, sessionID := newIngress(t), uuid.New()
	c := ingress.push(t, announce(sessionID, 0, ""))
	for cursor := uint64(1); cursor <= count; cursor++ {
		c.send(entryFrame(testEntry(cursor, text)))
	}
	c.acknowledged(count, false)
	page, err := ingress.history.ReadPage(t.Context(), sessionID, 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.GetEntries()) != count {
		t.Fatalf("committed %d entries, want %d", len(page.GetEntries()), count)
	}
	for index, entry := range page.GetEntries() {
		if entry.GetCursor() != uint64(index)+1 {
			t.Fatalf("entry %d has cursor %d", index, entry.GetCursor())
		}
	}
}

// A central producer retains its own writer identity while its execution target is
// the Sandbox displayed in inventory. Reconnects cannot silently replace that target.
func TestCentralSamplerWriterAndExecutorAreSeparate(t *testing.T) {
	service, db := newService(t, testTiming)
	account := auth.Account{Namespace: testNamespace, Name: "test-central-sampler"}
	service.SamplerAccounts = []auth.Account{account}
	ingress := serveIngress(t, service)
	token := workloadToken(t, account)
	sessionID := uuid.NewString()
	executorUID := uuid.NewString()
	announcement := &runneringresspb.SamplerAnnouncement{
		Session: &runnerpb.Attached{SessionId: sessionID, Spec: &runnerpb.SessionSpec{
			Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Model: "test-model", Cwd: "/test-workspace",
		}},
		Executor: &sandboxespb.SandboxDestination{
			Sandbox: "test-execution-sandbox", SandboxUid: executorUID,
			Owner: &sandboxespb.ServiceAccount{Namespace: testNamespace, Name: "test-execution-sandbox"},
		},
	}
	c := ingress.open(t, token)
	c.send(&runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_SamplerAnnounce{SamplerAnnounce: announcement}})
	c.acknowledged(0, false)
	var writerName, sandboxName, sandboxUID string
	if err := db.QueryRowContext(t.Context(), "SELECT writer_name,sandbox_name,sandbox_uid::text FROM session_history WHERE id=$1", sessionID).
		Scan(&writerName, &sandboxName, &sandboxUID); err != nil {
		t.Fatal(err)
	}
	if writerName != account.Name || sandboxName != announcement.Executor.Sandbox || sandboxUID != executorUID {
		t.Fatalf("binding writer=%s executor=%s uid=%s", writerName, sandboxName, sandboxUID)
	}
	c.cancel()
	changed := proto.Clone(announcement).(*runneringresspb.SamplerAnnouncement)
	changed.Executor.SandboxUid = uuid.NewString()
	next := ingress.open(t, token)
	next.send(&runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_SamplerAnnounce{SamplerAnnounce: changed}})
	if _, err := next.stream.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("changed executor: %v", err)
	}
	unauthorized := ingress.open(t, writerToken)
	unauthorized.send(&runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_SamplerAnnounce{SamplerAnnounce: announcement}})
	if _, err := unauthorized.stream.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unauthorized producer: %v", err)
	}
	disguised := ingress.open(t, writerToken)
	disguised.send(&runneringresspb.PushFrame{Frame: &runneringresspb.PushFrame_Announce{Announce: announcement.Session}})
	if _, err := disguised.stream.Recv(); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("disguised central producer: %v", err)
	}
}
