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
)

type subprocessBackend struct {
	config               ProcessConfig
	stateOwnerFD         *os.File
	supervisorExecutable string
	cmd                  *exec.Cmd
	liveness             *os.File
	fenceConfirmation    *os.File
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
	fenceReader, fenceWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter)
		return fmt.Errorf("create supervisor fence confirmation pipe: %w", err)
	}

	extraFiles := []*os.File{livenessReader, reportWriter, fenceWriter}
	args := []string{SupervisorMode, "--parent-liveness-fd", "3", "--native-pid-report-fd", "4", "--fence-confirmation-fd", "5", "--stop-grace", b.config.StopGrace.String()}
	if b.stateOwnerFD != nil {
		extraFiles = append(extraFiles, b.stateOwnerFD)
		args = append(args, "--state-owner-fd", "6")
	}
	args = append(args, "--")
	args = append(args, b.config.Command...)
	executable := b.supervisorExecutable
	if executable == "" {
		executable, err = os.Executable()
		if err != nil {
			closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter, fenceReader, fenceWriter)
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
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter, stderrReader, stderrWriter, livenessReader, livenessWriter, reportReader, reportWriter, fenceReader, fenceWriter)
		return fmt.Errorf("start harness supervisor: %w", err)
	}
	_ = stdinReader.Close()
	_ = stdoutWriter.Close()
	_ = stderrWriter.Close()
	_ = livenessReader.Close()
	_ = reportWriter.Close()
	_ = fenceWriter.Close()

	b.cmd = cmd
	b.fenceConfirmation = fenceReader
	launched = true
	b.liveness = livenessWriter
	p.attachStreams(stdinWriter, stdoutReader, stderrReader)
	go b.reapSupervisor(p)

	pid, err := readPIDReport(ctx, reportReader)
	_ = reportReader.Close()
	if err != nil {
		cleanupErr := b.killSupervisor(p)
		if cleanupErr == nil {
			_, cleanupErr = p.Wait(context.Background())
		}
		// Preserve the supervisor's startup diagnostic without waiting forever
		// for an inherited stderr writer that survived a failed PID report.
		p.drainFailedStartStderr()
		return errors.Join(fmt.Errorf("harness supervisor did not report a native PID: %w", err), cleanupErr)
	}
	p.setNativePID(pid)
	return nil
}

func (b *subprocessBackend) stop(ctx context.Context, p *Process) error {
	if err := p.CloseStdin(); err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	b.signalSupervisor(syscall.SIGTERM)
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-ctx.Done():
		b.escalate()
		return ctx.Err()
	}
}

func (b *subprocessBackend) reapSupervisor(p *Process) {
	waitErr := b.cmd.Wait()
	waitOwnership.Lock()
	waitOwnership.subprocess--
	waitOwnership.Unlock()
	_ = p.CloseStdin()
	// The container supervisor retains ownership until process-group cleanup completes.
	if b.liveness != nil {
		_ = b.liveness.Close()
	}
	fenceConfirmed, fenceErr := readFenceConfirmation(b.fenceConfirmation)
	_ = b.fenceConfirmation.Close()
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
	if fenceErr != nil {
		waitErr = fmt.Errorf("read native process-group fence confirmation: %w", fenceErr)
	} else if !fenceConfirmed {
		waitErr = errors.New("harness supervisor exited without confirming native process-group cleanup")
	} else {
		// ExitError is the expected representation of a nonzero native exit status;
		// the explicit report proves cleanup completed before the supervisor exited.
		var exitError *exec.ExitError
		if errors.As(waitErr, &exitError) {
			waitErr = nil
		}
	}
	p.completeWith(code, waitErr)
}

const fenceConfirmationByte = 'F'

func readFenceConfirmation(file *os.File) (bool, error) {
	if file == nil {
		return false, errors.New("fence confirmation pipe is unavailable")
	}
	data, err := io.ReadAll(io.LimitReader(file, 2))
	if err != nil {
		return false, err
	}
	return len(data) == 1 && data[0] == fenceConfirmationByte, nil
}

func (b *subprocessBackend) killSupervisor(p *Process) error {
	// EOF asks the supervisor to fence its process group. Do not signal it during
	// startup: its Go runtime may not have installed signal handling yet.
	if b.liveness != nil {
		_ = b.liveness.Close()
	}
	timer := time.NewTimer(b.config.StopGrace + time.Second)
	defer timer.Stop()
	select {
	case <-p.waitDone:
		return nil
	case <-timer.C:
	}
	b.escalate()
	// Cleanup confirmation is bounded. A timeout remains an explicit failed
	// fence; the supervisor's pipe cannot turn signal delivery into success.
	timer.Reset(nativeGroupFenceTimeout)
	select {
	case <-p.waitDone:
		return nil
	case <-timer.C:
		return fmt.Errorf("harness supervisor did not complete cleanup within %s after force-stop", nativeGroupFenceTimeout)
	}
}

func (b *subprocessBackend) signalSupervisor(signal syscall.Signal) {
	if b.cmd != nil && b.cmd.Process != nil {
		_ = b.cmd.Process.Signal(signal)
	}
}

func (b *subprocessBackend) escalate() {
	// The supervisor owns the numeric native PGID. It keeps the leader unreaped
	// while signaling, so a force request must go through it rather than risk a
	// stale PGID that has already been reused.
	b.signalSupervisor(syscall.SIGUSR1)
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
