package ingress

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// acknowledgementSink is the server side of a call whose frames the test has already queued.
type acknowledgementSink struct {
	grpc.ServerStream
}

func (acknowledgementSink) Send(*session_ingresspb.Acknowledgement) error { return nil }

func (acknowledgementSink) Recv() (*session_ingresspb.PushFrame, error) { return nil, io.EOF }

type authenticatedAcknowledgementSink struct {
	acknowledgementSink
	ctx context.Context
}

func (s authenticatedAcknowledgementSink) Context() context.Context { return s.ctx }

// A runner that never pauses keeps the ingress's frame queue non-empty, so only the batch limits bound a
// transaction. Each commit is one transaction, so the distinct transaction IDs on the stored entries count them.
func TestABurstIsCommittedInBoundedBatches(t *testing.T) {
	for name, burst := range map[string]struct {
		count        int
		text         string
		transactions int
	}{
		// 128 entries to a commit; the last 104 commit when the runner closes its side.
		"many-small-entries": {count: 1000, text: "x", transactions: 8},
		// A commit before an entry that would take the batch past 1 MiB: ten of these entries fit.
		"large-entries": {count: 40, text: strings.Repeat("x", 100*1024), transactions: 4},
	} {
		t.Run(name, func(t *testing.T) {
			service, db := newService(t, testTiming)
			sessionID := uuid.New()
			frames := &frameReader{queue: make(chan received, burst.count+2), done: make(chan struct{})}
			frames.queue <- received{frame: announce(sessionID, 0, "")}
			for cursor := uint64(1); cursor <= uint64(burst.count); cursor++ {
				frames.queue <- received{frame: entryFrame(testEntry(cursor, burst.text))}
			}
			frames.queue <- received{err: io.EOF}
			ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+writerToken))
			stream := authenticatedAcknowledgementSink{ctx: ctx}
			c := &pushCall{history: service.History, timing: testTiming, stream: stream, frames: frames, log: slog.Default(), service: service}
			principals, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
			if err != nil {
				t.Fatal(err)
			}
			methods := auth.Methods{session_ingresspb.SessionJournalIngress_Push_FullMethodName: auth.Accounts(principals, []auth.Account{{Namespace: testNamespace, Name: "test-sandbox-a"}})}
			interceptor := methods.StreamInterceptor(time.Second)
			err = interceptor(nil, stream, &grpc.StreamServerInfo{FullMethod: session_ingresspb.SessionJournalIngress_Push_FullMethodName}, func(_ any, admitted grpc.ServerStream) error {
				principal, err := auth.Caller(admitted.Context())
				if err != nil {
					return err
				}
				writer := storage.ServiceAccountWriter{Namespace: principal.Account.Namespace, Name: principal.Account.Name, UID: principal.ServiceAccountUID}
				return c.failure(c.run(admitted.Context(), accountPusher{writer}))
			})
			if err != nil {
				t.Fatal(err)
			}
			var stored, transactions int
			err = db.QueryRowContext(t.Context(), "SELECT count(*), count(DISTINCT xmin::text) FROM session_event WHERE session_id = $1", sessionID).
				Scan(&stored, &transactions)
			if err != nil {
				t.Fatal(err)
			}
			if stored != burst.count || transactions != burst.transactions {
				t.Errorf("stored %d entries in %d transactions, want %d in %d", stored, transactions, burst.count, burst.transactions)
			}
		})
	}
}
