package control

import (
	"context"
	"sync"
	"time"

	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Bound the stream's outbound payload backlog independently of per-channel queues.
const executorSendQueue = 32

type frameWrite struct {
	frame    *controlpb.RunnerFrame
	deadline time.Time
	done     chan error
}

// sender keeps WebSocket payload traffic behind Runner replies and heartbeats. The
// Gateway does the same on its side of the stream, so a full data queue cannot starve control.
type sender struct {
	ctx      context.Context
	cancel   context.CancelFunc
	timeout  time.Duration
	send     func(*controlpb.RunnerFrame) error
	priority chan *frameWrite
	data     chan *frameWrite
	done     chan struct{}
	stopped  chan struct{}
	mu       sync.Mutex
	err      error
}

func newSender(ctx context.Context, cancel context.CancelFunc, timeout time.Duration, send func(*controlpb.RunnerFrame) error) *sender {
	sender := &sender{
		ctx: ctx, cancel: cancel, timeout: timeout, send: send, priority: make(chan *frameWrite, 32),
		data: make(chan *frameWrite, executorSendQueue), done: make(chan struct{}), stopped: make(chan struct{}),
	}
	go sender.run()
	return sender
}

func (s *sender) wait() {
	<-s.stopped
}

func (s *sender) Send(frame *controlpb.RunnerFrame) error {
	return s.enqueue(frame, s.priority)
}

func (s *sender) SendExecutor(frame *controlpb.RunnerFrame) error {
	return s.enqueue(frame, s.data)
}

func (s *sender) enqueue(frame *controlpb.RunnerFrame, queue chan *frameWrite) error {
	request := &frameWrite{frame: frame, deadline: time.Now().Add(s.timeout), done: make(chan error, 1)}
	timerFinished := make(chan struct{})
	timer := time.AfterFunc(time.Until(request.deadline), func() {
		s.fail(status.Error(codes.Unavailable, "runner control stream send timed out"))
		close(timerFinished)
	})
	defer func() {
		if !timer.Stop() {
			// Stop can return false while the callback is still recording the terminal error.
			<-timerFinished
		}
	}()
	select {
	case queue <- request:
	case <-s.done:
		return s.error()
	case <-s.ctx.Done():
		return s.failureOr(status.FromContextError(s.ctx.Err()).Err())
	}
	select {
	case err := <-request.done:
		return s.failureOr(err)
	case <-s.done:
		return s.error()
	case <-s.ctx.Done():
		return s.failureOr(status.FromContextError(s.ctx.Err()).Err())
	}
}

func (s *sender) run() {
	defer close(s.stopped)
	for {
		select {
		case <-s.ctx.Done():
			s.fail(status.FromContextError(s.ctx.Err()).Err())
			return
		case <-s.done:
			return
		default:
		}
		select {
		case request := <-s.priority:
			s.write(request)
			continue
		default:
		}
		select {
		case request := <-s.priority:
			s.write(request)
		case request := <-s.data:
			s.write(request)
		case <-s.ctx.Done():
			s.fail(status.FromContextError(s.ctx.Err()).Err())
			return
		}
	}
}

func (s *sender) write(request *frameWrite) {
	if err := s.errorIfFailed(); err != nil {
		request.done <- err
		return
	}
	if time.Until(request.deadline) <= 0 {
		s.fail(status.Error(codes.Unavailable, "runner control stream send timed out"))
		request.done <- s.error()
		return
	}
	if err := s.send(request.frame); err != nil {
		s.fail(err)
	}
	request.done <- s.failureOr(nil)
}

func (s *sender) fail(err error) {
	s.mu.Lock()
	if s.err == nil {
		s.err = err
		close(s.done)
		s.mu.Unlock()
		s.cancel()
		return
	}
	s.mu.Unlock()
}

func (s *sender) errorIfFailed() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}

func (s *sender) error() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return s.err
	}
	return status.Error(codes.Unavailable, "runner control stream ended")
}

func (s *sender) failureOr(fallback error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return s.err
	}
	return fallback
}
