// Package scripts runs one-shot shell scripts through an injected process launcher.
package scripts

import (
	"context"
	"errors"
	"io"
	"maps"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"golang.org/x/sync/errgroup"
)

type ProcessLauncher func(context.Context, string, supervision.ProcessConfig) (*supervision.ManagedProcess, error)

type Config struct {
	HarnessEnvironment map[string]string
	LaunchProcess      ProcessLauncher
}

type Runner struct {
	harnessEnvironment map[string]string
	launchProcess      ProcessLauncher
}

func New(config Config) (*Runner, error) {
	if config.LaunchProcess == nil {
		return nil, errors.New("script process launcher is required")
	}

	return &Runner{
		harnessEnvironment: maps.Clone(config.HarnessEnvironment),
		launchProcess:      config.LaunchProcess,
	}, nil
}

func (r *Runner) Run(ctx context.Context, owner, script, cwd string, output func(bool, []byte) error) (int, error) {
	env := map[string]string{}
	for _, entry := range os.Environ() {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			env[key] = value
		}
	}
	for key, value := range r.harnessEnvironment {
		env[key] = value
	}
	process, err := r.launchProcess(ctx, owner, supervision.ProcessConfig{Command: []string{"/bin/sh", "-eu"}, Dir: cwd, Env: env})
	if err != nil {
		return 0, err
	}
	var readers errgroup.Group
	for _, stream := range []struct {
		reader io.Reader
		stdout bool
	}{{process.Stdout(), true}, {process.Stderr(), false}} {
		readers.Go(func() error {
			buffer := make([]byte, 64*1024)
			for {
				n, err := stream.reader.Read(buffer)
				if n > 0 {
					if e := output(stream.stdout, append([]byte(nil), buffer[:n]...)); e != nil {
						return errors.Join(e, process.Stop(context.Background()))
					}
				}
				if err == io.EOF {
					return nil
				}
				if err != nil {
					return errors.Join(err, process.Stop(context.Background()))
				}
			}
		})
	}
	writeErr := process.Write(ctx, []byte(script))
	if errors.Is(writeErr, os.ErrClosed) || errors.Is(writeErr, io.ErrClosedPipe) {
		writeErr = nil
	}
	closeErr := process.CloseStdin()
	code, waitErr := process.Wait(ctx)
	if waitErr != nil {
		waitErr = errors.Join(waitErr, process.Stop(context.WithoutCancel(ctx)))
	}
	return code, errors.Join(writeErr, closeErr, waitErr, readers.Wait())
}
