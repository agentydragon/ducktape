package runner

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const (
	// MaxFrameLineBytes matches the native transport's maximum newline-delimited frame.
	MaxFrameLineBytes = 64 * 1024 * 1024
	ReadBatchBytes    = 64 * 1024
	stderrTailBytes   = 4096

	supervisorMode = "--agentplane-runner-supervisor"
)

// ProcessConfig describes one native launch and its containment policy.
// Env is passed as the complete environment for the native command.
type ProcessConfig struct {
	Command              []string
	Dir                  string
	Env                  map[string]string
	StateOwnerFD         *os.File
	VMProcessPolicy      *VMProcessPolicy
	supervisorExecutable string
}

// Process owns a native execution domain and its streams.
type Process struct {
	config ProcessConfig

	mu          sync.Mutex
	writeMu     sync.Mutex
	started     bool
	cmd         *exec.Cmd
	stdin       *os.File
	stdout      *os.File
	stderr      *tailReader
	liveness    *os.File
	systemd     *systemdProcess
	nativePID   int
	waitDone    chan struct{}
	waitCode    int
	waitErr     error
	stdinClosed bool
}

// NewProcess validates the launch before any resources are created.
func NewProcess(config ProcessConfig) (*Process, error) {
	if len(config.Command) == 0 || config.Command[0] == "" {
		return nil, errors.New("native harness command is required")
	}
	if config.VMProcessPolicy != nil && !filepath.IsAbs(config.Command[0]) {
		return nil, errors.New("VM native executable must be absolute")
	}
	return &Process{config: config, waitDone: make(chan struct{})}, nil
}

// Start waits for native execution to begin. The context bounds startup;
// it is not attached to the native process lifetime.
func (p *Process) Start(ctx context.Context) error {
	p.mu.Lock()
	if p.started || p.cmd != nil {
		p.mu.Unlock()
		return errors.New("native harness process was already started")
	}
	p.started = true
	p.mu.Unlock()
	if ctx == nil {
		ctx = context.Background()
	}

	if p.config.VMProcessPolicy != nil {
		return p.startSystemd(ctx)
	}

	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		return p.cleanupFailedStart(fmt.Errorf("create native stdin pipe: %w", err))
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		_ = stdinReader.Close()
		_ = stdinWriter.Close()
		return p.cleanupFailedStart(fmt.Errorf("create native stdout pipe: %w", err))
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		_ = stdinReader.Close()
		_ = stdinWriter.Close()
		_ = stdoutReader.Close()
		_ = stdoutWriter.Close()
		return p.cleanupFailedStart(fmt.Errorf("create native stderr pipe: %w", err))
	}
	livenessReader, livenessWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter)
		return p.cleanupFailedStart(fmt.Errorf("create supervisor liveness pipe: %w", err))
	}
	reportReader, reportWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter)
		return p.cleanupFailedStart(fmt.Errorf("create supervisor PID report pipe: %w", err))
	}

	extraFiles := []*os.File{livenessReader, reportWriter}
	args := []string{supervisorMode, "--parent-liveness-fd", "3", "--native-pid-fd", "4"}
	if p.config.StateOwnerFD != nil {
		extraFiles = append(extraFiles, p.config.StateOwnerFD)
		args = append(args, "--state-owner-fd", "5")
	}

	args = append(args, "--")
	args = append(args, p.config.Command...)
	executable := p.config.supervisorExecutable
	if executable == "" {
		executable, err = os.Executable()
		if err != nil {
			closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter)
			return p.cleanupFailedStart(fmt.Errorf("locate runner executable: %w", err))
		}
	}
	cmd := exec.Command(executable, args...)
	cmd.Dir = p.config.Dir
	cmd.Env = environment(p.config.Env)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdinReader, stdoutWriter, stderrWriter
	cmd.ExtraFiles = extraFiles
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter)
		return p.cleanupFailedStart(fmt.Errorf("start harness supervisor: %w", err))
	}
	_ = stdinReader.Close()
	_ = stdoutWriter.Close()
	_ = stderrWriter.Close()
	_ = livenessReader.Close()
	_ = reportWriter.Close()

	p.mu.Lock()
	p.cmd = cmd
	p.stdin = stdinWriter
	p.stdout = stdoutReader
	p.stderr = &tailReader{reader: stderrReader}
	p.liveness = livenessWriter
	p.mu.Unlock()
	go p.reapSupervisor()

	pid, err := readPIDReport(ctx, reportReader)
	_ = reportReader.Close()
	if err != nil {
		p.killSupervisor()
		_, waitErr := p.Wait(context.Background())
		_, _ = io.Copy(io.Discard, p.stderr)
		return errors.Join(fmt.Errorf("harness supervisor did not report a native PID: %w", err), waitErr)
	}
	p.mu.Lock()
	p.nativePID = pid
	p.mu.Unlock()
	return nil
}

// Running reports whether native execution and its domain cleanup remain incomplete.
func (p *Process) Running() bool {
	p.mu.Lock()
	started := p.started
	cmd, backend := p.cmd, p.systemd
	select {
	case <-p.waitDone:
		p.mu.Unlock()
		return false
	default:
	}
	p.mu.Unlock()
	return started && (cmd != nil || backend != nil)
}

// PID returns the native harness PID after Start has completed successfully.
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
	if p.systemd != nil && p.Running() {
		if ctx == nil {
			ctx = context.Background()
		}
		return p.stopSystemd(ctx, grace)
	}
	if !p.Running() {
		_, err := p.Wait(ctx)
		return err
	}
	if err := p.CloseStdin(); err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	p.signalSupervisor(syscall.SIGTERM)
	if p.PID() != 0 {
		_ = unix.Kill(-p.PID(), unix.SIGTERM)
	}
	if grace < 0 {
		grace = 0
	}
	if ctx == nil {
		ctx = context.Background()
	}
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-ctx.Done():
		p.escalate()
		return ctx.Err()
	case <-timer.C:
		p.escalate()
	}
	_, err := p.Wait(ctx)
	return err
}

// ExitDescription reports the native status and the tail of stderr seen by the caller.
func (p *Process) ExitDescription() string {
	p.mu.Lock()
	code, tail, cmd := p.waitCode, []byte(nil), p.cmd
	if p.stderr != nil {
		tail = p.stderr.Tail()
	}
	p.mu.Unlock()
	if cmd == nil && p.systemd == nil {
		return "exit_code=0, stderr_tail=\"\""
	}
	select {
	case <-p.waitDone:
	default:
		code = -1
	}
	return fmt.Sprintf("exit_code=%d, stderr_tail=%q", code, string(tail))
}

func (p *Process) reapSupervisor() {
	err := p.cmd.Wait()
	p.mu.Lock()
	liveness := p.liveness
	p.mu.Unlock()
	_ = p.CloseStdin()
	// The container supervisor retains ownership until process-group cleanup completes.

	if liveness != nil {
		_ = liveness.Close()
	}
	code := 0
	if p.cmd.ProcessState != nil {
		code = p.cmd.ProcessState.ExitCode()
		if code < 0 {
			if status, ok := p.cmd.ProcessState.Sys().(syscall.WaitStatus); ok && status.Signaled() {
				code = 128 + int(status.Signal())
			} else {
				code = 1
			}
		}
	}
	// ExitError is the expected representation of a nonzero exit status; Wait returns the status separately.
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		err = nil
	}
	p.mu.Lock()
	p.waitCode = code
	p.waitErr = err
	close(p.waitDone)
	p.mu.Unlock()
}

func (p *Process) cleanupFailedStart(cause error) error {
	result := cause
	p.mu.Lock()
	if p.cmd == nil {
		p.waitCode = 125
		p.waitErr = result
		close(p.waitDone)
	}
	p.mu.Unlock()
	return result
}

func (p *Process) killSupervisor() {
	p.mu.Lock()
	liveness := p.liveness
	p.mu.Unlock()
	// EOF asks the supervisor to fence its process group. Do not signal
	// it during startup: its Go runtime may not have installed signal handling yet.
	if liveness != nil {
		_ = liveness.Close()
	}
	timer := time.NewTimer(stopGrace + time.Second)
	defer timer.Stop()
	select {
	case <-p.waitDone:
		return
	case <-timer.C:
	}
	p.escalate()
	// Keep waiting until the supervisor has reaped and fenced the native process group;
	// returning early could let the runner release state ownership while children live.
	<-p.waitDone
}

func (p *Process) signalSupervisor(signal syscall.Signal) {
	p.mu.Lock()
	cmd := p.cmd
	p.mu.Unlock()
	if cmd != nil && cmd.Process != nil {
		_ = unix.Kill(-cmd.Process.Pid, unix.Signal(signal))
	}
}

func (p *Process) escalate() {
	if pid := p.PID(); pid != 0 {
		_ = unix.Kill(-pid, unix.SIGKILL)
	}
	p.mu.Lock()
	cmd := p.cmd
	p.mu.Unlock()

	if cmd != nil && cmd.Process != nil {
		// Leave the supervisor alive to reap the native leader while it holds state ownership.
		_ = unix.Kill(-cmd.Process.Pid, unix.SIGTERM)
	}
}

func readPIDReport(ctx context.Context, report *os.File) (int, error) {
	stop := context.AfterFunc(ctx, func() { _ = report.Close() })
	defer stop()
	data, err := io.ReadAll(io.LimitReader(report, 33))
	if ctx.Err() != nil {
		return 0, ctx.Err()
	}
	if err != nil {
		return 0, fmt.Errorf("read native PID report: %w", err)
	}
	line := bytes.TrimSpace(data)
	pid, err := strconv.Atoi(string(line))
	if len(data) > 32 || err != nil || pid <= 0 {
		return 0, fmt.Errorf("invalid native PID report %q", line)
	}
	return pid, nil
}

func environment(values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, key := range keys {
		result = append(result, key+"="+values[key])
	}
	return result
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
