// Package pid1 owns the native child mechanics used when Environment Runtime
// is PID 1 in a private container PID namespace.
package pid1

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// Launch describes one child. Command and Env are copied before Start uses
// them. File handles are borrowed through the exec handshake and remain owned
// by the caller.
type Launch struct {
	Command []string
	Dir     string
	Env     []string
	Stdin   *os.File
	Stdout  *os.File
	Stderr  *os.File
}

// Exit is the immutable result of reaping the child leader. FenceError records
// a failure to kill its process group before consuming the leader's status.
type Exit struct {
	Code       int
	FenceError error
}

// Namespace returns the process-wide reaper for a private PID namespace.
// PID 1 owns all wait4 calls, including those for adopted children.
func Namespace() (*Reaper, error) {
	if os.Getpid() != 1 {
		return nil, errors.New("Pod PID1 supervision requires Environment Runtime to be PID 1 in a private container PID namespace")
	}
	return processReaper(), nil
}

var processReaper = sync.OnceValue(func() *Reaper {
	reaper := &Reaper{children: map[int]*Child{}}
	go reaper.run()
	return reaper
})

// Reaper serializes native starts, registration and waits so orphan reaping
// cannot consume a managed child's status.
type Reaper struct {
	mu       sync.Mutex
	children map[int]*Child
}

// Child is a registered native process with a stable PID and process-group ID.
type Child struct {
	reaper *Reaper
	cmd    *exec.Cmd
	pid    int

	reaped    chan struct{}
	exit      Exit
	fenced    chan struct{}
	fenceOnce sync.Once
}

// Start starts and registers a child while holding the same lock used by every
// wait4 call. The child PID remains stable even after its exec handle is
// released.
func (r *Reaper) Start(launch Launch) (*Child, error) {
	command := slices.Clone(launch.Command)
	environment := slices.Clone(launch.Env)
	cmd := exec.Command(command[0], command[1:]...)
	cmd.Dir, cmd.Env = launch.Dir, environment
	cmd.Stdin, cmd.Stdout, cmd.Stderr = launch.Stdin, launch.Stdout, launch.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	r.mu.Lock()
	err := cmd.Start()
	var child *Child
	if err == nil {
		child = &Child{
			reaper: r,
			cmd:    cmd,
			pid:    cmd.Process.Pid,
			reaped: make(chan struct{}),
			fenced: make(chan struct{}),
		}
		r.children[child.pid] = child
	}
	r.mu.Unlock()
	if err != nil {
		return nil, fmt.Errorf("start PID1 native process: %w", err)
	}
	return child, nil
}

// PID returns the stable process ID and process-group ID for this child.
func (c *Child) PID() int { return c.pid }

// Terminate requests graceful termination while this exact child remains
// registered. The reaper reports failures from its final group fence.
func (c *Child) Terminate() { c.signal(syscall.SIGTERM) }

// Kill requests forced termination while this exact child remains registered.
func (c *Child) Kill() { c.signal(syscall.SIGKILL) }

func (c *Child) signal(signal syscall.Signal) {
	c.reaper.mu.Lock()
	defer c.reaper.mu.Unlock()
	if c.reaper.children[c.pid] == c {
		_ = signalProcessGroup(c.pid, signal)
	}
}

// WaitReaped returns the replayable leader exit result. A canceled observation
// never cancels the process-lifetime reaper or releases child ownership.
func (c *Child) WaitReaped(ctx context.Context) (Exit, error) {
	if err := ctx.Err(); err != nil {
		return Exit{}, err
	}
	select {
	case <-c.reaped:
		return c.exit, nil
	case <-ctx.Done():
		return Exit{}, ctx.Err()
	}
}

// WaitFenced waits for leader reaping and process-group disappearance.
// Cancellation stops this observation only; a later call can continue waiting.
func (c *Child) WaitFenced(ctx context.Context) error {
	exit, err := c.WaitReaped(ctx)
	if err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if exit.FenceError != nil {
		return exit.FenceError
	}
	select {
	case <-c.fenced:
		return nil
	default:
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-c.fenced:
			return nil
		default:
		}
		err := unix.Kill(-c.pid, 0)
		if errors.Is(err, unix.ESRCH) {
			c.fenceOnce.Do(func() { close(c.fenced) })
			return nil
		}
		if err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

func (c *Child) publishReaped(exit Exit) {
	// Publishing the immutable result before closing reaped makes every waiter's
	// read happen after this write.
	c.exit = exit
	close(c.reaped)
}

func exitCode(status unix.WaitStatus) int {
	code := status.ExitStatus()
	if status.Signaled() {
		code = 128 + int(status.Signal())
	}
	return code
}

func (r *Reaper) run() {
	changed := make(chan os.Signal, 1)
	signal.Notify(changed, syscall.SIGCHLD)
	// A coalesced SIGCHLD or one delivered before Notify must not strand a zombie.
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		for pid, child := range r.children {
			var info unix.Siginfo
			err := unix.Waitid(unix.P_PID, pid, &info, unix.WEXITED|unix.WNOHANG|unix.WNOWAIT, nil)
			if errors.Is(err, unix.EINTR) {
				continue
			}
			if err != nil {
				panic(fmt.Errorf("observe PID1 native child %d: %w", pid, err))
			}
			if info.Signo == 0 {
				continue
			}
			// WNOWAIT keeps the zombie leader's PID reserved until its group is
			// fenced. Native forks cannot reuse the group identity in this gap.
			fenceErr := signalProcessGroup(pid, syscall.SIGKILL)
			var status unix.WaitStatus
			for {
				_, err = unix.Wait4(pid, &status, 0, nil)
				if !errors.Is(err, unix.EINTR) {
					break
				}
			}
			if err != nil {
				panic(fmt.Errorf("reap PID1 native child %d: %w", pid, err))
			}
			delete(r.children, pid)
			_ = child.cmd.Process.Release()
			child.publishReaped(Exit{Code: exitCode(status), FenceError: fenceErr})
		}
		// Only unregistered adopted children may go through orphan wait4.
		// Every native Start shares this mutex with discovery and registration.
		children, err := directChildren()
		if err != nil {
			panic(fmt.Errorf("discover PID1 adopted children: %w", err))
		}
		for _, pid := range children {
			if r.children[pid] != nil {
				continue
			}
			var status unix.WaitStatus
			_, err := unix.Wait4(pid, &status, unix.WNOHANG, nil)
			if err != nil && !errors.Is(err, unix.ECHILD) && !errors.Is(err, unix.EINTR) {
				panic(fmt.Errorf("reap PID1 adopted child %d: %w", pid, err))
			}
		}
		r.mu.Unlock()
		select {
		case <-changed:
		case <-ticker.C:
		}
	}
}

// directChildren reads every runtime thread because Go can start a child from
// any thread and /proc/self/task/<leader>/children does not show all of them.
func directChildren() ([]int, error) {
	tasks, err := os.ReadDir("/proc/self/task")
	if err != nil {
		return nil, err
	}
	var children []int
	for _, task := range tasks {
		data, err := os.ReadFile(filepath.Join("/proc/self/task", task.Name(), "children"))
		if errors.Is(err, os.ErrNotExist) {
			continue
		} // A runtime thread exited.
		if err != nil {
			return nil, err
		}
		for _, field := range strings.Fields(string(data)) {
			pid, err := strconv.Atoi(field)
			if err != nil || pid <= 0 {
				return nil, fmt.Errorf("invalid child PID %q", field)
			}
			children = append(children, pid)
		}
	}
	return children, nil
}

// FenceNamespace kills and joins every same-UID workload process except PID 1.
func (r *Reaper) FenceNamespace(ctx context.Context) error {
	if os.Getpid() != 1 {
		return errors.New("PID namespace fencing requires the caller to be PID 1")
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		managed := len(r.children)
		r.mu.Unlock()
		if managed != 0 {
			return fmt.Errorf("PID namespace fence started with %d managed child processes", managed)
		}

		if _, err := namespaceWorkloadPIDs(os.Getuid()); err != nil {
			return err
		}
		if err := unix.Kill(-1, unix.SIGKILL); err != nil && !errors.Is(err, unix.ESRCH) {
			return fmt.Errorf("kill PID namespace workload: %w", err)
		}
		remaining, err := namespaceWorkloadPIDs(os.Getuid())
		if err != nil {
			return err
		}
		if len(remaining) == 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("wait for PID namespace workload fence: %w", ctx.Err())
		case <-ticker.C:
		}
	}
}

// namespaceWorkloadPIDs fails closed if a workload process has credentials
// PID 1 cannot signal. Container workloads inherit PID 1's identity and cannot
// gain privilege because the companion runs without capabilities or privilege
// escalation.
func namespaceWorkloadPIDs(uid int) ([]int, error) {
	return namespaceWorkloadPIDsAt("/proc", uid, os.ReadFile)
}

func namespaceWorkloadPIDsAt(procDir string, uid int, readStatus func(string) ([]byte, error)) ([]int, error) {
	entries, err := os.ReadDir(procDir)
	if err != nil {
		return nil, fmt.Errorf("read PID namespace process table: %w", err)
	}
	var pids []int
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid == 1 {
			continue
		}
		status, err := readStatus(filepath.Join(procDir, entry.Name(), "status"))
		// procfs can return ESRCH as well as ENOENT when a process exits
		// between listing /proc and opening its status file.
		if errors.Is(err, os.ErrNotExist) || errors.Is(err, unix.ESRCH) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("read PID namespace process %d credentials: %w", pid, err)
		}
		credentials, err := processUIDs(string(status))
		if err != nil {
			return nil, fmt.Errorf("read PID namespace process %d credentials: %w", pid, err)
		}
		if !slices.Equal(credentials, []int{uid, uid, uid, uid}) {
			return nil, fmt.Errorf("PID namespace process %d does not share PID 1's unprivileged UID", pid)
		}
		pids = append(pids, pid)
	}
	return pids, nil
}

func processUIDs(status string) ([]int, error) {
	for _, line := range strings.Split(status, "\n") {
		if !strings.HasPrefix(line, "Uid:") {
			continue
		}
		fields := strings.Fields(strings.TrimPrefix(line, "Uid:"))
		if len(fields) != 4 {
			return nil, errors.New("process status has an invalid Uid record")
		}
		uids := make([]int, 0, len(fields))
		for _, field := range fields {
			uid, err := strconv.Atoi(field)
			if err != nil || uid < 0 {
				return nil, errors.New("process status has an invalid Uid record")
			}
			uids = append(uids, uid)
		}
		return uids, nil
	}
	return nil, errors.New("process status has no Uid record")
}

func signalProcessGroup(leader int, signal syscall.Signal) error {
	err := unix.Kill(-leader, unix.Signal(signal))
	if err == unix.ESRCH {
		return nil
	}
	return err
}
