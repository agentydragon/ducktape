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
	// MaxFrameLineBytes matches the native transport's maximum newline-delimited frame.
	MaxFrameLineBytes = 64 * 1024 * 1024
	ReadBatchBytes    = 64 * 1024
	stderrTailBytes   = 4096
)

var (
	errCommandRequired        = errors.New("native harness command is required")
	errSupervisorUnconfigured = errors.New("native process supervisor is not configured")
)

// Process owns a native execution domain's shared streams and lifecycle state.
// Platform-specific handles live only in backend.
type Process struct {
	backend processBackend

	mu          sync.Mutex
	writeMu     sync.Mutex
	started     bool
	active      bool
	stdin       *os.File
	stdout      io.Reader
	stderr      *tailReader
	nativePID   int
	waitDone    chan struct{}
	waitCode    int
	waitErr     error
	stdinClosed bool
	complete    sync.Once
}

// Start waits for native execution to begin. The context bounds startup; it
// is not attached to the native process lifetime.
func (p *Process) Start(ctx context.Context) error {
	p.mu.Lock()
	if p.started {
		p.mu.Unlock()
		return errors.New("native harness process was already started")
	}
	p.started = true
	p.mu.Unlock()
	if ctx == nil {
		ctx = context.Background()
	}
	err := p.backend.start(ctx, p)
	if err != nil {
		p.cleanupFailedStart(err)
	}
	return err
}

// Running reports whether native execution and domain cleanup remain incomplete.
func (p *Process) Running() bool {
	p.mu.Lock()
	started, active := p.started, p.active
	p.mu.Unlock()
	if !started || !active {
		return false
	}
	select {
	case <-p.waitDone:
		return false
	default:
		return true
	}
}

// PID returns the native process ID after Start has completed successfully.
func (p *Process) PID() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.nativePID
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
	if ctx == nil {
		ctx = context.Background()
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
	stdin := p.stdin
	if stdin == nil {
		p.mu.Unlock()
		return errors.New("native harness process was not started")
	}
	wasClosed := p.stdinClosed
	p.stdinClosed = true
	p.mu.Unlock()

	if wasClosed {
		return nil
	}
	return stdin.Close()
}

// Wait waits for native exit and fencing; cancellation only stops this observation.
func (p *Process) Wait(ctx context.Context) (int, error) {
	p.mu.Lock()
	started := p.started
	p.mu.Unlock()
	if !started {
		return 0, errors.New("native harness process was not started")
	}
	if ctx == nil {
		ctx = context.Background()
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
func (p *Process) Stop(ctx context.Context, grace time.Duration) error {
	if !p.Running() {
		_, err := p.Wait(ctx)
		return err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return p.backend.stop(ctx, p, grace)
}

// ExitDescription reports native status and the captured stderr tail.
func (p *Process) ExitDescription() string {
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

// BackendDescription reports backend lifecycle evidence without exposing argv or environment values.
func (p *Process) BackendDescription() string {
	p.mu.Lock()
	active := p.active
	p.mu.Unlock()
	if !active {
		return p.ExitDescription()
	}
	return p.backend.description(p)
}

func (p *Process) attachStreams(stdin *os.File, stdout io.Reader, stderr io.Reader) {
	p.mu.Lock()
	p.stdin = stdin
	p.stdout = stdout
	p.stderr = &tailReader{reader: stderr}
	p.active = true
	p.mu.Unlock()
}

func (p *Process) setNativePID(pid int) {
	p.mu.Lock()
	p.nativePID = pid
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

// ValidateFrameLine is useful to callers that implement their own batching policy.
func ValidateFrameLine(line []byte) error {
	if len(line) > MaxFrameLineBytes {
		return fmt.Errorf("a harness stdout line exceeds %d bytes", MaxFrameLineBytes)
	}
	return nil
}
