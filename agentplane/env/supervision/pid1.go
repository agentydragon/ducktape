package supervision

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

var pid1Children = sync.OnceValue(func() *childReaper {
	r := &childReaper{children: map[int]*pid1Backend{}}
	go r.run()
	return r
})

var waitOwnership struct {
	sync.Mutex
	pid1       bool
	subprocess int
}

// NewPID1Supervisor requires an outer container-runtime fence before replacement.
// PID 1 owns every wait4, including adopted orphans; exec.Cmd.Wait is never used.
func NewPID1Supervisor(stateOwnerFD *os.File) (*Supervisor, error) {
	return newPID1Supervisor(stateOwnerFD, true)
}

// NewPID1SupervisorWithoutStateOwner uses the container lifetime as the ownership fence.
// The caller must run as PID 1 in a private container PID namespace whose runtime fences the
// namespace before replacement.
func NewPID1SupervisorWithoutStateOwner() (*Supervisor, error) {
	return newPID1Supervisor(nil, false)
}

func newPID1Supervisor(stateOwnerFD *os.File, requireStateOwner bool) (*Supervisor, error) {
	if os.Getpid() != 1 {
		return nil, errors.New("Pod PID1 supervision requires Runner to be PID 1 in a private container PID namespace")
	}
	if requireStateOwner && stateOwnerFD == nil {
		return nil, errors.New("Pod PID1 supervision requires the state-owner descriptor")
	}
	waitOwnership.Lock()
	defer waitOwnership.Unlock()
	if waitOwnership.subprocess != 0 {
		return nil, errors.New("cannot select PID1 reaping while subprocess supervisors own child waits")
	}
	waitOwnership.pid1 = true
	reaper := pid1Children()
	return &Supervisor{makeBackend: func(config ProcessConfig) processBackend {
		return &pid1Backend{config: config, owner: stateOwnerFD, reaper: reaper, exited: make(chan nativeExit, 1)}
	}}, nil
}

type nativeExit struct {
	status unix.WaitStatus
	err    error
}

type childReaper struct {
	mu       sync.Mutex
	children map[int]*pid1Backend
}

func (r *childReaper) run() {
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
			child.exited <- nativeExit{status: status, err: fenceErr}
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

// Go can start a child from any runtime thread. Looking only at the thread-group
// leader's children file misses native processes started by another thread.
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

type pid1Backend struct {
	config ProcessConfig
	owner  *os.File
	reaper *childReaper
	cmd    *exec.Cmd
	exited chan nativeExit
}

func (b *pid1Backend) validate() error { return nil }

func (b *pid1Backend) start(ctx context.Context, p *Process) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		return err
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		return err
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		return err
	}
	cmd := exec.Command(b.config.Command[0], b.config.Command[1:]...)
	cmd.Dir, cmd.Env = b.config.Dir, environment(b.config.Env)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdinReader, stdoutWriter, stderrWriter
	if b.owner != nil {
		cmd.ExtraFiles = []*os.File{b.owner}
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// Start's failed-exec handshake may itself wait for the child. Registration
	// and every wait4 share this lock, so neither path steals the other's status.
	b.reaper.mu.Lock()
	err = cmd.Start()
	if err == nil {
		b.cmd = cmd
		// Process.Release sets cmd.Process.Pid=-1 as soon as the reaper runs.
		p.setNativePID(cmd.Process.Pid)
		b.reaper.children[p.PID()] = b
	}
	b.reaper.mu.Unlock()
	closeFiles(stdinReader, stdoutWriter, stderrWriter)
	if err != nil {
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return fmt.Errorf("start PID1 native process: %w", err)
	}
	p.attachStreams(stdinWriter, stdoutReader, stderrReader)
	go b.finish(p)
	if err := ctx.Err(); err != nil {
		b.signal(p, syscall.SIGKILL)
		_, waitErr := p.Wait(context.Background())
		return errors.Join(err, waitErr)
	}
	return nil
}

func (b *pid1Backend) finish(p *Process) {
	exit := <-b.exited
	_ = p.CloseStdin()
	// Adopted descendants must be reaped by the shared loop before the group is
	// empty. A leader's exit alone cannot release the process cleanup barrier.
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for exit.err == nil {
		err := unix.Kill(-p.PID(), 0)
		if errors.Is(err, unix.ESRCH) {
			break
		}
		if err != nil {
			exit.err = err
			break
		}
		<-ticker.C
	}
	code := exit.status.ExitStatus()
	if exit.status.Signaled() {
		code = 128 + int(exit.status.Signal())
	}
	p.completeWith(code, exit.err)
}

func (b *pid1Backend) signal(p *Process, signal syscall.Signal) {
	b.reaper.mu.Lock()
	defer b.reaper.mu.Unlock()
	if b.reaper.children[p.PID()] == b {
		_ = signalProcessGroup(p.PID(), signal)
	}
}

func (b *pid1Backend) stop(ctx context.Context, p *Process, grace time.Duration) error {
	if err := p.CloseStdin(); err != nil && !errors.Is(err, os.ErrClosed) {
		return err
	}
	b.signal(p, syscall.SIGTERM)
	timer := time.NewTimer(max(grace, 0))
	defer timer.Stop()
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-ctx.Done():
		b.signal(p, syscall.SIGKILL)
		return ctx.Err()
	case <-timer.C:
		b.signal(p, syscall.SIGKILL)
	}
	_, err := p.Wait(ctx)
	return err
}

func (b *pid1Backend) description(p *Process) string { return p.ExitDescription() }
