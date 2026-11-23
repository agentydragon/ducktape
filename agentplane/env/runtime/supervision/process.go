package supervision

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

const (
	ReadBatchBytes  = 64 * 1024
	stderrTailBytes = 4096
)

var (
	errCommandRequired        = errors.New("native harness command is required")
	errSupervisorUnconfigured = errors.New("native process supervisor is not configured")
)

// Process owns a native execution domain's shared streams and lifecycle state.
// Platform-specific handles live only in backend.
type Process struct {
	backend processBackend
	handle  processHandle

	mu          sync.Mutex
	writeMu     sync.Mutex
	started     bool
	active      bool
	stdin       *os.File
	stdout      io.Reader
	stderr      *tailReader
	waitDone    chan struct{}
	waitCode    int
	waitErr     error
	stdinClosed bool
	complete    sync.Once
}

// Start waits for native execution to begin. The context bounds startup; it
// is not attached to the native process lifetime.
func (p *Process) Start(ctx context.Context) error {
	err := p.start(ctx, nil)
	if err != nil && p.Running() {
		stopErr := p.Stop(context.Background())
		if stopErr != nil && !errors.Is(stopErr, err) && !errors.Is(err, stopErr) {
			return errors.Join(err, stopErr)
		}
	}
	return err
}

// start lets the lifecycle owner observe the returned handle before waiting
// for start confirmation. The Process retains only generic handle and stream
// state; backend-specific evidence stays with the handle.
func (p *Process) start(ctx context.Context, onHandle func(processHandle)) error {
	p.mu.Lock()
	if p.started {
		p.mu.Unlock()
		return errors.New("native harness process was already started")
	}
	p.started = true
	p.mu.Unlock()
	handle, err := p.backend.start(ctx)
	if handle == nil {
		if err == nil {
			err = errors.New("native process backend returned no handle")
		}
		p.cleanupFailedStart(err)
		return err
	}
	p.attachHandle(handle)
	if onHandle != nil {
		onHandle(handle)
	}
	if err != nil {
		return err
	}
	if starter, ok := handle.(processStarter); ok {
		return starter.WaitStarted(ctx)
	}
	return nil
}

// Running reports whether native execution and domain cleanup remain incomplete.
func (p *Process) Running() bool {
	p.mu.Lock()
	started, active, handle := p.started, p.active, p.handle
	p.mu.Unlock()
	if !started || !active {
		return false
	}
	select {
	case <-handle.Done():
		return false
	default:
		return true
	}
}

// PID returns the native process ID after Start has completed successfully.
func (p *Process) PID() int {
	p.mu.Lock()
	handle := p.handle
	p.mu.Unlock()
	if handle == nil {
		return 0
	}
	return handle.PID()
}

// Stdout returns the raw native stdout stream. The caller owns stream consumption.
func (p *Process) Stdout() io.Reader {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.stdout
}

// Stderr returns the raw native stderr stream and records its final 4 KiB for ExitDescription.
func (p *Process) Stderr() io.Reader {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.stderr
}

// Write writes bytes to native stdin, serializing concurrent callers and honoring cancellation.
func (p *Process) Write(ctx context.Context, data []byte) error {
	p.mu.Lock()
	stdin := p.stdin
	closed := p.stdinClosed
	p.mu.Unlock()
	if stdin == nil {
		return errors.New("native harness process was not started")
	}
	if closed {
		return errors.New("native harness stdin is closed")
	}
	// Writes are serialized; closing the pollable pipe interrupts any pending write.
	return writeProcessInput(ctx, p, stdin, data)
}

// WriteLine appends exactly one LF byte to line and writes it atomically with respect to other callers.
func (p *Process) WriteLine(ctx context.Context, line string) error {
	data := make([]byte, len(line)+1)
	copy(data, line)
	data[len(line)] = '\n'
	return p.Write(ctx, data)
}

// CloseStdin signals end-of-session to the native harness.
func (p *Process) CloseStdin() error {
	p.mu.Lock()
	handle := p.handle
	if handle == nil || p.stdin == nil {
		p.mu.Unlock()
		return errors.New("native harness process was not started")
	}
	wasClosed := p.stdinClosed
	p.stdinClosed = true
	p.mu.Unlock()

	if wasClosed {
		return nil
	}
	return handle.CloseStdin()
}

// Wait returns the native exit result when the backend observes it. Stop joins
// any remaining backend cleanup and fencing; cancellation only stops this wait.
func (p *Process) Wait(ctx context.Context) (int, error) {
	p.mu.Lock()
	started, handle := p.started, p.handle
	p.mu.Unlock()
	if !started {
		return 0, errors.New("native harness process was not started")
	}
	if handle != nil {
		code, err := handle.Wait(ctx)
		select {
		case <-handle.Done():
			// The handle is replayable. Re-read its terminal result in case the
			// caller context won a simultaneous select with completion.
			code, err = handle.Wait(context.Background())
			p.completeWith(code, err)
		default:
			return code, err
		}
	}
	select {
	case <-p.waitDone:
		p.mu.Lock()
		defer p.mu.Unlock()
		return p.waitCode, p.waitErr
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

// Stop closes stdin, requests graceful shutdown, then fences the execution domain.
// The grace period is fixed by ProcessConfig at launch so all backends use the
// same stop contract and systemd can apply its deadline when the unit is created.
func (p *Process) Stop(ctx context.Context) error {
	p.mu.Lock()
	started, handle := p.started, p.handle
	p.mu.Unlock()
	if !started {
		return errors.New("native harness process was not started")
	}
	if handle == nil {
		_, err := p.Wait(ctx)
		return err
	}
	err := handle.Stop(ctx)
	select {
	case <-handle.Done():
		_, waitErr := p.Wait(context.Background())
		if err == nil {
			return waitErr
		}
		if errors.Is(err, waitErr) || errors.Is(waitErr, err) {
			return err
		}
		return errors.Join(err, waitErr)
	default:
		return err
	}
}

// ExitDescription reports native status and the captured stderr tail.
func (p *Process) ExitDescription() string {
	p.mu.Lock()
	handle := p.handle
	p.mu.Unlock()
	if handle != nil {
		select {
		case <-handle.Done():
			_, _ = p.Wait(context.Background())
		default:
		}
	}
	p.mu.Lock()
	if !p.active {
		p.mu.Unlock()
		return `exit_code=0, stderr_tail=""`
	}
	code, tail := p.waitCode, []byte(nil)
	if p.stderr != nil {
		tail = p.stderr.Tail()
	}
	p.mu.Unlock()
	select {
	case <-p.waitDone:
	default:
		code = -1
	}
	return fmt.Sprintf("exit_code=%d, stderr_tail=%q", code, string(tail))
}

func (p *Process) attachHandle(handle processHandle) {
	p.mu.Lock()
	p.handle = handle
	p.stdin = handle.Stdin()
	p.stdout = handle.Stdout()
	p.stderr = &tailReader{reader: handle.Stderr()}
	p.active = true
	p.mu.Unlock()
}

func (p *Process) cleanupFailedStart(cause error) {
	p.completeWith(125, cause)
}

func (p *Process) completeWith(code int, err error) {
	p.complete.Do(func() {
		p.mu.Lock()
		p.waitCode = code
		p.waitErr = err
		close(p.waitDone)
		p.mu.Unlock()
	})
}

func writeProcessInput(ctx context.Context, p *Process, stdin *os.File, data []byte) error {
	p.writeMu.Lock()
	defer p.writeMu.Unlock()
	p.mu.Lock()
	closed := p.stdinClosed
	p.mu.Unlock()
	if closed {
		return errors.New("native harness stdin is closed")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	deadline, _ := ctx.Deadline()
	if err := stdin.SetWriteDeadline(deadline); err != nil {
		return err
	}
	cancelled := make(chan struct{})
	stop := context.AfterFunc(ctx, func() {
		_ = stdin.SetWriteDeadline(time.Now())
		close(cancelled)
	})
	defer func() {
		// A cancellation callback must finish before the next writer resets the deadline.
		if !stop() {
			<-cancelled
		}
	}()
	_, err := stdin.Write(data)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if err != nil {
		return fmt.Errorf("write native stdin: %w", err)
	}
	return nil
}

func closeFiles(files ...*os.File) {
	for _, file := range files {
		if file != nil {
			_ = file.Close()
		}
	}
}

type tailReader struct {
	mu     sync.Mutex
	reader io.Reader
	tail   []byte
}

func (r *tailReader) Read(buffer []byte) (int, error) {
	n, err := r.reader.Read(buffer)
	if n != 0 {
		r.mu.Lock()
		r.tail = append(r.tail, buffer[:n]...)
		if len(r.tail) > stderrTailBytes {
			r.tail = append([]byte(nil), r.tail[len(r.tail)-stderrTailBytes:]...)
		}
		r.mu.Unlock()
	}
	return n, err
}

func (r *tailReader) Tail() []byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]byte(nil), r.tail...)
}
