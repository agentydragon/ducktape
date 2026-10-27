// Docker exec supervisor: stdin EOF ends this exec's ordinary process group.
package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 || args[0] == "" {
		fmt.Fprintln(os.Stderr, "docker exec supervisor requires a command")
		return 64
	}
	// Adopt ordinary descendants when their leader dies, so EOF cleanup can
	// reap them rather than leaving zombies in the session container.
	if err := unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	stdin, err := os.Open(os.DevNull)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	defer stdin.Close()
	stdoutRead, stdoutWrite, err := os.Pipe()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	defer stdoutRead.Close()
	defer stdoutWrite.Close()
	stderrRead, stderrWrite, err := os.Pipe()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	defer stderrRead.Close()
	defer stderrWrite.Close()

	// EOF remains pending across this check/start race.
	gone := make(chan struct{})
	go func() {
		_, _ = io.Copy(io.Discard, os.Stdin)
		close(gone)
	}()
	select {
	case <-gone:
		return 137
	default:
	}
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdin, stdoutWrite, stderrWrite
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		if errors.Is(err, exec.ErrNotFound) || errors.Is(err, os.ErrNotExist) {
			return 127
		}
		return 125
	}
	_ = stdoutWrite.Close()
	_ = stderrWrite.Close()
	stdoutDone := make(chan error, 1)
	stderrDone := make(chan error, 1)
	go copyOutput(os.Stdout, stdoutRead, stdoutDone)
	go copyOutput(os.Stderr, stderrRead, stderrDone)
	leaderExited := make(chan error, 1)
	go func() {
		var info unix.Siginfo
		for {
			err := unix.Waitid(unix.P_PID, cmd.Process.Pid, &info, unix.WEXITED|unix.WNOWAIT, nil)
			if errors.Is(err, unix.EINTR) {
				continue
			}
			leaderExited <- err
			return
		}
	}()

	// Keep the leader's PID reserved until every inherited output pipe closes.
	// This also prevents the group ID from being reused before an EOF signal.
	terminated := false
	var outputError error
	for stdoutDone != nil || stderrDone != nil || leaderExited != nil {
		select {
		case <-gone:
			terminated = true
		case err := <-leaderExited:
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				return 125
			}
			leaderExited = nil
		case err := <-stdoutDone:
			if err != nil {
				outputError = err
				terminated = true
			}
			stdoutDone = nil
		case err := <-stderrDone:
			if err != nil {
				outputError = err
				terminated = true
			}
			stderrDone = nil
		}
		if terminated {
			break
		}
	}
	if terminated {
		if err := unix.Kill(-cmd.Process.Pid, unix.SIGKILL); err != nil && !errors.Is(err, unix.ESRCH) {
			fmt.Fprintln(os.Stderr, err)
			return 125
		}
		_ = stdoutRead.Close()
		_ = stderrRead.Close()
		if leaderExited != nil {
			if err := <-leaderExited; err != nil {
				fmt.Fprintln(os.Stderr, err)
				return 125
			}
		}
		if err := cmd.Wait(); err != nil {
			var exit *exec.ExitError
			if !errors.As(err, &exit) {
				fmt.Fprintln(os.Stderr, err)
				return 125
			}
		}
		if err := reapGroupDescendants(cmd.Process.Pid, true); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 125
		}
		if outputError != nil {
			fmt.Fprintln(os.Stderr, outputError)
			return 125
		}
		return 137
	}
	waitError := cmd.Wait()
	if err := reapGroupDescendants(cmd.Process.Pid, false); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	if err := waitError; err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			if code := exit.ExitCode(); code >= 0 {
				return code
			}
			return 128 + int(exit.Sys().(syscall.WaitStatus).Signal())
		}
		fmt.Fprintln(os.Stderr, err)
		return 125
	}
	return 0
}

func reapGroupDescendants(pgid int, wait bool) error {
	deadline := time.Now().Add(5 * time.Second)
	for {
		var status unix.WaitStatus
		pid, err := unix.Wait4(-pgid, &status, unix.WNOHANG, nil)
		switch {
		case errors.Is(err, unix.EINTR):
			continue
		case errors.Is(err, unix.ECHILD):
			return nil
		case err != nil:
			return err
		case pid > 0:
			continue
		case !wait:
			return nil
		case time.Now().After(deadline):
			return errors.New("timed out reaping exec descendants")
		default:
			time.Sleep(10 * time.Millisecond)
		}
	}
}

func copyOutput(dst, src *os.File, done chan<- error) {
	_, err := io.Copy(dst, src)
	done <- err
}
