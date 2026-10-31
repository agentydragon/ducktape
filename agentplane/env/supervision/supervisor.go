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
	// SupervisorMode is the private runner child mode for native process supervision.
	SupervisorMode = "--agentplane-runner-supervisor"

	parentLivenessFDDefault = 3
	nativePIDFDDefault      = 4
	stopGrace               = 5 * time.Second
)

// SupervisorMain runs one native command, retaining inherited state ownership
// until the command exits and its process group has been signaled. Container
// isolation supplies the stronger whole-domain fence after runner death.
func SupervisorMain(args []string) int {
	flags := flag.NewFlagSet("runner supervisor", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	parentFD := flags.Int("parent-liveness-fd", parentLivenessFDDefault, "")
	reportFD := flags.Int("native-pid-fd", nativePIDFDDefault, "")
	stateFD := flags.Int("state-owner-fd", -1, "")
	if err := flags.Parse(args); err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: invalid options: %v\n", err)
		return 64
	}
	command := flags.Args()
	if len(command) > 0 && command[0] == "--" {
		command = command[1:]
	}
	if len(command) == 0 || command[0] == "" || *parentFD < 0 || *reportFD < 0 {
		fmt.Fprintln(os.Stderr, "harness supervisor requires a command, parent liveness FD, and native PID FD")
		return 64
	}

	liveness := os.NewFile(uintptr(*parentFD), "runner-parent-liveness")
	report := os.NewFile(uintptr(*reportFD), "native-pid-report")
	if liveness == nil || report == nil {
		fmt.Fprintln(os.Stderr, "harness supervisor received invalid file descriptors")
		return 64
	}
	unix.CloseOnExec(*parentFD)
	unix.CloseOnExec(*reportFD)
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

	child, err := startNative(command, stateOwner)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: start native harness: %v\n", err)
		return 125
	}
	if err := writePIDReport(report, child.Process.Pid); err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: report native PID: %v\n", err)
		return stopAndFence(child)
	}
	_ = report.Close()

	code, err := waitForNative(child, parentGone, signals)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: wait for native harness: %v\n", err)
		return 125
	}
	return code
}

func startNative(command []string, stateOwner *os.File) (*exec.Cmd, error) {
	program := command[0]
	args := command[1:]

	child := exec.Command(program, args...)
	child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
	if stateOwner != nil {
		child.ExtraFiles = []*os.File{stateOwner}
	}
	child.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return child, child.Start()
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

func waitForNative(child *exec.Cmd, parentGone <-chan struct{}, signals <-chan os.Signal) (int, error) {
	done := make(chan error, 1)
	go func() { done <- child.Wait() }()
	for {
		select {
		case err := <-done:
			if fenceErr := fenceNativeGroup(child.Process.Pid); fenceErr != nil {
				return 125, fenceErr
			}
			return processExitCode(child.ProcessState), expectedWaitError(err)
		case <-parentGone:
			return stopNative(child, done)
		case <-signals:
			return stopNative(child, done)
		}
	}
}

func stopNative(child *exec.Cmd, done <-chan error) (int, error) {
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGTERM)
	timer := time.NewTimer(stopGrace)
	defer timer.Stop()
	select {
	case err := <-done:
		if fenceErr := fenceNativeGroup(child.Process.Pid); fenceErr != nil {
			return 125, fenceErr
		}
		return processExitCode(child.ProcessState), expectedWaitError(err)
	case <-timer.C:
	}
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGKILL)

	err := <-done
	if fenceErr := fenceNativeGroup(child.Process.Pid); fenceErr != nil {
		return 125, fenceErr
	}
	return processExitCode(child.ProcessState), expectedWaitError(err)
}

func stopAndFence(child *exec.Cmd) int {
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGKILL)
	_ = child.Wait()
	_ = fenceNativeGroup(child.Process.Pid)
	return 125
}

func fenceNativeGroup(leader int) error {
	return signalProcessGroup(leader, syscall.SIGKILL)
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
