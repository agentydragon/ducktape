// Package ingress serves SessionJournalIngress: a runner pushes each Session's journal here, one call per Session.
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
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
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

// Service is the SessionJournalIngress server. Each call uses the subject admitted by the RPC method, and the
// Session it announces is bound to the ServiceAccount or Identity that first pushed it.
type Service struct {
	session_ingresspb.UnimplementedSessionJournalIngressServer
	SandboxUIDs     SandboxUIDResolver
	History         *storage.Store
	Timing          Timing
	SamplerAccounts []auth.Account
}

type pushStream = grpc.BidiStreamingServer[session_ingresspb.PushFrame, session_ingresspb.Acknowledgement]

// pusher is a Push caller: an accountPusher, an identityPusher or a delegatingSamplerPusher.
type pusher interface{ isPusher() }

// accountPusher is a runner or central sampler authenticated as a ServiceAccount.
type accountPusher struct{ storage.ServiceAccountWriter }

// identityPusher is a runner authenticated as an Identity, with the Sandbox owning its workload
// credential.
type identityPusher struct {
	writer  storage.IdentityWriter
	sandbox storage.Sandbox
}

// delegatingSamplerPusher is a central sampler acting for the Identity a Session executes as. The
// sampler stays the journal writer; the Identity, verified live by the middleware, is the Session's
// retained execution Identity, and sessionID is the Session its envelope names.
type delegatingSamplerPusher struct {
	actor     storage.ServiceAccountWriter
	identity  storage.Identity
	sessionID string
}

func (accountPusher) isPusher()           {}
func (identityPusher) isPusher()          {}
func (delegatingSamplerPusher) isPusher() {}

// AllowDelegatingSamplers grants each configured sampler account the delegated mode on the Push
// rule: asserting the Identity a central Session executes as, resolved live by identities.
func AllowDelegatingSamplers(push auth.Rule, samplers []auth.Account, identities auth.IdentityUIDReader) (auth.Rule, error) {
	delegators := make([]auth.Delegator, 0, len(samplers))
	for _, sampler := range samplers {
		delegators = append(delegators, auth.Delegator{Account: sampler, IdentityCallers: true})
	}
	return push.AllowDelegationFrom(delegators, nil, identities)
}

func (s *Service) Push(stream pushStream) error {
	ctx := stream.Context()
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return err
	}
	var caller pusher
	switch admission := admission.(type) {
	case auth.IdentityAdmission:
		if caller, err = newIdentityPusher(admission.Caller); err != nil {
			return err
		}
	case auth.DelegatedIdentityAdmission:
		session := admission.Provenance.GetSession()
		if session == nil {
			return status.Error(codes.PermissionDenied, "a delegating sampler names the Session it pushes")
		}
		caller = delegatingSamplerPusher{
			actor: storage.ServiceAccountWriter{
				Namespace: admission.Actor.Account.Namespace, Name: admission.Actor.Account.Name, UID: admission.Actor.ServiceAccountUID,
			},
			identity:  storage.Identity{Name: admission.EffectiveCaller.Name, UID: admission.EffectiveCaller.UID},
			sessionID: session.GetSessionId(),
		}
	default:
		principal, err := auth.Caller(ctx)
		if err != nil {
			return err
		}
		caller = accountPusher{storage.ServiceAccountWriter{
			Namespace: principal.Account.Namespace, Name: principal.Account.Name, UID: principal.ServiceAccountUID,
		}}
	}
	frames := readFrames(stream)
	defer frames.stop()
	c := &pushCall{history: s.History, timing: s.Timing, stream: stream, frames: frames, log: slog.Default(), service: s}
	return c.failure(c.run(ctx, caller))
}

// samplerWriter is the ServiceAccount a central sampler pushes as, whether as itself or acting for
// an Identity; a runner is no sampler.
func samplerWriter(caller pusher) (storage.ServiceAccountWriter, bool) {
	switch caller := caller.(type) {
	case accountPusher:
		return caller.ServiceAccountWriter, true
	case delegatingSamplerPusher:
		return caller.actor, true
	default:
		return storage.ServiceAccountWriter{}, false
	}
}

// newIdentityPusher pins an Identity runner to the Sandbox that controls its workload credential, as
// the Sandbox controller's ownerReference names it. A record no Sandbox controls writes no history.
func newIdentityPusher(caller auth.IdentityPrincipal) (identityPusher, error) {
	owner, ok := caller.Workload.Owner.(auth.ControllerOwner)
	if !ok || owner.APIVersion != sandboxapi.GroupVersion.String() || owner.Kind != sandboxapi.SandboxKind || owner.UID == "" {
		return identityPusher{}, status.Error(codes.PermissionDenied, "the workload credential is not a Sandbox's")
	}
	return identityPusher{
		writer:  storage.IdentityWriter{Identity: storage.Identity{Name: caller.Identity.Name, UID: caller.Identity.UID}},
		sandbox: storage.Sandbox{Namespace: caller.Workload.Namespace, Name: owner.Name, UID: owner.UID},
	}, nil
}

type pushCall struct {
	history *storage.Store
	timing  Timing
	stream  pushStream
	frames  *frameReader
	log     *slog.Logger
	service *Service
}

func (c *pushCall) run(ctx context.Context, caller pusher) error {
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
	var executionIdentity *storage.Identity
	if sampler != nil {
		account, ok := samplerWriter(caller)
		if !ok || !slices.Contains(c.service.SamplerAccounts, auth.Account{Namespace: account.Namespace, Name: account.Name}) {
			return status.Error(codes.PermissionDenied, "central sampler producer is not authorized")
		}
		announce = sampler.GetSession()
		executor := sampler.GetExecutor()
		if announce == nil || announce.GetSpec().GetPlacement().GetCentralSamplerV1() == nil ||
			executor.GetTarget().GetNamespace() == "" || executor.GetTarget().GetName() == "" ||
			announce.GetSpec().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetName() != executor.GetTarget().GetName() ||
			announce.GetSpec().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetUid() != executor.GetTarget().GetUid() ||
			executor.GetOwner().GetNamespace() == "" || executor.GetOwner().GetName() == "" || executor.GetTarget().GetNamespace() != executor.GetOwner().GetNamespace() {
			return status.Error(codes.InvalidArgument, "central sampler announcement is incomplete")
		}
		if _, err := uuid.Parse(executor.GetTarget().GetUid()); err != nil {
			return status.Error(codes.InvalidArgument, "executor UID is not a UUID")
		}
		sandboxUID = executor.GetTarget().GetUid()
	} else {
		if announce == nil {
			return status.Error(codes.InvalidArgument, "the first frame is not an announcement")
		}
		if announce.GetSpec().GetPlacement().GetCentralSamplerV1() != nil {
			return status.Error(codes.PermissionDenied, "central Sessions require a sampler announcement")
		}
		switch caller := caller.(type) {
		case delegatingSamplerPusher:
			return status.Error(codes.PermissionDenied, "a delegating sampler announces a central Session")
		case accountPusher:
			if c.service.SandboxUIDs != nil {
				var err error
				sandboxUID, err = c.service.SandboxUIDs.Resolve(ctx, caller.ServiceAccountWriter)
				if err != nil {
					return err
				}
			}
		case identityPusher:
			sandboxUID = caller.sandbox.UID
		}
	}
	if identity := announce.GetExecutionPrincipal(); identity != nil {
		if identity.GetAccount().GetNamespace() == "" || identity.GetAccount().GetName() == "" || identity.GetUid() == "" {
			return status.Error(codes.InvalidArgument, "execution principal is incomplete")
		}
		executionPrincipal = &storage.ExecutionPrincipal{
			Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName(), UID: identity.GetUid(),
		}
		if sampler != nil {
			owner := sampler.GetExecutor().GetOwner()
			if owner == nil || identity.GetAccount().GetNamespace() != owner.GetNamespace() || identity.GetAccount().GetName() != owner.GetName() {
				return status.Error(codes.PermissionDenied, "session execution principal does not match its executor")
			}
		} else if err := c.checkRunnerPrincipal(ctx, caller, *executionPrincipal); err != nil {
			return err
		}
	}
	// The Dispatcher records the Identity a Session executes as; only a writer that can vouch for it
	// is retained: an Identity runner running as it, a central sampler acting for it, or a
	// configured central sampler relaying it.
	if identity := announce.GetExecutionIdentity(); identity != nil {
		if identity.GetName() == "" || identity.GetUid() == "" {
			return status.Error(codes.InvalidArgument, "execution identity is incomplete")
		}
		announced := storage.Identity{Name: identity.GetName(), UID: identity.GetUid()}
		switch caller := caller.(type) {
		case identityPusher:
			if announced != caller.writer.Identity {
				return status.Error(codes.PermissionDenied, "session execution identity does not match the runner identity")
			}
			executionIdentity = &announced
		case delegatingSamplerPusher:
			if announced != caller.identity {
				return status.Error(codes.PermissionDenied, "session execution identity does not match the delegated Identity")
			}
			executionIdentity = &announced
		case accountPusher:
			if sampler != nil {
				executionIdentity = &announced
			}
		}
	} else if _, delegating := caller.(delegatingSamplerPusher); delegating {
		// A Session with no Identity is the sampler's own to push; acting for one it does not
		// execute as would misattribute the journal.
		return status.Error(codes.PermissionDenied, "the delegated Identity is not the session execution identity")
	}
	sessionID, err := uuid.Parse(announce.GetSessionId())
	if err != nil {
		return status.Error(codes.InvalidArgument, "the Session ID is not a UUID")
	}
	// The feed snapshot is stored under the canonical spelling, so only that one is accepted.
	if sessionID.String() != announce.GetSessionId() {
		return status.Error(codes.InvalidArgument, "the Session ID is not a canonical UUID")
	}
	if delegating, ok := caller.(delegatingSamplerPusher); ok && delegating.sessionID != announce.GetSessionId() {
		return status.Error(codes.PermissionDenied, "the announced Session is not the one the delegation names")
	}
	c.log = c.log.With("session_id", sessionID)
	var checkpoint storage.Checkpoint
	if err := auth.RevalidateStream(ctx); err != nil {
		return err
	}
	switch caller := caller.(type) {
	case accountPusher:
		if sampler != nil {
			executor := sampler.GetExecutor()
			checkpoint, err = c.history.BindSamplerWriter(ctx, sessionID, caller.ServiceAccountWriter, storage.Sandbox{
				Namespace: executor.GetTarget().GetNamespace(), Name: executor.GetTarget().GetName(), UID: sandboxUID,
			}, executionPrincipal, executionIdentity)
		} else {
			checkpoint, err = c.history.BindWriter(ctx, sessionID, caller.ServiceAccountWriter, sandboxUID, executionPrincipal)
		}
	case identityPusher:
		checkpoint, err = c.history.BindIdentityWriter(ctx, sessionID, caller.writer, caller.sandbox, executionPrincipal, executionIdentity)
	case delegatingSamplerPusher:
		executor := sampler.GetExecutor()
		checkpoint, err = c.history.BindSamplerWriter(ctx, sessionID, caller.actor, storage.Sandbox{
			Namespace: executor.GetTarget().GetNamespace(), Name: executor.GetTarget().GetName(), UID: sandboxUID,
		}, executionPrincipal, executionIdentity)
	}
	if err != nil {
		return err
	}
	if checkpoint.Seal != nil && announce.IncarnationUid != nil {
		// An incarnation other than the one that sealed continues the journal; the same one gets the seal back.
		if err := auth.RevalidateStream(ctx); err != nil {
			return err
		}
		if checkpoint, err = c.history.ReopenSealed(ctx, sessionID, announce.GetIncarnationUid(), announce.GetLastCursor()); err != nil {
			return err
		}
	}
	if err := c.stream.Send(&session_ingresspb.Acknowledgement{Cursor: checkpoint.Cursor, Sealed: checkpoint.Seal != nil}); err != nil {
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

// checkRunnerPrincipal admits a runner's announced execution ServiceAccount: a ServiceAccount runner's
// own, or the ServiceAccount the Identity runner's Sandbox controls.
func (c *pushCall) checkRunnerPrincipal(ctx context.Context, caller pusher, principal storage.ExecutionPrincipal) error {
	switch caller := caller.(type) {
	case accountPusher:
		if principal != storage.ExecutionPrincipal(caller.ServiceAccountWriter) {
			return status.Error(codes.PermissionDenied, "session execution principal does not match the runner account")
		}
	case identityPusher:
		if c.service.SandboxUIDs == nil {
			return status.Error(codes.PermissionDenied, "session execution principal cannot be verified")
		}
		controller, err := c.service.SandboxUIDs.Resolve(ctx, storage.ServiceAccountWriter(principal))
		if err != nil {
			return err
		}
		if controller != caller.sandbox.UID {
			return status.Error(codes.PermissionDenied, "session execution principal is not the runner Sandbox's account")
		}
	}
	return nil
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
			if err := auth.RevalidateStream(ctx); err != nil {
				return err
			}
			if err := c.acknowledge(j.committed, false); err != nil {
				return err
			}
			continue
		}
		silent = 0
		switch body := frame.GetFrame().(type) {
		case *session_ingresspb.PushFrame_Heartbeat:
			if len(j.batch) > 0 {
				if err := c.commitAndAcknowledge(ctx, j); err != nil {
					return err
				}
			} else if err := auth.RevalidateStream(ctx); err != nil {
				return err
			}
		case *session_ingresspb.PushFrame_Entry:
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
		case *session_ingresspb.PushFrame_Sealed:
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
	return c.stream.Send(&session_ingresspb.Acknowledgement{Cursor: cursor, Sealed: sealed})
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
	announce  *sessionpb.Attached
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
		if err := auth.RevalidateStream(ctx); err != nil {
			return err
		}
		committed, err := j.history.Append(ctx, j.sessionID, j.batch)
		if err != nil {
			return err
		}
		j.committed, j.batch, j.batchBytes = committed, j.batch[:0], 0
	}
	// The announced snapshot is stored once history covers it.
	if !j.snapshotRecorded && j.committed >= j.announce.GetLastCursor() {
		if err := auth.RevalidateStream(ctx); err != nil {
			return err
		}
		if err := j.history.RecordFeedState(ctx, j.sessionID, &historypb.SessionFeedState{Attached: j.announce}); err != nil {
			return err
		}
		j.snapshotRecorded = true
	}
	return nil
}

func (j *journal) seal(ctx context.Context, sealed *sessionpb.Sealed) error {
	if sealed.GetCursor() != j.last {
		return status.Error(codes.InvalidArgument, "the seal does not follow the last entry")
	}
	if err := j.commit(ctx); err != nil {
		return err
	}
	if err := auth.RevalidateStream(ctx); err != nil {
		return err
	}
	return j.history.RecordFeedState(ctx, j.sessionID, &historypb.SessionFeedState{Attached: j.announce, Sealed: sealed})
}

type received struct {
	frame *session_ingresspb.PushFrame
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
func (f *frameReader) next(timeout time.Duration) (frame *session_ingresspb.PushFrame, timedOut bool, err error) {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case item := <-f.queue:
		return item.frame, false, item.err
	case <-timer.C:
		return nil, true, nil
	}
}
