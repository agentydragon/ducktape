package runner

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"github.com/containerd/cgroups/v3/cgroup2"
	"golang.org/x/sys/unix"
)

const (
	execMode                 = "--agentplane-runner-exec"
	workspaceMode            = "--agentplane-runner-workspace"
	vmResourcePropertiesMode = "--vm-resource-properties"

	parentLivenessFDDefault = 3
	nativePIDFDDefault      = 4
	stopGrace               = 5 * time.Second
)

// InternalMode dispatches the private child modes before the runner starts its service.
// args excludes argv[0].
func InternalMode(args []string) (handled bool, exitCode int) {
	if len(args) == 0 {
		return false, 0
	}
	switch args[0] {
	case codexExecServerContainerMode:
		return true, CodexExecServerContainerMain(args[1:])
	case codexExecServerContainerHealthMode:
		return true, CodexExecServerContainerHealthMain(args[1:])
	case supervisorMode:
		return true, SupervisorMain(args[1:])
	case execMode:
		return true, ExecMain(args[1:])
	case workspaceMode:
		return true, WorkspaceMain(args[1:])
	case vmResourcePropertiesMode:
		return true, VMResourcePropertiesMain(args[1:])
	default:
		return false, 0
	}
}

// VMResourcePropertiesMain validates the shared Runner config and prints the
// systemd properties that may be applied to the Runner service.
func VMResourcePropertiesMain(args []string) int {
	if len(args) != 1 || args[0] == "" {
		fmt.Fprintln(os.Stderr, "--vm-resource-properties requires one Runner config path")
		return 64
	}
	properties, err := guestResourceSystemdProperties(args[0])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	for _, property := range properties {
		if _, err := fmt.Fprintln(os.Stdout, property); err != nil {
			return 1
		}
	}
	return 0
}

// SupervisorMain runs one native command, retaining inherited state ownership until the
// command and every cgroup member have exited. It returns an exit code for the parent main.
func SupervisorMain(args []string) int {
	flags := flag.NewFlagSet("runner supervisor", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	parentFD := flags.Int("parent-liveness-fd", parentLivenessFDDefault, "")
	reportFD := flags.Int("native-pid-fd", nativePIDFDDefault, "")
	stateFD := flags.Int("state-owner-fd", -1, "")
	cgroupDir := flags.String("cgroup-dir", "", "")
	uid := flags.Int64("agent-uid", -1, "")
	gid := flags.Int64("agent-gid", -1, "")
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
	if (*uid < 0) != (*gid < 0) || *uid > int64(^uint32(0)) || *gid > int64(^uint32(0)) {
		fmt.Fprintln(os.Stderr, "harness supervisor received an incomplete agent identity")
		return 64
	}
	if *cgroupDir != "" && *uid < 0 {
		fmt.Fprintln(os.Stderr, "harness supervisor requires an agent identity when using cgroups")
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
		// The descriptor remains open in this supervisor. A child gets a deliberate duplicate via ExtraFiles.
		unix.CloseOnExec(*stateFD)
	}
	defer liveness.Close()
	defer report.Close()

	var cgroup *supervisorCgroup
	if *cgroupDir != "" {
		opened, err := openSupervisorCgroup(*cgroupDir)
		if err != nil {
			fmt.Fprintf(os.Stderr, "harness supervisor: cannot open agent cgroup controls: %v\n", err)
			return 125
		}
		cgroup = opened
		defer cgroup.close()
	}

	// A pipe is a process-lifetime token. Unlike PR_SET_PDEATHSIG it is independent of
	// which Go runtime thread launched this process, and EOF remains pending across the
	// check/spawn race. Check once before the command can execute, then monitor EOF.
	if parentAlreadyGone(liveness) {
		return 128 + int(syscall.SIGUSR1)
	}
	parentGone := make(chan struct{}, 1)
	go watchParentPipe(liveness, parentGone)
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGUSR1)
	defer signal.Stop(signals)

	program, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: locate runner executable: %v\n", err)
		return 125
	}
	child, err := startNative(program, command, *uid, *gid, stateOwner, cgroup)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: start native harness: %v\n", err)
		return 125
	}
	if err := writePIDReport(report, child.Process.Pid); err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: report native PID: %v\n", err)
		return stopAndFence(child, cgroup)
	}
	_ = report.Close()

	code, err := waitForNative(child, parentGone, signals, cgroup)
	if err != nil {
		fmt.Fprintf(os.Stderr, "harness supervisor: wait for native harness: %v\n", err)
		return 125
	}
	return code
}

// ExecMain runs after the kernel has applied the requested UID/GID. It clears every
// capability set before replacing itself with the native command, preserving its PID,
// process group, cgroup membership, inherited owner descriptor, and stdio.
func ExecMain(args []string) int {
	command := args
	if len(command) > 0 && command[0] == "--" {
		command = command[1:]
	}
	if len(command) == 0 || command[0] == "" {
		fmt.Fprintln(os.Stderr, "runner exec trampoline requires a command")
		return 64
	}
	runtime.LockOSThread()
	if err := clearCapabilities(); err != nil {
		fmt.Fprintf(os.Stderr, "runner exec trampoline: clear capabilities: %v\n", err)
		return 125
	}
	if err := unix.Exec(command[0], command, os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "runner exec trampoline: exec %s: %v\n", command[0], err)
		return 125
	}
	return 125
}

type supervisorCgroup struct {
	manager *cgroup2.Manager
	dir     *os.File
}

func openSupervisorCgroup(path string) (*supervisorCgroup, error) {
	dir, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	kill, err := os.OpenFile(filepath.Join(path, "cgroup.kill"), os.O_WRONLY, 0)
	if err != nil {
		_ = dir.Close()
		return nil, err
	}
	_ = kill.Close()
	events, err := os.Open(filepath.Join(path, "cgroup.events"))
	if err != nil {
		_ = dir.Close()
		return nil, err
	}
	_ = events.Close()
	manager, err := loadCgroup(path)
	if err != nil {
		_ = dir.Close()
		return nil, err
	}
	return &supervisorCgroup{manager: manager, dir: dir}, nil
}

func (c *supervisorCgroup) close() {
	if c == nil {
		return
	}
	_ = c.dir.Close()
}

func startNative(executable string, command []string, uid, gid int64, stateOwner *os.File, cgroup *supervisorCgroup) (*exec.Cmd, error) {
	program := command[0]
	args := command[1:]
	if uid >= 0 {
		program = executable
		args = append([]string{execMode, "--"}, command...)
	}
	child := exec.Command(program, args...)
	child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
	if stateOwner != nil {
		child.ExtraFiles = []*os.File{stateOwner}
	}
	proc := &syscall.SysProcAttr{Setpgid: true}
	if uid >= 0 {
		proc.Credential = &syscall.Credential{Uid: uint32(uid), Gid: uint32(gid), Groups: []uint32{uint32(gid)}}
	}
	if cgroup != nil {
		proc.UseCgroupFD = true
		proc.CgroupFD = int(cgroup.dir.Fd())
	}
	child.SysProcAttr = proc
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

func waitForNative(child *exec.Cmd, parentGone <-chan struct{}, signals <-chan os.Signal, cgroup *supervisorCgroup) (int, error) {
	done := make(chan error, 1)
	go func() { done <- child.Wait() }()
	for {
		select {
		case err := <-done:
			if fenceErr := fenceNativeGroup(child.Process.Pid, cgroup); fenceErr != nil {
				return 125, fenceErr
			}
			return processExitCode(child.ProcessState), expectedWaitError(err)
		case <-parentGone:
			return stopNative(child, done, cgroup)
		case <-signals:
			return stopNative(child, done, cgroup)
		}
	}
}

func stopNative(child *exec.Cmd, done <-chan error, cgroup *supervisorCgroup) (int, error) {
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGTERM)
	timer := time.NewTimer(stopGrace)
	defer timer.Stop()
	select {
	case err := <-done:
		if fenceErr := fenceNativeGroup(child.Process.Pid, cgroup); fenceErr != nil {
			return 125, fenceErr
		}
		return processExitCode(child.ProcessState), expectedWaitError(err)
	case <-timer.C:
	}
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGKILL)
	if cgroup != nil {
		if err := killCgroup(cgroup); err != nil {
			return 125, err
		}
	}
	err := <-done
	if fenceErr := fenceNativeGroup(child.Process.Pid, cgroup); fenceErr != nil {
		return 125, fenceErr
	}
	return processExitCode(child.ProcessState), expectedWaitError(err)
}

func stopAndFence(child *exec.Cmd, cgroup *supervisorCgroup) int {
	_ = signalProcessGroup(child.Process.Pid, syscall.SIGKILL)
	if cgroup != nil {
		_ = killCgroup(cgroup)
	}
	_ = child.Wait()
	_ = fenceNativeGroup(child.Process.Pid, cgroup)
	return 125
}

func fenceNativeGroup(leader int, cgroup *supervisorCgroup) error {
	_ = signalProcessGroup(leader, syscall.SIGKILL)
	if cgroup == nil {
		return nil
	}
	diagnosticsLogged := false
	for {
		if err := killCgroup(cgroup); err != nil {
			if errors.Is(err, os.ErrNotExist) {
				return nil
			}
			time.Sleep(time.Second)
			continue
		}
		if !diagnosticsLogged {
			logCgroupMemoryDiagnostics(cgroup.dir.Name())
			diagnosticsLogged = true
		}
		if err := cgroup.manager.Delete(); err == nil || errors.Is(err, os.ErrNotExist) {
			return nil
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func killCgroup(cgroup *supervisorCgroup) error {
	if cgroup == nil {
		return nil
	}
	return cgroup.manager.Kill()
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

func clearCapabilities() error {
	if err := unix.Prctl(unix.PR_CAP_AMBIENT, unix.PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0); err != nil && !errors.Is(err, unix.EINVAL) {
		return err
	}
	header := unix.CapUserHeader{Version: unix.LINUX_CAPABILITY_VERSION_3}
	data := [2]unix.CapUserData{}
	return unix.Capset(&header, &data[0])
}
