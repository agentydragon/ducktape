package supervision

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"time"

	systemdops "github.com/agentydragon/ducktape/agentplane/env/runtime/supervision/internal/systemd"
	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/google/uuid"
)

const (
	commandFenceTimeout = 90 * time.Second
	// PID 1 needs time to consume SIGCHLD before leaving its OOM stop state.
	commandStopGrace = 2 * time.Second
)

var errCommandFence = errors.New("command unit fence failed")

// CommandConfig describes one command in the guest's shared command slice.
// Path names the executable independently of the argv[0] passed to it.
type CommandConfig struct {
	Path        string
	Argv        []string
	Dir         string
	Environment []string
	TTY         bool
}

// CommandFiles contains the child-side standard streams. Nil Stdin selects /dev/null.
type CommandFiles struct {
	Stdin, Stdout, Stderr *os.File
}

func (f CommandFiles) close() { closeFiles(f.Stdin, f.Stdout, f.Stderr) }

func (f CommandFiles) validate(tty bool) error {
	if f.Stdout == nil || f.Stderr == nil || (tty && f.Stdin == nil) {
		return errors.New("missing command standard stream descriptors")
	}
	return nil
}

type CommandExit struct {
	Code      int
	OOMKilled bool
}

// Command owns one transient unit in the shared command slice.
type Command struct {
	handle *systemdops.StartHandle
	pidfd  *os.File
}

// WaitCommandScope waits until the guest has applied the aggregate limits and
// fenced stale command units from a prior Runtime invocation.
func (s *Supervisor) WaitCommandScope(ctx context.Context) error {
	if s == nil || s.commands == nil || s.commands.launchReady == nil {
		return errors.New("systemd command scope is not configured")
	}
	select {
	case <-s.commands.launchReady:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// StartCommand takes ownership of the supplied descriptors on every path.
// An unproven startup fence fails the command owner.
func (s *Supervisor) StartCommand(ctx context.Context, helperPath string, config CommandConfig, files CommandFiles) (*Command, error) {
	defer files.close()
	if s == nil || s.commands == nil {
		return nil, errors.New("systemd command scope is not configured")
	}
	if !filepath.IsAbs(helperPath) || !filepath.IsAbs(config.Path) || !filepath.IsAbs(config.Dir) || len(config.Argv) == 0 {
		return nil, errors.New("command requires absolute executable and working directory paths, and argv")
	}
	if err := files.validate(config.TTY); err != nil {
		return nil, err
	}
	launcher := s.commands
	helperSocket := filepath.Join(filepath.Dir(launcher.lifetimeMarker), "f-"+uuid.NewString()+".sock")
	listener, err := ListenCommandSocket(helperSocket)
	if err != nil {
		return nil, fmt.Errorf("listen for command helper: %w", err)
	}
	stopListener := context.AfterFunc(ctx, func() { _ = listener.Close() })
	defer func() {
		stopListener()
		_ = listener.Close()
	}()
	backend := systemdops.NewBackend(systemdops.BackendConfig{
		Command: []string{helperPath, "command-helper", helperSocket}, Dir: config.Dir, StopGrace: commandStopGrace, RetainUntilStop: true,
		Scope: launcher.scope, Slice: launcher.Slice, LifetimeMarker: launcher.lifetimeMarker, ParentUnit: launcher.parentUnit(),
		LaunchReady: func() bool { return scopeReady(launcher.launchReady) },
		Properties:  func() ([]systemd.Property, error) { return nil, nil },
		Identity:    launcher.identityProperties(), OpenConnection: openSystemdConnection,
		StartTransient: launcher.startTransient, ReapSnapshot: launcher.reapSnapshot, Property: property,
	})
	handle, err := backend.Start(ctx)
	if err != nil {
		return nil, err
	}
	var pidfd *os.File
	if err = handle.WaitStarted(ctx); err == nil {
		pidfd, err = startCommandHelper(ctx, listener, handle.PID(), config, files)
	}
	if err != nil {
		fenceErr := stopOwnedCommand(handle.Stop)
		select {
		case <-handle.Done():
			if fenceErr != nil && !errors.Is(fenceErr, err) {
				err = errors.Join(err, fenceErr)
			}
		default:
			err = errors.Join(err, errCommandFence, fenceErr)
		}
		return nil, err
	}
	return &Command{handle: handle, pidfd: pidfd}, nil
}

func startCommandHelper(ctx context.Context, listener *net.UnixListener, pid int, config CommandConfig, files CommandFiles) (*os.File, error) {
	for {
		conn, err := listener.AcceptUnix()
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return nil, err
		}
		peerPID, err := commandPeerPID(conn)
		if err != nil || peerPID != pid {
			_ = conn.Close()
			continue
		}
		return sendHelperCommand(ctx, conn, config, files)
	}
}

func sendHelperCommand(ctx context.Context, conn *net.UnixConn, config CommandConfig, files CommandFiles) (*os.File, error) {
	stopConn := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer func() {
		stopConn()
		_ = conn.Close()
	}()
	_ = conn.SetDeadline(time.Now().Add(commandStartupTimeout))
	if err := writeCommandStart(conn, config, files); err != nil {
		return nil, err
	}
	pidfd, err := readCommandReady(conn)
	if err != nil {
		return nil, fmt.Errorf("command helper preparation: %w", err)
	}
	var reply commandReply
	err = readCommandFrame(conn, &reply, commandStartMaxBytes)
	if errors.Is(err, io.EOF) && ctx.Err() == nil {
		return pidfd, nil
	}
	_ = pidfd.Close()
	if err != nil {
		return nil, fmt.Errorf("command helper exec: %w", err)
	}
	if err := reply.error(); err != nil {
		return nil, err
	}
	return nil, errors.New("unexpected reply from command helper")
}

// Done closes after the command's unit and descendants have been fenced.
func (c *Command) Done() <-chan struct{} { return c.handle.Done() }

func (c *Command) Wait(ctx context.Context) (CommandExit, error) {
	code, err := c.handle.Wait(ctx)
	return CommandExit{Code: code, OOMKilled: c.handle.OOMKilled()}, err
}

func (c *Command) Stop(ctx context.Context) error {
	_ = c.pidfd.Close()
	return c.handle.Stop(ctx)
}

func stopOwnedCommand(stop func(context.Context) error) error {
	ctx, cancel := context.WithTimeout(context.Background(), commandFenceTimeout)
	defer cancel()
	return stop(ctx)
}
