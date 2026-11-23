package supervision

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// RunCommandHelper installs command stdio inside a transient unit and execs
// the target. Keeping the stream descriptors out of systemd lets EOF follow
// the processes that actually hold each pipe, even if descendants stay alive.
func RunCommandHelper(args []string) int {
	if len(args) != 1 || !filepath.IsAbs(args[0]) {
		return 125
	}
	conn, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: args[0], Net: "unix"})
	if err != nil {
		return 125
	}
	defer conn.Close()
	credentials, err := commandPeerCredentials(conn)
	if err != nil || credentials.Uid != 0 {
		return 125
	}
	_ = conn.SetDeadline(time.Now().Add(commandStartupTimeout))
	config, pidfd, err := prepareCommandHelper(conn)
	replyErr := writeCommandReady(conn, pidfd, err)
	if pidfd != nil {
		_ = pidfd.Close()
	}
	if err != nil || replyErr != nil {
		return 125
	}
	// The socket is close-on-exec. If exec returns, report the failure instead.
	err = syscall.Exec(config.Path, config.Argv, config.Environment)
	_ = writeCommandReply(conn, commandReply{Error: fmt.Sprintf("exec command: %v", err)})
	return 125
}

func prepareCommandHelper(conn *net.UnixConn) (CommandConfig, *os.File, error) {
	config, files, err := readCommandStart(conn)
	if err != nil {
		return CommandConfig{}, nil, err
	}
	defer files.close()
	for target, file := range []*os.File{files.Stdin, files.Stdout, files.Stderr} {
		if file != nil {
			if err := unix.Dup3(int(file.Fd()), target, 0); err != nil {
				return CommandConfig{}, nil, fmt.Errorf("install command fd %d: %w", target, err)
			}
		}
	}
	if err := prepareCommandGroup(config.TTY); err != nil {
		return CommandConfig{}, nil, err
	}
	fd, err := unix.PidfdOpen(os.Getpid(), 0)
	if err != nil {
		return CommandConfig{}, nil, fmt.Errorf("open command pidfd: %w", err)
	}
	pidfd := os.NewFile(uintptr(fd), "command-pidfd")
	if err := signalCommandGroup(pidfd, 0); err != nil {
		_ = pidfd.Close()
		return CommandConfig{}, nil, fmt.Errorf("signal command group via pidfd: %w", err)
	}
	return config, pidfd, nil
}

func prepareCommandGroup(tty bool) error {
	pid := os.Getpid()
	if tty {
		sid, err := unix.Getsid(0)
		if err != nil {
			return err
		}
		if sid != pid {
			if _, err := unix.Setsid(); err != nil {
				return err
			}
		}
		if err := unix.IoctlSetInt(unix.Stdin, unix.TIOCSCTTY, 0); err != nil {
			return fmt.Errorf("acquire command controlling terminal: %w", err)
		}
		return nil
	}
	pgid, err := unix.Getpgid(0)
	if err != nil {
		return err
	}
	if pgid != pid {
		return unix.Setpgid(0, 0)
	}
	return nil
}
