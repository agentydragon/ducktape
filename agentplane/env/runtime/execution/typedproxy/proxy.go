// Package typedproxy enforces Runtime admission before forwarding to the local execution owner.
package typedproxy

import (
	"context"
	"errors"
	"sync"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type (
	Admit   func(context.Context, *pb.ExecutionScope, bool, func(context.Context) error) error
	session struct {
		scope      *pb.ExecutionScope
		ctx        context.Context
		cancel     context.CancelFunc
		done       chan struct{}
		calls      chan struct{}
		pending    sync.WaitGroup
		handles    map[string]uint64
		nextHandle uint64
		id         string
	}
)

type Proxy struct {
	pb.UnimplementedExecutorServer
	client   pb.ExecutorClient
	admit    Admit
	ctx      context.Context
	cancel   context.CancelFunc
	mu       sync.Mutex
	sessions map[string]*session
	poisoned map[string]error
	slots    map[uint64]context.CancelFunc
	nextSlot uint64
	opening  int
	closed   bool
	workers  sync.WaitGroup
}

func New(ctx context.Context, client pb.ExecutorClient, admit Admit) (*Proxy, error) {
	if ctx == nil || client == nil || admit == nil {
		return nil, errors.New("context, backend client and admission are required")
	}
	ctx, cancel := context.WithCancel(ctx)
	return &Proxy{client: client, admit: admit, ctx: ctx, cancel: cancel, sessions: map[string]*session{}, poisoned: map[string]error{}, slots: map[uint64]context.CancelFunc{}}, nil
}

func (p *Proxy) Close() error {
	p.mu.Lock()
	p.closed = true
	p.cancel()
	for _, cancel := range p.slots {
		cancel()
	}
	p.mu.Unlock()
	p.workers.Wait()
	return nil
}

func (p *Proxy) OpenSession(r *pb.OpenSessionRequest, stream pb.Executor_OpenSessionServer) error {
	if r.GetRequestId() == "" || len(r.GetRequestId()) > 256 {
		return status.Error(codes.InvalidArgument, "request ID is required and limited to 256 bytes")
	}
	if r.ResumeSessionId != nil && r.GetResumeSessionId() == "" {
		return status.Error(codes.InvalidArgument, "resume session ID cannot be empty")
	}
	var c context.Context
	var cancel context.CancelFunc
	var prior *session
	var slot uint64
	err := p.admit(stream.Context(), r.Scope, true, func(ctx context.Context) error {
		p.mu.Lock()
		defer p.mu.Unlock()
		if p.closed || p.ctx.Err() != nil {
			return status.Error(codes.Unavailable, "execution proxy is stopping")
		}
		if r.GetResumeSessionId() != "" && p.poisoned[r.GetResumeSessionId()] != nil {
			return status.Error(codes.Unavailable, "previous session handle cleanup failed")
		}
		if old := p.sessions[r.GetResumeSessionId()]; old != nil {
			if !proto.Equal(old.scope, r.Scope) {
				return status.Error(codes.PermissionDenied, "execution session belongs to a different scope")
			}
			prior = old
		}
		count := p.opening + len(p.sessions)
		if prior != nil {
			count--
		}
		if count >= 8 {
			return status.Error(codes.ResourceExhausted, "environment has too many open execution connections")
		}
		c, cancel = context.WithCancel(ctx)
		p.nextSlot++
		slot = p.nextSlot
		p.slots[slot] = cancel
		p.opening++
		p.workers.Add(1)
		return nil
	})
	if err != nil {
		return err
	}
	defer p.workers.Done()
	stopRoot := context.AfterFunc(p.ctx, cancel)
	defer stopRoot()
	stopCaller := context.AfterFunc(stream.Context(), cancel)
	defer stopCaller()
	defer cancel()
	var current *session
	registered := false
	defer func() {
		p.mu.Lock()
		delete(p.slots, slot)
		if !registered {
			p.opening--
		}
		p.mu.Unlock()
		if current != nil {
			// Join admitted calls before closing their handles; a replacement waits on done.
			cancel()
			current.pending.Wait()
			cleanup, stop := context.WithTimeout(context.Background(), 5*time.Second)
			var cleanupErr error
			for handle := range current.handles {
				response, err := p.client.CloseFile(cleanup, &pb.CloseFileRequest{Scope: current.scope, ExecutionSessionId: current.id, RequestId: "close-on-disconnect", HandleId: handle})
				if err != nil {
					cleanupErr = err
					break
				}
				outcome := response.GetStatus()
				if outcome.GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED && !(outcome.GetOutcome() == pb.ExecutionStatus_OUTCOME_REJECTED && outcome.GetMessage() == "file handle not found") {
					cleanupErr = errors.New("file handle close was not confirmed")
					break
				}
			}
			stop()
			p.mu.Lock()
			if cleanupErr != nil {
				p.poisoned[current.id] = cleanupErr
			}
			if p.sessions[current.id] == current {
				delete(p.sessions, current.id)
			}
			close(current.done)
			p.mu.Unlock()
		}
	}()
	if prior != nil {
		prior.cancel()
		select {
		case <-prior.done:
			p.mu.Lock()
			cleanupErr := p.poisoned[r.GetResumeSessionId()]
			p.mu.Unlock()
			if cleanupErr != nil {
				return status.Error(codes.Unavailable, "previous session handle cleanup failed")
			}
		case <-c.Done():
			return status.FromContextError(c.Err()).Err()
		}
	}
	backend, err := p.client.OpenSession(c, r)
	if err != nil {
		return err
	}
	first, err := backend.Recv()
	if err != nil {
		return err
	}
	if first.GetStatus().GetOutcome() == pb.ExecutionStatus_OUTCOME_COMPLETED {
		if first.ExecutionSessionId == "" || (r.ResumeSessionId != nil && first.ExecutionSessionId != r.GetResumeSessionId()) {
			return errors.New("backend returned unexpected execution session ID")
		}
		current = &session{scope: proto.Clone(r.Scope).(*pb.ExecutionScope), ctx: c, cancel: cancel, done: make(chan struct{}), calls: make(chan struct{}, 64), handles: make(map[string]uint64), id: first.ExecutionSessionId}
		p.mu.Lock()
		if p.closed || c.Err() != nil || p.sessions[first.ExecutionSessionId] != nil {
			p.mu.Unlock()
			return status.Error(codes.Unavailable, "execution proxy stopped during session open")
		}
		p.sessions[first.ExecutionSessionId] = current
		p.opening--
		registered = true
		p.mu.Unlock()
	}
	if err := streaming.SendWithin(c, 10*time.Second, stream.Send, first); err != nil {
		return err
	}
	if current == nil {
		return nil
	}
	for {
		resp, err := backend.Recv()
		if err != nil {
			if c.Err() != nil {
				return nil
			}
			return err
		}
		if err = streaming.SendWithin(c, 10*time.Second, stream.Send, resp); err != nil {
			return err
		}
	}
}

type request interface {
	GetScope() *pb.ExecutionScope
	GetRequestId() string
	GetExecutionSessionId() string
}

func call[T any](ctx context.Context, p *Proxy, r request, fn func(context.Context) (*T, error)) (*T, error) {
	if r.GetRequestId() == "" || len(r.GetRequestId()) > 256 || r.GetExecutionSessionId() == "" {
		return nil, status.Error(codes.InvalidArgument, "request ID and execution session ID required")
	}
	type result struct {
		v   *T
		err error
	}
	done := make(chan result, 1)
	err := p.admit(ctx, r.GetScope(), false, func(context.Context) error {
		p.mu.Lock()
		defer p.mu.Unlock()
		if p.closed || p.ctx.Err() != nil {
			return status.Error(codes.Unavailable, "execution proxy is stopping")
		}
		s := p.sessions[r.GetExecutionSessionId()]
		if s == nil || s.ctx.Err() != nil {
			return status.Error(codes.FailedPrecondition, "execution session connection is closed")
		}
		if !proto.Equal(s.scope, r.GetScope()) {
			return status.Error(codes.PermissionDenied, "execution session belongs to a different scope")
		}
		select {
		case s.calls <- struct{}{}:
		default:
			return status.Error(codes.ResourceExhausted, "execution session has too many outstanding calls")
		}
		var openID string
		var openGeneration uint64
		var openHadHandle bool
		if open, ok := r.(*pb.OpenFileRequest); ok && open.GetHandleId() != "" {
			openID = open.GetHandleId()
			_, openHadHandle = s.handles[openID]
			// Track even an uncertain outcome: the backend may have opened it.
			s.nextHandle++
			openGeneration = s.nextHandle
			s.handles[openID] = openGeneration
		}
		var closeID string
		var closeGeneration uint64
		if closeRequest, ok := r.(*pb.CloseFileRequest); ok {
			closeID = closeRequest.GetHandleId()
			closeGeneration = s.handles[closeID]
		}
		s.pending.Add(1)
		p.workers.Add(1)
		go func() {
			defer p.workers.Done()
			defer s.pending.Done()
			defer func() { <-s.calls }()
			v, err := fn(s.ctx)
			if response, ok := any(v).(*pb.OpenFileResponse); ok && err == nil && response.GetStatus().GetOutcome() == pb.ExecutionStatus_OUTCOME_REJECTED && !openHadHandle {
				p.mu.Lock()
				if openGeneration != 0 && s.handles[openID] == openGeneration {
					delete(s.handles, openID)
				}
				p.mu.Unlock()
			}
			if response, ok := any(v).(*pb.CloseFileResponse); ok && err == nil && response.GetStatus().GetOutcome() == pb.ExecutionStatus_OUTCOME_COMPLETED {
				p.mu.Lock()
				if closeGeneration != 0 && s.handles[closeID] == closeGeneration {
					delete(s.handles, closeID)
				}
				p.mu.Unlock()
			}
			done <- result{v, err}
		}()
		return nil
	})
	if err != nil {
		return nil, err
	}
	select {
	case r := <-done:
		return r.v, r.err
	case <-ctx.Done():
		return nil, status.FromContextError(ctx.Err()).Err()
	}
}

func (p *Proxy) StartProcess(ctx context.Context, r *pb.StartProcessRequest) (*pb.StartProcessResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.StartProcessResponse, error) { return p.client.StartProcess(c, r) })
}

func (p *Proxy) ReadProcess(ctx context.Context, r *pb.ReadProcessRequest) (*pb.ReadProcessResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.ReadProcessResponse, error) { return p.client.ReadProcess(c, r) })
}

func (p *Proxy) WriteProcess(ctx context.Context, r *pb.WriteProcessRequest) (*pb.WriteProcessResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.WriteProcessResponse, error) { return p.client.WriteProcess(c, r) })
}

func (p *Proxy) SignalProcess(ctx context.Context, r *pb.SignalProcessRequest) (*pb.SignalProcessResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.SignalProcessResponse, error) { return p.client.SignalProcess(c, r) })
}

func (p *Proxy) TerminateProcess(ctx context.Context, r *pb.TerminateProcessRequest) (*pb.TerminateProcessResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.TerminateProcessResponse, error) { return p.client.TerminateProcess(c, r) })
}

func (p *Proxy) GetFileMetadata(ctx context.Context, r *pb.GetFileMetadataRequest) (*pb.GetFileMetadataResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.GetFileMetadataResponse, error) { return p.client.GetFileMetadata(c, r) })
}

func (p *Proxy) OpenFile(ctx context.Context, r *pb.OpenFileRequest) (*pb.OpenFileResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.OpenFileResponse, error) { return p.client.OpenFile(c, r) })
}

func (p *Proxy) ReadFile(ctx context.Context, r *pb.ReadFileRequest) (*pb.ReadFileResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.ReadFileResponse, error) { return p.client.ReadFile(c, r) })
}

func (p *Proxy) CloseFile(ctx context.Context, r *pb.CloseFileRequest) (*pb.CloseFileResponse, error) {
	return call(ctx, p, r, func(c context.Context) (*pb.CloseFileResponse, error) { return p.client.CloseFile(c, r) })
}
