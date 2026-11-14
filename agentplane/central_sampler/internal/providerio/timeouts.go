package providerio

import (
	"context"
	"errors"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

// WithProviderIdleTimeout starts the idle budget after the response head is durably recorded and
// resets it only after the provider parser reports durable semantic progress. The returned stop
// function cancels and joins its timer goroutine.
func WithProviderIdleTimeout(parent context.Context, timeout time.Duration, progress protocol.ProviderProgress) (context.Context, protocol.ProviderHooks, func()) {
	ctx, cancel := context.WithCancelCause(parent)
	responseHeads := make(chan chan struct{})
	semanticProgress := make(chan chan error)
	done := make(chan struct{})

	go func() {
		defer close(done)
		var timer *time.Timer
		var timerC <-chan time.Time
		var deadline time.Time
		started := false
		defer func() {
			if timer != nil {
				stopAndDrainTimer(timer)
			}
		}()

		for {
			select {
			case <-ctx.Done():
				return
			case ack := <-responseHeads:
				if !started {
					started = true
					deadline = time.Now().Add(timeout)
					timer = time.NewTimer(timeout)
					timerC = timer.C
				}
				close(ack)
			case ack := <-semanticProgress:
				if !started {
					close(ack)
					continue
				}
				if remaining := time.Until(deadline); remaining <= 0 {
					cause := &protocol.ProviderPhaseTimeoutError{Phase: protocol.ProviderTimeoutSemanticIdle, Timeout: timeout, Cause: context.DeadlineExceeded}
					cancel(cause)
					ack <- cause
					return
				}
				stopAndDrainTimer(timer)
				timer.Reset(timeout)
				timerC = timer.C
				deadline = time.Now().Add(timeout)
				ack <- nil
			case <-timerC:
				if remaining := time.Until(deadline); remaining > 0 {
					timer.Reset(remaining)
					timerC = timer.C
					continue
				}
				cause := &protocol.ProviderPhaseTimeoutError{Phase: protocol.ProviderTimeoutSemanticIdle, Timeout: timeout, Cause: context.DeadlineExceeded}
				cancel(cause)
				return
			}
		}
	}()

	hooks := protocol.ProviderHooks{
		ResponseHeadRecorded: func() {
			ack := make(chan struct{})
			select {
			case responseHeads <- ack:
			case <-ctx.Done():
				return
			}
			select {
			case <-ack:
			case <-ctx.Done():
			}
		},
		Progress: func(progressCtx context.Context) error {
			if cause := context.Cause(ctx); cause != nil {
				return cause
			}
			ack := make(chan error, 1)
			select {
			case semanticProgress <- ack:
			case <-ctx.Done():
				return context.Cause(ctx)
			}
			select {
			case err := <-ack:
				if err != nil {
					return err
				}
			case <-ctx.Done():
				return context.Cause(ctx)
			}
			if progress != nil {
				if err := progress(progressCtx); err != nil {
					if progressCtx.Err() != nil && (errors.Is(err, progressCtx.Err()) || errors.Is(err, context.Canceled) && errors.Is(progressCtx.Err(), context.Canceled)) {
						return context.Cause(progressCtx)
					}
					return &protocol.ProviderProgressError{Cause: err}
				}
			}
			return nil
		},
	}
	stop := func() {
		cancel(context.Canceled)
		<-done
	}
	return ctx, hooks, stop
}

func stopAndDrainTimer(timer *time.Timer) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
}

// WithProviderResponseHeaderTimeout wraps a client with a per-request budget to obtain response
// headers, including request transmission. It preserves the underlying transport and any
// service-SVID TLS clone.
func WithProviderResponseHeaderTimeout(client *http.Client, timeout time.Duration) *http.Client {
	if timeout <= 0 {
		return client
	}
	base := client.Transport
	if base == nil {
		base = http.DefaultTransport
	}
	copy := *client
	copy.Transport = providerResponseHeaderTimeoutTransport{base: base, timeout: timeout}
	return &copy
}

type providerResponseHeaderTimeoutTransport struct {
	base    http.RoundTripper
	timeout time.Duration
}

func (t providerResponseHeaderTimeoutTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	ctx, cancel := context.WithCancelCause(request.Context())
	deadline := time.Now().Add(t.timeout)

	var mu sync.Mutex
	timerDone := make(chan struct{})
	var timeoutErr *protocol.ProviderPhaseTimeoutError
	finished := false
	timer := time.AfterFunc(t.timeout, func() {
		mu.Lock()
		if !finished && ctx.Err() == nil {
			timeoutErr = &protocol.ProviderPhaseTimeoutError{Phase: protocol.ProviderTimeoutResponseHeader, Timeout: t.timeout, Cause: context.DeadlineExceeded}
		}
		cause := timeoutErr
		mu.Unlock()
		if cause != nil {
			cancel(cause)
		}
		close(timerDone)
	})

	request = request.WithContext(ctx)
	bodyCloseDone := make(chan struct{})
	stopCloseBody := context.AfterFunc(ctx, func() {
		if request.Body != nil {
			_ = request.Body.Close()
		}
		close(bodyCloseDone)
	})
	response, err := t.base.RoundTrip(request)
	if !stopCloseBody() {
		<-bodyCloseDone
	}
	mu.Lock()
	finished = true
	if timeoutErr == nil && ctx.Err() == nil && time.Until(deadline) <= 0 {
		timeoutErr = &protocol.ProviderPhaseTimeoutError{Phase: protocol.ProviderTimeoutResponseHeader, Timeout: t.timeout, Cause: context.DeadlineExceeded}
	}
	stopped := timer.Stop()
	timedOut := timeoutErr
	mu.Unlock()
	if !stopped {
		<-timerDone
	}
	if timedOut != nil {
		if response != nil {
			_ = response.Body.Close()
		}
		cancel(timedOut)
		return nil, timedOut
	}
	if err != nil || response == nil || response.Body == nil {
		cancel(err)
	} else {
		response.Body = cancelOnCloseReadCloser{ReadCloser: response.Body, cancel: func() { cancel(nil) }}
	}
	return response, err
}

type cancelOnCloseReadCloser struct {
	io.ReadCloser
	cancel func()
}

func (r cancelOnCloseReadCloser) Close() error {
	err := r.ReadCloser.Close()
	r.cancel()
	return err
}

func (t providerResponseHeaderTimeoutTransport) CloseIdleConnections() {
	if closer, ok := t.base.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
}
