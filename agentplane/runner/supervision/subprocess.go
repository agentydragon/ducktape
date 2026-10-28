package supervision

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

type subprocessBackend struct {
	config               ProcessConfig
	stateOwnerFD         *os.File
	supervisorExecutable string
	cmd                  *exec.Cmd
	liveness             *os.File
}

func (b *subprocessBackend) validate() error { return nil }

func (b *subprocessBackend) start(ctx context.Context, p *Process) error {
	waitOwnership.Lock()
	if waitOwnership.pid1 {
		waitOwnership.Unlock()
		return errors.New("PID1 reaper owns child waits; subprocess supervision cannot be mixed with it")
	}
	waitOwnership.subprocess++
	waitOwnership.Unlock()
	launched := false
	defer func() {
		if !launched {
			waitOwnership.Lock()
			waitOwnership.subprocess--
			waitOwnership.Unlock()
		}
	}()
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		return fmt.Errorf("create native stdin pipe: %w", err)
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		return fmt.Errorf("create native stdout pipe: %w", err)
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		return fmt.Errorf("create native stderr pipe: %w", err)
	}
	livenessReader, livenessWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter)
		return fmt.Errorf("create supervisor liveness pipe: %w", err)
	}
	reportReader, reportWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter)
		return fmt.Errorf("create supervisor PID report pipe: %w", err)
	}

	extraFiles := []*os.File{livenessReader, reportWriter}
	args := []string{SupervisorMode, "--parent-liveness-fd", "3", "--native-pid-fd", "4"}
	if b.stateOwnerFD != nil {
		extraFiles = append(extraFiles, b.stateOwnerFD)
		args = append(args, "--state-owner-fd", "5")
	}
	args = append(args, "--")
	args = append(args, b.config.Command...)
	executable := b.supervisorExecutable
	if executable == "" {
		executable, err = os.Executable()
		if err != nil {
			closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter)
			return fmt.Errorf("locate runner executable: %w", err)
		}
	}
	cmd := exec.Command(executable, args...)
	cmd.Dir = b.config.Dir
	cmd.Env = environment(b.config.Env)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdinReader, stdoutWriter, stderrWriter
	cmd.ExtraFiles = extraFiles
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter)
		return fmt.Errorf("start harness supervisor: %w", err)
	}
	_ = stdinReader.Close()
	_ = stdoutWriter.Close()
	_ = stderrWriter.Close()
	_ = livenessReader.Close()
	_ = reportWriter.Close()

	b.cmd = cmd
	launched = true
	b.liveness = livenessWriter
	p.attachStreams(stdinWriter, stdoutReader, stderrReader)
	go b.reapSupervisor(p)

	pid, err := readPIDReport(ctx, reportReader)
	_ = reportReader.Close()
	if err != nil {
		b.killSupervisor(p)
		_, waitErr := p.Wait(context.Background())
		_, _ = io.Copy(io.Discard, p.Stderr())
		return errors.Join(fmt.Errorf("harness supervisor did not report a native PID: %w", err), waitErr)
	}
	p.setNativePID(pid)
	return nil
}

func (b *subprocessBackend) stop(ctx context.Context, p *Process, grace time.Duration) error {
	if err := p.CloseStdin(); err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	b.signalSupervisor(syscall.SIGTERM)
	if p.PID() != 0 {
		_ = unix.Kill(-p.PID(), unix.SIGTERM)
	}
	if grace < 0 {
		grace = 0
	}
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-ctx.Done():
		b.escalate(p)
		return ctx.Err()
	case <-timer.C:
		b.escalate(p)
	}
	_, err := p.Wait(ctx)
	return err
}

func (b *subprocessBackend) description(p *Process) string { return p.ExitDescription() }

func (b *subprocessBackend) reapSupervisor(p *Process) {
	err := b.cmd.Wait()
	waitOwnership.Lock()
	waitOwnership.subprocess--
	waitOwnership.Unlock()
	_ = p.CloseStdin()
	// The container supervisor retains ownership until process-group cleanup completes.
	if b.liveness != nil {
		_ = b.liveness.Close()
	}
	code := 0
	if b.cmd.ProcessState != nil {
		code = b.cmd.ProcessState.ExitCode()
		if code < 0 {
			if status, ok := b.cmd.ProcessState.Sys().(syscall.WaitStatus); ok && status.Signaled() {
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
	p.completeWith(code, err)
}

func (b *subprocessBackend) killSupervisor(p *Process) {
	// EOF asks the supervisor to fence its process group. Do not signal it during
	// startup: its Go runtime may not have installed signal handling yet.
	if b.liveness != nil {
		_ = b.liveness.Close()
	}
	timer := time.NewTimer(stopGrace + time.Second)
	defer timer.Stop()
	select {
	case <-p.waitDone:
		return
	case <-timer.C:
	}
	b.escalate(p)
	// Keep waiting until the supervisor has reaped and fenced the native process
	// group; returning early could release state ownership while children live.
	<-p.waitDone
}

func (b *subprocessBackend) signalSupervisor(signal syscall.Signal) {
	if b.cmd != nil && b.cmd.Process != nil {
		_ = unix.Kill(-b.cmd.Process.Pid, unix.Signal(signal))
	}
}

func (b *subprocessBackend) escalate(p *Process) {
	if pid := p.PID(); pid != 0 {
		_ = unix.Kill(-pid, unix.SIGKILL)
	}
	if b.cmd != nil && b.cmd.Process != nil {
		// Leave the supervisor alive to reap the native leader while it holds state ownership.
		_ = unix.Kill(-b.cmd.Process.Pid, unix.SIGTERM)
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
