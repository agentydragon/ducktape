package centralsampler

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"k8s.io/client-go/util/workqueue"
)

const (
	exportPageSize     = 16
	exportPagesPerTurn = 16
	exportConcurrency  = 8
	exportListPage     = 256
	exportRetryBase    = time.Second
	exportRetryMax     = 5 * time.Minute
)

func ExportSession(ctx context.Context, store *Store, sessionID, target, tokenFile string) error {
	channel, err := bearer.Dial(target, tokenFile, 30*time.Second)
	if err != nil {
		return err
	}
	defer channel.Close()
	_, err = exportSession(ctx, store, sessionID, target, channel)
	return err
}

// exportSession sends at most one scheduling quantum and reports whether History has caught up.
func exportSession(ctx context.Context, store *Store, sessionID, target string, channel grpc.ClientConnInterface) (bool, error) {
	pages := 0
	for refresh := 0; refresh < exportPagesPerTurn; refresh++ {
		session, err := store.GetSession(ctx, sessionID, "")
		if err != nil {
			return false, err
		}
		snapshot, err := Describe(session)
		if err != nil {
			return false, err
		}
		callCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		err = func() error {
			call, err := ingresspb.NewRunnerIngressClient(channel).Push(callCtx)
			if err != nil {
				return err
			}
			if err := call.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_SamplerAnnounce{SamplerAnnounce: &ingresspb.SamplerAnnouncement{Session: snapshot.GetAttached(), Executor: snapshot.GetExecutor()}}}); err != nil {
				return err
			}
			first, err := call.Recv()
			if err != nil {
				return fmt.Errorf("History initial acknowledgement: %w", err)
			}
			current, err := store.GetSession(callCtx, sessionID, "")
			if err != nil {
				return err
			}
			if first.GetSealed() || first.GetCursor() > current.Cursor {
				return errors.New("History acknowledgement conflicts with sampler journal")
			}
			after := first.GetCursor()
			for after < session.Cursor && pages < exportPagesPerTurn {
				entries, err := store.Events(callCtx, sessionID, after, min(exportPageSize, int(session.Cursor-after)))
				if err != nil {
					return err
				}
				if len(entries) == 0 {
					return errors.New("sampler journal missing committed evidence")
				}
				through := entries[len(entries)-1].GetCursor()
				tasks, pageCtx := errgroup.WithContext(callCtx)
				// A reader failure must also interrupt a sender blocked in gRPC flow control.
				tasks.Go(func() error {
					err := sendExportPage(call, entries)
					if err != nil {
						cancel()
					}
					return err
				})
				next := after
				tasks.Go(func() error {
					var err error
					next, err = acknowledgeExportPage(pageCtx, store, sessionID, target, call, after, through)
					if err != nil {
						cancel()
					}
					return err
				})
				if err := tasks.Wait(); err != nil {
					return err
				}
				after = next
				pages++
			}
			if err := call.CloseSend(); err != nil {
				return err
			}
			// Half-close and read through EOF to let History commit the announced metadata snapshot.
			for {
				ack, err := call.Recv()
				if errors.Is(err, io.EOF) {
					break
				}
				if err != nil {
					return err
				}
				if ack.GetSealed() || ack.GetCursor() < first.GetCursor() || ack.GetCursor() > after {
					return errors.New("History acknowledgement outside sent prefix")
				}
			}
			return store.CommitCheckpoint(callCtx, sessionID, target, after)
		}()
		cancel()
		if err != nil {
			return false, err
		}
		current, err := store.GetSession(ctx, sessionID, "")
		if err != nil {
			return false, err
		}
		checkpoint, err := store.Checkpoint(ctx, sessionID, target)
		if err != nil {
			return false, err
		}
		caughtUp := checkpoint != nil && *checkpoint == current.Cursor && session.Cursor == current.Cursor
		if caughtUp || pages == exportPagesPerTurn {
			return caughtUp, nil
		}
	}
	return false, nil
}

func sendExportPage(call grpc.BidiStreamingClient[ingresspb.PushFrame, ingresspb.Acknowledgement], entries []*eventlogpb.EventEntry) error {
	for _, entry := range entries {
		if err := call.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Entry{Entry: entry}}); err != nil {
			return err
		}
	}
	return call.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Heartbeat{Heartbeat: &ingresspb.Heartbeat{}}})
}

func acknowledgeExportPage(ctx context.Context, store *Store, sessionID, target string, call grpc.BidiStreamingClient[ingresspb.PushFrame, ingresspb.Acknowledgement], after, through uint64) (uint64, error) {
	for after < through {
		ack, err := call.Recv()
		if err != nil {
			return 0, fmt.Errorf("History batch acknowledgement: %w", err)
		}
		if ack.GetSealed() || ack.GetCursor() < after || ack.GetCursor() > through {
			return 0, errors.New("History acknowledgement outside sent prefix")
		}
		after = ack.GetCursor()
		if err := store.CommitCheckpoint(ctx, sessionID, target, after); err != nil {
			return 0, err
		}
	}
	return after, nil
}

// exportQueue schedules Sessions with pending outbox entries. One worker at a
// time exports a Session; one that exhausts its quantum goes to the back of
// the queue, and one that fails waits out a per-Session exponential backoff
// that resets on success.
type exportQueue struct {
	queue  workqueue.TypedRateLimitingInterface[string]
	export func(ctx context.Context, sessionID string) (caughtUp bool, err error)
}

func newExportQueue(config workqueue.TypedRateLimitingQueueConfig[string], export func(context.Context, string) (bool, error)) *exportQueue {
	return &exportQueue{
		queue:  workqueue.NewTypedRateLimitingQueueWithConfig(workqueue.NewTypedItemExponentialFailureRateLimiter[string](exportRetryBase, exportRetryMax), config),
		export: export,
	}
}

func (q *exportQueue) enqueuePending(ctx context.Context, store *Store, target string) error {
	after := ""
	for {
		ids, err := store.PendingSessionIDs(ctx, target, after, exportListPage)
		if err != nil {
			return err
		}
		for _, id := range ids {
			// Add would cut a failing Session's backoff short; its AddRateLimited already holds it.
			if q.queue.NumRequeues(id) == 0 {
				q.queue.Add(id)
			}
		}
		if len(ids) < exportListPage {
			return nil
		}
		after = ids[len(ids)-1]
	}
}

// processNext exports one queued Session; it returns false once the queue shuts down.
func (q *exportQueue) processNext(ctx context.Context) bool {
	id, shutdown := q.queue.Get()
	if shutdown {
		return false
	}
	defer q.queue.Done(id)
	caughtUp, err := q.export(ctx, id)
	switch {
	case ctx.Err() != nil:
	case err != nil:
		slog.Warn("History export failed; retaining outbox for replay", "session_id", id, "error", err, "failures", q.queue.NumRequeues(id)+1)
		q.queue.AddRateLimited(id)
	case caughtUp:
		q.queue.Forget(id)
	default:
		q.queue.Forget(id)
		q.queue.Add(id)
	}
	return true
}

func RunExporter(ctx context.Context, store *Store, target, tokenFile string, hub *wakeups.Hub) error {
	changed, unsubscribe, err := hub.Subscribe(OutboxChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	channel, err := bearer.Dial(target, tokenFile, 30*time.Second)
	if err != nil {
		return err
	}
	defer channel.Close()
	queue := newExportQueue(workqueue.TypedRateLimitingQueueConfig[string]{}, func(ctx context.Context, sessionID string) (bool, error) {
		return exportSession(ctx, store, sessionID, target, channel)
	})
	tasks, taskCtx := errgroup.WithContext(ctx)
	for range exportConcurrency {
		tasks.Go(func() error {
			for queue.processNext(taskCtx) {
			}
			return nil
		})
	}
	tasks.Go(func() error {
		defer queue.queue.ShutDown()
		for {
			if err := queue.enqueuePending(taskCtx, store, target); err != nil {
				if taskCtx.Err() != nil {
					return taskCtx.Err()
				}
				return err
			}
			select {
			case <-taskCtx.Done():
				return taskCtx.Err()
			case <-changed:
			}
		}
	})
	return tasks.Wait()
}
