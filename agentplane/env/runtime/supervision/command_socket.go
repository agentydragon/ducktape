package supervision

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const (
	commandStartupTimeout  = 30 * time.Second
	commandMessageTimeout  = 5 * time.Second
	commandStartMaxBytes   = 1 << 20
	commandControlMaxBytes = 256
)

type commandStart struct {
	Config   CommandConfig
	HasStdin bool
}

type commandControl struct {
	Release bool
	Stop    bool
}

type commandReply struct {
	Exit     *CommandExit
	Fenced   bool
	Released bool
	Error    string
}

// ListenCommandSocket binds a socket in an existing root-owned directory.
// The socket accepts a connection from any UID so Runtime can retain its
// restricted capabilities. The server authenticates the peer PID before I/O.
func ListenCommandSocket(path string) (*net.UnixListener, error) {
	if !filepath.IsAbs(path) {
		return nil, errors.New("command socket path must be absolute")
	}
	info, err := os.Lstat(filepath.Dir(path))
	if err != nil {
		return nil, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.IsDir() || !ok || stat.Uid != 0 || info.Mode().Perm()&0022 != 0 {
		return nil, errors.New("command socket directory must be owned and writable only by root")
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0666); err != nil {
		_ = listener.Close()
		return nil, err
	}
	return listener, nil
}

// ServeCommands owns accepted connections and their transient units until
// every unit has been fenced. Each connection owns exactly one command.
func ServeCommands(ctx context.Context, listener *net.UnixListener, supervisor *Supervisor, helperPath string, executorPID func() int) (serveErr error) {
	ctx, cancel := context.WithCancel(ctx)
	stopListener := context.AfterFunc(ctx, func() { _ = listener.Close() })
	var active sync.WaitGroup
	var failOnce sync.Once
	var commandErr error
	defer func() {
		cancel()
		_ = listener.Close()
		stopListener()
		active.Wait()
		serveErr = errors.Join(serveErr, commandErr)
	}()
	if executorPID == nil {
		return errors.New("command service has no executor PID")
	}
	expectedPID := executorPID()
	if expectedPID <= 0 {
		return errors.New("command service has no executor PID")
	}
	pidfd, err := unix.PidfdOpen(expectedPID, 0)
	if err != nil {
		return fmt.Errorf("pin command executor process identity: %w", err)
	}
	defer unix.Close(pidfd)
	poll := []unix.PollFd{{Fd: int32(pidfd), Events: unix.POLLIN}}
	for {
		conn, err := listener.AcceptUnix()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		pid, err := commandPeerPID(conn)
		if err != nil || pid != expectedPID {
			_ = conn.Close()
			continue
		}
		if exited, err := unix.Poll(poll, 0); err != nil || exited != 0 {
			_ = conn.Close()
			continue
		}
		active.Go(func() {
			if err := serveCommand(ctx, conn, supervisor, helperPath); err != nil {
				failOnce.Do(func() {
					commandErr = err
					cancel()
				})
			}
		})
	}
}

func commandPeerPID(conn *net.UnixConn) (int, error) {
	credentials, err := commandPeerCredentials(conn)
	if err != nil {
		return 0, err
	}
	return int(credentials.Pid), nil
}

func commandPeerCredentials(conn *net.UnixConn) (*unix.Ucred, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return nil, err
	}
	var credentials *unix.Ucred
	var credErr error
	if err := raw.Control(func(fd uintptr) {
		credentials, credErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	}); err != nil {
		return nil, err
	}
	if credErr != nil {
		return nil, credErr
	}
	return credentials, nil
}

func serveCommand(ownerCtx context.Context, conn *net.UnixConn, supervisor *Supervisor, helperPath string) (stopErr error) {
	ctx, cancel := context.WithCancel(ownerCtx)
	stopConn := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer func() {
		cancel()
		stopConn()
		_ = conn.Close()
	}()

	_ = conn.SetReadDeadline(time.Now().Add(commandStartupTimeout))
	config, files, err := readCommandStart(conn)
	if err != nil {
		_ = writeCommandReady(conn, nil, err)
		return
	}
	_ = conn.SetReadDeadline(time.Time{})
	requests := make(chan commandControl)
	go readCommandControls(ctx, cancel, conn, requests)

	startCtx, stopStart := context.WithTimeout(ctx, commandStartupTimeout)
	command, err := supervisor.StartCommand(startCtx, helperPath, config, files)
	stopStart()
	if err != nil {
		_ = writeCommandReady(conn, nil, err)
		if errors.Is(err, errCommandFence) {
			return err
		}
		return nil
	}
	stop := sync.OnceValue(func() error { return stopOwnedCommand(command.Stop) })
	defer func() { stopErr = stop() }()
	if err := writeCommandReady(conn, command.pidfd, nil); err != nil {
		return
	}
	_ = command.pidfd.Close()
	exitResults := make(chan commandReply, 1)
	go func() {
		exit, err := command.Wait(ctx)
		reply := commandReply{Exit: &exit}
		if err != nil {
			reply.Error = err.Error()
		}
		exitResults <- reply
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case exit := <-exitResults:
			if err := writeCommandReply(conn, exit); err != nil {
				return
			}
			exitResults = nil
		case <-command.Done():
			reply := commandReply{Fenced: true}
			if exitResults != nil {
				reply = <-exitResults
				reply.Fenced = true
			}
			_ = writeCommandReply(conn, reply)
			return
		case request := <-requests:
			if request.Stop {
				if err := stop(); err != nil {
					return err
				}
				continue
			}
			if exitResults != nil {
				// Release is valid only after the client has observed leader exit.
				return
			}
			_ = writeCommandReply(conn, commandReply{Released: true})
			cancel()
			_ = conn.Close()
			select {
			case <-command.Done():
			case <-ownerCtx.Done():
			}
			return
		}
	}
}

func readCommandControls(ctx context.Context, cancel context.CancelFunc, conn *net.UnixConn, requests chan<- commandControl) {
	defer cancel()
	for {
		var request commandControl
		if err := readCommandFrame(conn, &request, commandControlMaxBytes); err != nil {
			return
		}
		if request.Stop == request.Release {
			return
		}
		select {
		case requests <- request:
		case <-ctx.Done():
			return
		}
	}
}

func readCommandRights(conn *net.UnixConn) ([]*os.File, error) {
	var marker [1]byte
	rights := make([]byte, unix.CmsgSpace(3*4))
	n, rightsLen, flags, _, err := conn.ReadMsgUnix(marker[:], rights)
	if err != nil {
		return nil, err
	}
	messages, err := unix.ParseSocketControlMessage(rights[:rightsLen])
	var files []*os.File
	for _, msg := range messages {
		fds, rightsErr := unix.ParseUnixRights(&msg)
		if rightsErr != nil {
			err = errors.Join(err, rightsErr)
		}
		for _, fd := range fds {
			files = append(files, os.NewFile(uintptr(fd), "command-descriptor"))
		}
	}
	if err == nil && (n != 1 || marker[0] != 1 || flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0) {
		err = errors.New("invalid command descriptor frame")
	}
	return files, err
}

func writeCommandRights(conn *net.UnixConn, files ...*os.File) error {
	var rights []byte
	if len(files) > 0 {
		fds := make([]int, len(files))
		for i, file := range files {
			fds[i] = int(file.Fd())
		}
		rights = unix.UnixRights(fds...)
	}
	_, _, err := conn.WriteMsgUnix([]byte{1}, rights, nil)
	runtime.KeepAlive(files)
	return err
}

func readCommandStart(conn *net.UnixConn) (CommandConfig, CommandFiles, error) {
	files, err := readCommandRights(conn)
	keep := false
	defer func() {
		if !keep {
			closeFiles(files...)
		}
	}()
	if err != nil {
		return CommandConfig{}, CommandFiles{}, err
	}
	var request commandStart
	if err := readCommandFrame(conn, &request, commandStartMaxBytes); err != nil {
		return CommandConfig{}, CommandFiles{}, err
	}
	expected := 2
	if request.HasStdin {
		expected++
	}
	if len(files) != expected {
		return CommandConfig{}, CommandFiles{}, errors.New("wrong command descriptor count")
	}
	keep = true
	result := CommandFiles{Stdout: files[0], Stderr: files[1]}
	if request.HasStdin {
		result.Stdin = files[2]
	}
	return request.Config, result, nil
}

func writeCommandStart(conn *net.UnixConn, config CommandConfig, files CommandFiles) error {
	attached := []*os.File{files.Stdout, files.Stderr}
	if files.Stdin != nil {
		attached = append(attached, files.Stdin)
	}
	if err := writeCommandRights(conn, attached...); err != nil {
		return err
	}
	return writeCommandFrame(conn, commandStart{Config: config, HasStdin: files.Stdin != nil}, commandStartMaxBytes)
}

func readCommandReady(conn *net.UnixConn) (*os.File, error) {
	files, err := readCommandRights(conn)
	if err == nil {
		var reply commandReply
		err = readCommandFrame(conn, &reply, commandStartMaxBytes)
		if err == nil {
			err = reply.error()
		}
		if err == nil && len(files) == 1 {
			return files[0], nil
		}
		if err == nil {
			err = errors.New("command readiness reply is missing its process group pidfd")
		}
	}
	closeFiles(files...)
	return nil, err
}

func writeCommandReady(conn *net.UnixConn, pidfd *os.File, startErr error) error {
	_ = conn.SetWriteDeadline(time.Now().Add(commandMessageTimeout))
	var files []*os.File
	if pidfd != nil {
		files = append(files, pidfd)
	}
	if err := writeCommandRights(conn, files...); err != nil {
		return err
	}
	reply := commandReply{}
	if startErr != nil {
		reply.Error = startErr.Error()
	}
	return writeCommandReply(conn, reply)
}

func writeCommandReply(conn *net.UnixConn, reply commandReply) error {
	_ = conn.SetWriteDeadline(time.Now().Add(commandMessageTimeout))
	return writeCommandFrame(conn, reply, commandStartMaxBytes)
}

func writeCommandFrame(w io.Writer, value any, limit int) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if len(data) > limit {
		return errors.New("command message exceeds size limit")
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(data)))
	if _, err := w.Write(header[:]); err != nil {
		return err
	}
	_, err = w.Write(data)
	return err
}

func readCommandFrame(r io.Reader, value any, limit int) error {
	var header [4]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return err
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > uint32(limit) {
		return errors.New("invalid command message size")
	}
	data := make([]byte, int(size))
	if _, err := io.ReadFull(r, data); err != nil {
		return err
	}
	return json.Unmarshal(data, value)
}

// RemoteCommand is the executor's lease on one Runtime-owned command unit.
type RemoteCommand struct {
	conn      *net.UnixConn
	done      chan struct{}
	exitDone  chan struct{}
	pidfd     *os.File
	controlMu sync.Mutex
	exit      CommandExit
	exitErr   error
	fenceErr  error
}

// StartRemoteCommand borrows the files for the launch request. Runtime owns
// the received copies. Closing the connection during startup fences the unit.
func StartRemoteCommand(ctx context.Context, socketPath string, config CommandConfig, files CommandFiles) (*RemoteCommand, error) {
	if err := files.validate(config.TTY); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, commandStartupTimeout)
	defer cancel()
	dialer := net.Dialer{}
	raw, err := dialer.DialContext(ctx, "unix", socketPath)
	if err != nil {
		return nil, err
	}
	conn := raw.(*net.UnixConn)
	credentials, err := commandPeerCredentials(conn)
	if err != nil || credentials.Uid != 0 {
		_ = conn.Close()
		return nil, errors.New("command supervisor peer must be root")
	}
	stopConn := context.AfterFunc(ctx, func() { _ = conn.Close() })
	err = writeCommandStart(conn, config, files)
	var pidfd *os.File
	if err == nil {
		pidfd, err = readCommandReady(conn)
	}
	if !stopConn() && err == nil {
		err = ctx.Err()
	}
	if err != nil {
		if pidfd != nil {
			_ = pidfd.Close()
		}
		_ = conn.Close()
		return nil, fmt.Errorf("start systemd command: %w", err)
	}
	command := &RemoteCommand{conn: conn, done: make(chan struct{}), exitDone: make(chan struct{}), pidfd: pidfd}
	go command.receive()
	return command, nil
}

func (r commandReply) error() error {
	if r.Error == "" {
		return nil
	}
	return errors.New(r.Error)
}

func (c *RemoteCommand) receive() {
	defer close(c.done)
	defer c.conn.Close()
	defer c.pidfd.Close()
	exitObserved := false
	defer func() {
		if !exitObserved {
			c.exitErr = c.fenceErr
			close(c.exitDone)
		}
	}()
	for {
		var reply commandReply
		if err := readCommandFrame(c.conn, &reply, commandStartMaxBytes); err != nil {
			c.fenceErr = fmt.Errorf("command ownership connection lost: %w", err)
			return
		}
		if reply.Exit != nil && !exitObserved {
			c.exit, c.exitErr = *reply.Exit, reply.error()
			exitObserved = true
			close(c.exitDone)
		}
		if reply.Fenced || reply.Released {
			c.fenceErr = reply.error()
			return
		}
		if reply.Exit == nil {
			c.fenceErr = errors.New("unexpected command reply")
			return
		}
	}
}

// Done closes when Runtime acknowledges release or fencing, or the ownership connection fails.
func (c *RemoteCommand) Done() <-chan struct{} { return c.done }

// Wait reports leader exit. The connection retains ownership of descendants
// until Stop fences the execution domain.
func (c *RemoteCommand) Wait(ctx context.Context) (CommandExit, error) {
	select {
	case <-c.exitDone:
		return c.exit, c.exitErr
	default:
	}
	select {
	case <-c.exitDone:
		return c.exit, c.exitErr
	case <-ctx.Done():
		return CommandExit{}, ctx.Err()
	}
}

func (c *RemoteCommand) waitFenced(ctx context.Context) error {
	select {
	case <-c.done:
		return c.fenceErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (c *RemoteCommand) writeControl(ctx context.Context, control commandControl) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	deadline := time.Now().Add(commandMessageTimeout)
	if ctxDeadline, ok := ctx.Deadline(); ok && ctxDeadline.Before(deadline) {
		deadline = ctxDeadline
	}
	_ = c.conn.SetWriteDeadline(deadline)
	err := writeCommandFrame(c.conn, control, commandControlMaxBytes)
	if errors.Is(err, syscall.EPIPE) || errors.Is(err, syscall.ECONNRESET) || errors.Is(err, net.ErrClosed) {
		return c.waitFenced(ctx)
	}
	if err != nil {
		_ = c.conn.Close()
	}
	return err
}

// Signal targets the original process group even after systemd reaps its
// leader. A detached setsid child belongs to a different group.
func (c *RemoteCommand) Signal(ctx context.Context, signal syscall.Signal) error {
	if signal < 1 || signal > 64 {
		return errors.New("invalid command signal")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case <-c.done:
		return c.fenceErr
	default:
	}
	err := signalCommandGroup(c.pidfd, signal)
	if errors.Is(err, os.ErrClosed) {
		return c.waitFenced(ctx)
	}
	return err
}

func signalCommandGroup(pidfd *os.File, signal syscall.Signal) error {
	raw, err := pidfd.SyscallConn()
	if err != nil {
		return err
	}
	var signalErr error
	if err = raw.Control(func(fd uintptr) {
		signalErr = unix.PidfdSendSignal(int(fd), signal, nil, unix.PIDFD_SIGNAL_PROCESS_GROUP)
	}); err != nil {
		return err
	}
	if errors.Is(signalErr, syscall.ESRCH) {
		return nil
	}
	return signalErr
}

// Release kills the original process group, then transfers any detached
// descendants to Runtime until they exit or Runtime shuts down.
func (c *RemoteCommand) Release(ctx context.Context) error {
	if err := c.Signal(ctx, syscall.SIGKILL); err != nil {
		return err
	}
	if _, err := c.Wait(ctx); err != nil {
		return err
	}
	return c.finish(ctx, commandControl{Release: true})
}

func (c *RemoteCommand) Stop(ctx context.Context) error {
	return c.finish(ctx, commandControl{Stop: true})
}

func (c *RemoteCommand) finish(ctx context.Context, control commandControl) error {
	c.controlMu.Lock()
	select {
	case <-c.done:
		c.controlMu.Unlock()
		return c.fenceErr
	default:
	}
	err := c.writeControl(ctx, control)
	c.controlMu.Unlock()
	if err != nil {
		return err
	}
	return c.waitFenced(ctx)
}
