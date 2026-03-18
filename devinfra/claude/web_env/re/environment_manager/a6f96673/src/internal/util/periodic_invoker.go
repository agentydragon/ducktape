package util

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"
)

// Reconstructed from symbol: internal/util.PeriodicInvoker
//
// PeriodicInvoker periodically invokes a function at a fixed interval.
// It runs the function once immediately upon Start, then repeats at the
// configured interval until Stop is called or the context is cancelled.
type PeriodicInvoker struct {
	interval time.Duration
	fn       func() error
	ctx      context.Context
	cancel   context.CancelFunc
	wg       sync.WaitGroup
	mu       sync.Mutex
	running  bool
	logger   *slog.Logger
}

// Reconstructed from symbol: internal/util.NewPeriodicInvoker
//
// NewPeriodicInvoker creates a new PeriodicInvoker with the given logger,
// interval, and function. It panics if logger is nil, interval is not positive,
// or fn is nil.
func NewPeriodicInvoker(logger *slog.Logger, interval time.Duration, fn func() error) *PeriodicInvoker {
	if logger == nil {
		panic("PeriodicInvoker: logger cannot be nil")
	}
	if fn == nil {
		panic("PeriodicInvoker: function cannot be nil")
	}
	if interval <= 0 {
		panic(fmt.Sprintf("PeriodicInvoker: interval must be positive, got %v", interval))
	}

	return &PeriodicInvoker{
		interval: interval,
		fn:       fn,
		logger:   logger,
	}
}

// Reconstructed from symbol: internal/util.(*PeriodicInvoker).Start
//
// Start begins periodic invocation. It panics if the invoker is already running.
// It creates a background context with cancel, marks the invoker as running,
// and launches the run goroutine.
func (p *PeriodicInvoker) Start() {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		panic("PeriodicInvoker: attempted to start an already running invoker")
	}

	p.ctx, p.cancel = context.WithCancel(context.Background())
	p.running = true
	p.wg.Add(1)
	go p.run()
}

// Reconstructed from symbol: internal/util.(*PeriodicInvoker).Stop
//
// Stop cancels the context, marks the invoker as not running, and waits
// for the run goroutine to finish. It is a no-op if the invoker is not running.
func (p *PeriodicInvoker) Stop() {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		if p.cancel != nil {
			p.cancel()
		}
		p.running = false
		p.wg.Wait()
	}
}

// Reconstructed from symbol: internal/util.(*PeriodicInvoker).run
//
// run is the main loop goroutine. It calls fn immediately, then creates a
// ticker and enters a select loop waiting on the ticker or context cancellation.
// Errors from fn are logged at Error level.
func (p *PeriodicInvoker) run() {
	defer p.wg.Done()

	if err := p.fn(); err != nil {
		p.logger.Error("periodic function failed", "error", err, "interval", p.interval)
	}

	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			if err := p.fn(); err != nil {
				p.logger.Error("periodic function failed", "error", err, "interval", p.interval)
			}
		case <-p.ctx.Done():
			return
		}
	}
}

// Reconstructed from symbol: internal/util.(*PeriodicInvoker).IsRunning
//
// IsRunning returns whether the invoker is currently running.
func (p *PeriodicInvoker) IsRunning() bool {
	p.mu.Lock()
	defer p.mu.Unlock()

	return p.running
}
