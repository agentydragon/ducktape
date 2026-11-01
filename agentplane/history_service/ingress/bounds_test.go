package ingress

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"

	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
)

// acknowledgementSink is the server side of a call whose frames the test has already queued.
type acknowledgementSink struct {
	grpc.ServerStream
}

func (acknowledgementSink) Send(*session_ingresspb.Acknowledgement) error { return nil }

func (acknowledgementSink) Recv() (*session_ingresspb.PushFrame, error) { return nil, io.EOF }

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
			c := &pushCall{history: service.History, timing: testTiming, stream: acknowledgementSink{}, frames: frames, log: slog.Default(), service: service}
			writer := storage.Writer{Namespace: testNamespace, Name: "test-sandbox-a", UID: "test-account-a-uid"}
			if err := c.failure(c.run(context.Background(), writer, auth.Account{Namespace: writer.Namespace, Name: writer.Name})); err != nil {
				t.Fatal(err)
			}
			var stored, transactions int
			err := db.QueryRowContext(t.Context(), "SELECT count(*), count(DISTINCT xmin::text) FROM session_event WHERE session_id = $1", sessionID).
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
