// Package ingress serves RunnerIngress: a runner pushes each Session's journal here, one call per Session.
//
// Contract: agentplane/docs/runner_control.md § Event ingress. Every acknowledgement follows a commit, so the
// runner's resume point is always durable here; Store.Append accepts exact replays and serializes concurrent
// calls for one Session, so any replica serves any call.
package ingress

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"slices"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	runneringresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	// frameQueueLimit bounds the decoded frames held while a commit or acknowledgement holds up the receiver;
	// gRPC's default receive limit is 4 MiB per frame.
	frameQueueLimit  = 16
	commitEntryLimit = 128
	commitByteLimit  = 1024 * 1024
)

// Timing paces a call. Runners heartbeat on an idle call at the rate the protocol names, so a deployment
// serves with the protocol's values; only a test shortens them.
type Timing struct {
	// IdleAck is how long the ingress waits for a frame before it repeats its acknowledgement.
	IdleAck time.Duration
	// SilenceLimit is how long a call may go without a frame before the ingress ends it.
	SilenceLimit time.Duration
}

// Service is the RunnerIngress server. Each call is authenticated by Principals, and the Session it announces
// is bound to the ServiceAccount that first pushed it.
type Service struct {
	runneringresspb.UnimplementedRunnerIngressServer
	Principals      *auth.Resolver
	SandboxUIDs     SandboxUIDResolver
	History         *storage.Store
	Timing          Timing
	SamplerAccounts []auth.Account
}

type pushStream = grpc.BidiStreamingServer[runneringresspb.PushFrame, runneringresspb.Acknowledgement]

func (s *Service) Push(stream pushStream) error {
	ctx := stream.Context()
	principal, err := s.Principals.Resolve(ctx)
	if err != nil {
		return err
	}
	frames := readFrames(stream)
	defer frames.stop()
	c := &pushCall{history: s.History, timing: s.Timing, stream: stream, frames: frames, log: slog.Default(), service: s}
	writer := storage.Writer{
		Namespace: principal.Account.Namespace, Name: principal.Account.Name, UID: principal.ServiceAccountUID,
	}
	return c.failure(c.run(ctx, writer, principal.Account))
}

type pushCall struct {
	history *storage.Store
	timing  Timing
	stream  pushStream
	frames  *frameReader
	log     *slog.Logger
	service *Service
}

func (c *pushCall) run(ctx context.Context, writer storage.Writer, account auth.Account) error {
	first, timedOut, err := c.frames.next(c.timing.SilenceLimit)
	switch {
	case errors.Is(err, io.EOF):
		return status.Error(codes.InvalidArgument, "the call ended unannounced")
	case err != nil:
		return err
	case timedOut:
		return status.Error(codes.Unavailable, "no announcement")
	}
	announce := first.GetAnnounce()
	sampler := first.GetSamplerAnnounce()
	var sandboxUID string
	var executionPrincipal *storage.ExecutionPrincipal
	if sampler != nil {
		if !slices.Contains(c.service.SamplerAccounts, account) {
			return status.Error(codes.PermissionDenied, "central sampler producer is not authorized")
		}
		announce = sampler.GetSession()
		executor := sampler.GetExecutor()
		if announce == nil || announce.GetSpec().GetHarness() != runnerpb.Harness_HARNESS_CENTRAL_SAMPLER ||
			executor.GetSandbox() == "" ||
			executor.GetOwner().GetNamespace() == "" || executor.GetOwner().GetName() == "" {
			return status.Error(codes.InvalidArgument, "central sampler announcement is incomplete")
		}
		if _, err := uuid.Parse(executor.GetSandboxUid()); err != nil {
			return status.Error(codes.InvalidArgument, "executor UID is not a UUID")
		}
		sandboxUID = executor.GetSandboxUid()
	} else {
		if announce == nil {
			return status.Error(codes.InvalidArgument, "the first frame is not an announcement")
		}
		if announce.GetSpec().GetHarness() == runnerpb.Harness_HARNESS_CENTRAL_SAMPLER {
			return status.Error(codes.PermissionDenied, "central Sessions require a sampler announcement")
		}
		if c.service.SandboxUIDs != nil {
			var err error
			sandboxUID, err = c.service.SandboxUIDs.Resolve(ctx, writer)
			if err != nil {
				return err
			}
		}
	}
	if identity := announce.GetExecutionPrincipal(); identity != nil {
		if identity.GetAccount().GetNamespace() == "" || identity.GetAccount().GetName() == "" || identity.GetUid() == "" {
			return status.Error(codes.InvalidArgument, "execution principal is incomplete")
		}
		if sampler != nil {
			owner := sampler.GetExecutor().GetOwner()
			if owner == nil || identity.GetAccount().GetNamespace() != owner.GetNamespace() || identity.GetAccount().GetName() != owner.GetName() {
				return status.Error(codes.PermissionDenied, "session execution principal does not match its executor")
			}
		} else if identity.GetAccount().GetNamespace() != writer.Namespace || identity.GetAccount().GetName() != writer.Name || identity.GetUid() != writer.UID {
			return status.Error(codes.PermissionDenied, "session execution principal does not match the runner account")
		}
		executionPrincipal = &storage.ExecutionPrincipal{
			Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName(), UID: identity.GetUid(),
		}
	}
	sessionID, err := uuid.Parse(announce.GetSessionId())
	if err != nil {
		return status.Error(codes.InvalidArgument, "the Session ID is not a UUID")
	}
	// The feed snapshot is stored under the canonical spelling, so only that one is accepted.
	if sessionID.String() != announce.GetSessionId() {
		return status.Error(codes.InvalidArgument, "the Session ID is not a canonical UUID")
	}
	c.log = c.log.With("session_id", sessionID)
	var checkpoint storage.Checkpoint
	if sampler != nil {
		executor := sampler.GetExecutor()
		checkpoint, err = c.history.BindSamplerWriter(ctx, sessionID, writer,
			executor.GetOwner().GetNamespace(), executor.GetSandbox(), sandboxUID, executionPrincipal)
	} else {
		checkpoint, err = c.history.BindWriter(ctx, sessionID, writer, sandboxUID, executionPrincipal)
	}
	if err != nil {
		return err
	}
	if checkpoint.Seal != nil && announce.IncarnationUid != nil {
		// An incarnation other than the one that sealed continues the journal; the same one gets the seal back.
		if checkpoint, err = c.history.ReopenSealed(ctx, sessionID, announce.GetIncarnationUid(), announce.GetLastCursor()); err != nil {
			return err
		}
	}
	if err := c.stream.Send(&runneringresspb.Acknowledgement{Cursor: checkpoint.Cursor, Sealed: checkpoint.Seal != nil}); err != nil {
		return err
	}
	if checkpoint.Seal != nil {
		return nil
	}
	j := &journal{history: c.history, sessionID: sessionID, announce: announce, committed: checkpoint.Cursor, last: checkpoint.Cursor}
	if err := j.commit(ctx); err != nil {
		return err
	}
	return c.receive(ctx, j)
}

func (c *pushCall) receive(ctx context.Context, j *journal) error {
	var silent time.Duration
	for {
		frame, timedOut, err := c.frames.next(c.timing.IdleAck)
		switch {
		case errors.Is(err, io.EOF):
			// The runner reconnects and resumes after the last acknowledgement; keep what arrived.
			return j.commit(ctx)
		case err != nil:
			return err
		case timedOut:
			if silent += c.timing.IdleAck; silent >= c.timing.SilenceLimit {
				return status.Error(codes.Unavailable, "no frame within the silence limit")
			}
			if err := c.acknowledge(j.committed, false); err != nil {
				return err
			}
			continue
		}
		silent = 0
		switch body := frame.GetFrame().(type) {
		case *runneringresspb.PushFrame_Heartbeat:
			if len(j.batch) > 0 {
				if err := c.commitAndAcknowledge(ctx, j); err != nil {
					return err
				}
			}
		case *runneringresspb.PushFrame_Entry:
			size := proto.Size(body.Entry)
			if len(j.batch) > 0 && j.batchBytes+size > commitByteLimit {
				if err := j.commit(ctx); err != nil {
					return err
				}
			}
			if err := j.take(body.Entry, size); err != nil {
				return err
			}
			// Keep transactions bounded even if the sender never pauses.
			if len(j.batch) >= commitEntryLimit || !c.frames.ready() {
				if err := c.commitAndAcknowledge(ctx, j); err != nil {
					return err
				}
			}
		case *runneringresspb.PushFrame_Sealed:
			if err := j.seal(ctx, body.Sealed); err != nil {
				return err
			}
			return c.acknowledge(j.committed, true)
		default:
			return status.Error(codes.InvalidArgument, "unexpected frame")
		}
	}
}

func (c *pushCall) commitAndAcknowledge(ctx context.Context, j *journal) error {
	if err := j.commit(ctx); err != nil {
		return err
	}
	return c.acknowledge(j.committed, false)
}

func (c *pushCall) acknowledge(cursor uint64, sealed bool) error {
	return c.stream.Send(&runneringresspb.Acknowledgement{Cursor: cursor, Sealed: sealed})
}

// failure answers a failed call. Refusals are already status errors; the message of any other error can carry
// SQL or connection details, so the client gets a fixed one and the log the cause.
func (c *pushCall) failure(err error) error {
	switch {
	case err == nil:
		return nil
	case errors.Is(err, storage.ErrWriterMismatch):
		c.log.Warn("push refused: the Session belongs to another writer", "error", err)
		return status.Error(codes.PermissionDenied, "the Session belongs to another writer")
	case errors.Is(err, storage.ErrSandboxMismatch):
		c.log.Warn("push refused: the Session belongs to another Sandbox", "error", err)
		return status.Error(codes.PermissionDenied, "the Session belongs to another Sandbox")
	case errors.Is(err, storage.ErrExecutionPrincipalMismatch):
		c.log.Warn("push refused: the Session execution principal differs from retained identity", "error", err)
		return status.Error(codes.PermissionDenied, "the Session execution principal differs from retained identity")
	case errors.Is(err, storage.ErrConflict):
		c.log.Warn("push refused", "error", err)
		return status.Error(codes.FailedPrecondition, "the journal differs from committed history")
	case status.Code(err) != codes.Unknown:
		return err
	default:
		c.log.Warn("push failed", "error", err)
		return status.Error(codes.Unavailable, "history or authentication unavailable")
	}
}

// journal is one call's view of a Session: what it committed and what the runner may send next.
type journal struct {
	history   *storage.Store
	sessionID uuid.UUID
	announce  *runnerpb.Attached
	committed uint64
	// last is the cursor of the last entry this call carried, or the committed cursor before it carried any.
	last uint64
	// started is set by the call's first entry, which may start anywhere through one past the committed
	// cursor; every later one follows the last.
	started          bool
	batch            []*eventlogpb.EventEntry
	batchBytes       int
	snapshotRecorded bool
}

func (j *journal) take(entry *eventlogpb.EventEntry, size int) error {
	switch {
	case !j.started && (entry.GetCursor() < 1 || entry.GetCursor() > j.committed+1):
		return status.Error(codes.InvalidArgument, "the first entry leaves a gap")
	case j.started && entry.GetCursor() != j.last+1:
		return status.Error(codes.InvalidArgument, "entries are not consecutive")
	}
	j.started, j.last = true, entry.GetCursor()
	j.batch = append(j.batch, entry)
	j.batchBytes += size
	return nil
}

func (j *journal) commit(ctx context.Context) error {
	if len(j.batch) > 0 {
		committed, err := j.history.Append(ctx, j.sessionID, j.batch)
		if err != nil {
			return err
		}
		j.committed, j.batch, j.batchBytes = committed, j.batch[:0], 0
	}
	// The announced snapshot is stored once history covers it.
	if !j.snapshotRecorded && j.committed >= j.announce.GetLastCursor() {
		if err := j.history.RecordFeedState(ctx, j.sessionID, &historypb.SessionFeedState{Attached: j.announce}); err != nil {
			return err
		}
		j.snapshotRecorded = true
	}
	return nil
}

func (j *journal) seal(ctx context.Context, sealed *runnerpb.Sealed) error {
	if sealed.GetCursor() != j.last {
		return status.Error(codes.InvalidArgument, "the seal does not follow the last entry")
	}
	if err := j.commit(ctx); err != nil {
		return err
	}
	return j.history.RecordFeedState(ctx, j.sessionID, &historypb.SessionFeedState{Attached: j.announce, Sealed: sealed})
}

type received struct {
	frame *runneringresspb.PushFrame
	err   error
}

// frameReader reads the call's frames on a goroutine, so waiting for one with a timeout never abandons a read.
type frameReader struct {
	queue chan received
	done  chan struct{}
}

func readFrames(stream pushStream) *frameReader {
	f := &frameReader{queue: make(chan received, frameQueueLimit), done: make(chan struct{})}
	go func() {
		for {
			frame, err := stream.Recv()
			select {
			case f.queue <- received{frame: frame, err: err}:
			case <-f.done:
				return
			}
			if err != nil {
				return
			}
		}
	}()
	return f
}

func (f *frameReader) stop() { close(f.done) }

func (f *frameReader) ready() bool { return len(f.queue) > 0 }

// next returns the next frame; io.EOF once the runner has closed its side of the call; or timedOut after
// timeout without either.
func (f *frameReader) next(timeout time.Duration) (frame *runneringresspb.PushFrame, timedOut bool, err error) {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case item := <-f.queue:
		return item.frame, false, item.err
	case <-timer.C:
		return nil, true, nil
	}
}
