package historyexport

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"k8s.io/client-go/util/workqueue"
)

type acknowledgementOutbox struct{ cursor uint64 }

func (o acknowledgementOutbox) ExportSnapshot(context.Context, string) (Snapshot, error) {
	return Snapshot{Description: &samplerpb.GetSessionResponse{Attached: &sessionpb.Attached{}}, Cursor: o.cursor - 1}, nil
}

func (o acknowledgementOutbox) CurrentCursor(context.Context, string) (uint64, error) {
	return o.cursor, nil
}

func (acknowledgementOutbox) Events(context.Context, string, uint64, int) ([]*eventlogpb.EventEntry, error) {
	return nil, errors.New("unexpected event read")
}

func (acknowledgementOutbox) Checkpoint(context.Context, string, string) (*uint64, error) {
	return nil, errors.New("unexpected checkpoint read")
}

func (acknowledgementOutbox) CommitCheckpoint(context.Context, string, string, uint64) error {
	return errors.New("unexpected checkpoint write")
}

func (acknowledgementOutbox) PendingSessionIDs(context.Context, string, string, int) ([]string, error) {
	return nil, errors.New("unexpected pending-session read")
}

func TestAcknowledgementValidatesTheSessionJournal(t *testing.T) {
	const journalCursor = 4
	outbox := acknowledgementOutbox{cursor: journalCursor + 1}
	for _, test := range []struct {
		name    string
		ack     *ingresspb.Acknowledgement
		invalid bool
	}{
		{"unchanged", &ingresspb.Acknowledgement{Cursor: journalCursor}, false},
		{"newer source commit", &ingresspb.Acknowledgement{Cursor: journalCursor + 1}, false},
		{"backwards", &ingresspb.Acknowledgement{Cursor: journalCursor - 1}, true},
		{"beyond journal", &ingresspb.Acknowledgement{Cursor: journalCursor + 2}, true},
		{"sealed", &ingresspb.Acknowledgement{Cursor: journalCursor, Sealed: true}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := checkExportAcknowledgement(t.Context(), outbox, "session", test.ack, journalCursor, journalCursor)
			if (err != nil) != test.invalid {
				t.Fatalf("ack=%v err=%v invalid=%v", test.ack, err, test.invalid)
			}
		})
	}
}

// recordingDelayingQueue stands in for the queue's clock: it records each
// requested retry delay and makes the item ready at once.
type recordingDelayingQueue struct {
	workqueue.TypedDelayingInterface[string]
	delays []time.Duration
}

func (q *recordingDelayingQueue) AddAfter(item string, delay time.Duration) {
	q.delays = append(q.delays, delay)
	q.Add(item)
}

type exportOutcome struct {
	caughtUp bool
	err      error
}

func TestExportQueueBacksOffFailuresAndResetsAfterProgress(t *testing.T) {
	delaying := &recordingDelayingQueue{TypedDelayingInterface: workqueue.NewTypedDelayingQueue[string]()}
	unavailable := errors.New("History unavailable")
	outcomes := []exportOutcome{{err: unavailable}, {err: unavailable}, {caughtUp: false}, {err: unavailable}, {caughtUp: true}}
	queue := newExportQueue(workqueue.TypedRateLimitingQueueConfig[string]{DelayingQueue: delaying}, func(context.Context, string) (bool, error) {
		outcome := outcomes[0]
		outcomes = outcomes[1:]
		return outcome.caughtUp, outcome.err
	})
	defer queue.queue.ShutDown()
	queue.queue.Add("backoff-session")
	for range 5 {
		if !queue.processNext(t.Context()) {
			t.Fatal("export queue shut down")
		}
	}
	// Failures double the delay; an exhausted quantum is progress, so the next failure starts over.
	if expected := []time.Duration{exportRetryBase, 2 * exportRetryBase, exportRetryBase}; !slices.Equal(delaying.delays, expected) {
		t.Fatalf("retry delays=%v expected=%v", delaying.delays, expected)
	}
	if queue.queue.Len() != 0 || queue.queue.NumRequeues("backoff-session") != 0 {
		t.Fatalf("caught-up Session left queued=%d requeues=%d", queue.queue.Len(), queue.queue.NumRequeues("backoff-session"))
	}
}
