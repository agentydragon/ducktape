package supervision

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const (
	// SupervisorMode is the private Environment Runtime child mode for native process supervision.
	SupervisorMode = "--agentplane-environment-runtime-supervisor"

	parentLivenessFDDefault  = 3
	nativePIDReportFDDefault = 4
	fenceConfirmationFD      = 5
	stopGrace                = 5 * time.Second
	nativeGroupFenceTimeout  = 5 * time.Second
)

// SupervisorMain runs one native command, retaining inherited state ownership
// until the command exits and its process group has been signaled. Container
// isolation supplies the stronger whole-domain fence after runner death.
func SupervisorMain(args []string) int {
	flags := flag.NewFlagSet("runner supervisor", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	parentFD := flags.Int("parent-liveness-fd", parentLivenessFDDefault, "")
	reportFD := flags.Int("native-pid-report-fd", nativePIDReportFDDefault, "")
	fenceFD := flags.Int("fence-confirmation-fd", fenceConfirmationFD, "")
	stateFD := flags.Int("state-owner-fd", -1, "")
	configuredStopGrace := flags.Duration("stop-grace", stopGrace, "")
	if err := flags.Parse(args); err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: invalid options: %v\n", err)
		return 64
	}
	if *configuredStopGrace < 0 {
		fmt.Fprintf(os.Stderr, "harness supervisor: invalid stop grace %s\n", *configuredStopGrace)
		return 64
	}
	command := flags.Args()
	if len(command) > 0 && command[0] == "--" {
		command = command[1:]
	}
	if len(command) == 0 || command[0] == "" || *parentFD < 0 || *reportFD < 0 || *fenceFD < 0 {
		fmt.Fprintln(os.Stderr, "harness supervisor requires a command, parent liveness FD, and native PID FD")
		return 64
	}

	liveness := os.NewFile(uintptr(*parentFD), "runner-parent-liveness")
	report := os.NewFile(uintptr(*reportFD), "native-pid-report")
	fenceConfirmation := os.NewFile(uintptr(*fenceFD), "native-fence-confirmation")
	if liveness == nil || report == nil || fenceConfirmation == nil {
		fmt.Fprintln(os.Stderr, "harness supervisor received invalid file descriptors")
		return 64
	}
	unix.CloseOnExec(*parentFD)
	unix.CloseOnExec(*reportFD)
	unix.CloseOnExec(*fenceFD)
	var stateOwner *os.File
	if *stateFD >= 0 {
		stateOwner = os.NewFile(uintptr(*stateFD), "runner-state-owner")
		if stateOwner == nil {
			fmt.Fprintln(os.Stderr, "harness supervisor received an invalid state-owner descriptor")
			return 64
		}
		// The descriptor stays open in this supervisor. A child gets a deliberate duplicate via ExtraFiles.
		unix.CloseOnExec(*stateFD)
	}
	defer liveness.Close()
	defer report.Close()
	defer fenceConfirmation.Close()

	// A pipe is a process-lifetime token. Unlike PR_SET_PDEATHSIG it is independent
	// of which Go runtime thread launched this process, and EOF remains pending
	// across the check/spawn race. Check before the command can execute, then watch EOF.
	if parentAlreadyGone(liveness) {
		return 128 + int(syscall.SIGUSR1)
	}
	parentGone := make(chan struct{}, 1)
	go watchParentPipe(liveness, parentGone)
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGUSR1)
	defer signal.Stop(signals)

	child, fenceConfirmed, err := startNative(command, stateOwner)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: start native harness: %v\n", err)
		if fenceConfirmed {
			writeFenceConfirmation(fenceConfirmation)
		}
		return 125
	}
	if err := writePIDReport(report, child.Process.Pid); err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: report native PID: %v\n", err)
		code, fenceErr := stopAndFence(child)
		if fenceErr == nil {
			writeFenceConfirmation(fenceConfirmation)
		}
		return code
	}
	_ = report.Close()

	code, err := waitForNative(child, parentGone, signals, *configuredStopGrace)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: wait for native harness: %v\n", err)
		return 125
	}
	if err := writeFenceConfirmation(fenceConfirmation); err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: report native process-group fence: %v\n", err)
		return 125
	}
	return code
}

func startNative(command []string, stateOwner *os.File) (*exec.Cmd, bool, error) {
	program := command[0]
	args := command[1:]

	child := exec.Command(program, args...)
	child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
	if stateOwner != nil {
		child.ExtraFiles = []*os.File{stateOwner}
	}
	child.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := child.Start(); err != nil {
		return nil, true, err
	}
	return child, false, nil
}

func writePIDReport(report *os.File, pid int) error {
	line := []byte(strconv.Itoa(pid) + "\n")
	if len(line) > 32 {
		return errors.New("native PID report exceeds pipe atomicity bound")
	}
	n, err := report.Write(line)
	if err != nil {
		return err
	}
	if n != len(line) {
		return io.ErrShortWrite
	}
	return nil
}

func waitForNative(child *exec.Cmd, parentGone <-chan struct{}, signals <-chan os.Signal, stopGrace time.Duration) (int, error) {
	done := make(chan error, 1)
	go func() { done <- observeNativeExit(child.Process.Pid) }()
	for {
		select {
		case err := <-done:
			if err != nil {
				return 125, fmt.Errorf("observe native process exit: %w", err)
			}
			return fenceAndReapNative(child)
		case <-parentGone:
			return stopNative(child, done, signals, stopGrace, false)
		case signal := <-signals:
			return stopNative(child, done, signals, stopGrace, signal == syscall.SIGUSR1)
		}
	}
}

func stopNative(child *exec.Cmd, done <-chan error, signals <-chan os.Signal, stopGrace time.Duration, force bool) (int, error) {
	if !force {
		_ = signalProcessGroup(child.Process.Pid, syscall.SIGTERM)
		timer := time.NewTimer(stopGrace)
		defer timer.Stop()
		for {
			select {
			case err := <-done:
				if err != nil {
					return 125, fmt.Errorf("observe native process exit: %w", err)
				}
				return fenceAndReapNative(child)
			case signal := <-signals:
				if signal == syscall.SIGUSR1 {
					force = true
				}
			case <-timer.C:
				force = true
			}
			if force {
				break
			}
		}
	}
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGKILL)
	err := waitNativeExitWithTimeout(done, child.Process.Pid)
	if err != nil {
		return 125, err
	}
	return fenceAndReapNative(child)
}

func stopAndFence(child *exec.Cmd) (int, error) {
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGKILL)
	done := make(chan error, 1)
	go func() { done <- observeNativeExit(child.Process.Pid) }()
	if err := waitNativeExitWithTimeout(done, child.Process.Pid); err != nil {
		return 125, fmt.Errorf("observe native process exit after startup failure: %w", err)
	}
	return fenceAndReapNative(child)
}

func waitNativeExitWithTimeout(done <-chan error, pid int) error {
	timer := time.NewTimer(nativeGroupFenceTimeout)
	defer timer.Stop()
	select {
	case err := <-done:
		if err != nil {
			return fmt.Errorf("observe native process exit: %w", err)
		}
		return nil
	case <-timer.C:
		return fmt.Errorf("native process %d did not exit within %s after SIGKILL", pid, nativeGroupFenceTimeout)
	}
}

func observeNativeExit(pid int) error {
	for {
		var info unix.Siginfo
		err := unix.Waitid(unix.P_PID, pid, &info, unix.WEXITED|unix.WNOWAIT, nil)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return err
		}
		if info.Signo != 0 {
			return nil
		}
	}
}

func fenceAndReapNative(child *exec.Cmd) (int, error) {
	leader := child.Process.Pid
	if leader <= 1 {
		return 125, fmt.Errorf("invalid native process-group ID %d", leader)
	}
	// Observe exit with WNOWAIT first. The zombie leader remains attached to its
	// process group until Wait reaps it, so this one signal cannot hit a reused PGID.
	_ = signalProcessGroup(leader, syscall.SIGKILL)
	waitErr := child.Wait()
	if err := waitNativeGroupGone(leader); err != nil {
		return 125, err
	}
	return processExitCode(child.ProcessState), expectedWaitError(waitErr)
}

func waitNativeGroupGone(leader int) error {
	if leader <= 1 {
		return fmt.Errorf("invalid native process-group ID %d", leader)
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	timer := time.NewTimer(nativeGroupFenceTimeout)
	defer timer.Stop()
	lastProbe := "process group still exists"
	for {
		err := unix.Kill(-leader, 0)
		if errors.Is(err, unix.ESRCH) {
			return nil
		}
		// Permission and transient syscall errors do not prove the group is gone.
		// Keep the fence unconfirmed until ESRCH, but do not wedge the supervisor
		// indefinitely if another reaper owns orphaned zombie group members.
		if err != nil {
			lastProbe = err.Error()
		}
		select {
		case <-ticker.C:
		case <-timer.C:
			return fmt.Errorf("native process group %d did not disappear within %s (last probe: %s)", leader, nativeGroupFenceTimeout, lastProbe)
		}
	}
}

func writeFenceConfirmation(file *os.File) error {
	if file == nil {
		return errors.New("fence confirmation pipe is unavailable")
	}
	n, err := file.Write([]byte{fenceConfirmationByte})
	if err != nil {
		return err
	}
	if n != 1 {
		return io.ErrShortWrite
	}
	return nil
}

func parentAlreadyGone(parent *os.File) bool {
	fds := []unix.PollFd{{Fd: int32(parent.Fd()), Events: unix.POLLIN | unix.POLLHUP}}
	count, err := unix.Poll(fds, 0)
	return err != nil || count > 0 && (fds[0].Revents&unix.POLLHUP != 0 || fds[0].Revents&unix.POLLIN != 0)
}

func watchParentPipe(parent *os.File, gone chan<- struct{}) {
	var one [1]byte
	for {
		n, err := parent.Read(one[:])
		if n > 0 || err != nil {
			select {
			case gone <- struct{}{}:
			default:
			}
			return
		}
	}
}

func signalProcessGroup(leader int, signal syscall.Signal) error {
	err := unix.Kill(-leader, unix.Signal(signal))
	if err == unix.ESRCH {
		return nil
	}
	return err
}

func processExitCode(state *os.ProcessState) int {
	if state == nil {
		return 125
	}
	if code := state.ExitCode(); code >= 0 {
		return code
	}
	if status, ok := state.Sys().(syscall.WaitStatus); ok && status.Signaled() {
		return 128 + int(status.Signal())
	}
	return 125
}

func expectedWaitError(err error) error {
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return nil
	}
	return err
}
