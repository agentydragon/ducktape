package runtime

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"time"

	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

const hostStartupTimeout = 90 * time.Second

// Run starts the private Host channel and the Gateway-facing control server. The Host
// receives the configuration paths and resolves its own ingress endpoint; Runtime owns
// only the environment lifecycle and child-service process boundaries.
func (r *Runtime) Run(ctx context.Context) error {
	listener, err := r.listenRuntimeSocket()
	if err != nil {
		return err
	}
	defer func() {
		_ = listener.Close()
		_ = removeSocket(r.config.RuntimeSocket)
	}()
	privateServer := grpc.NewServer()
	runtimepb.RegisterEnvironmentRuntimeServer(privateServer, r)
	privateServeErr := make(chan error, 1)
	go func() {
		if err := privateServer.Serve(listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			privateServeErr <- err
		}
	}()

	if err := r.startHost(ctx); err != nil {
		privateServer.Stop()
		return errors.Join(fmt.Errorf("start Harness Host: %w", err), r.Close(context.Background()))
	}
	startupCtx, cancelStartup := context.WithTimeout(ctx, hostStartupTimeout)
	err = r.waitForInitialHost(startupCtx)
	cancelStartup()
	if err != nil {
		privateServer.Stop()
		return errors.Join(fmt.Errorf("wait for initial Harness Host drain: %w", err), r.Close(context.Background()))
	}

	publicListener, publicServer, err := r.control.Listen(r.executorProxy)
	if err != nil {
		privateServer.Stop()
		return errors.Join(fmt.Errorf("listen for Gateway control: %w", err), r.Close(context.Background()))
	}
	publicServeErr := make(chan error, 1)
	go func() {
		if err := publicServer.Serve(publicListener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			publicServeErr <- err
		}
	}()

	var runErr error
	select {
	case <-ctx.Done():
	case runErr = <-r.fatal:
	case runErr = <-publicServeErr:
	case runErr = <-privateServeErr:
	}
	publicServer.Stop()
	_ = publicListener.Close()
	closeErr := r.Close(context.Background())
	privateServer.Stop()
	return errors.Join(runErr, closeErr)
}

func (r *Runtime) listenRuntimeSocket() (net.Listener, error) {
	if err := os.MkdirAll(filepath.Dir(r.config.RuntimeSocket), 0o700); err != nil {
		return nil, err
	}
	if err := removeSocket(r.config.RuntimeSocket); err != nil {
		return nil, err
	}
	listener, err := r.deps.listen(r.config.RuntimeSocket)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(r.config.RuntimeSocket, 0o600); err != nil {
		_ = listener.Close()
		_ = removeSocket(r.config.RuntimeSocket)
		return nil, err
	}
	return listener, nil
}

func removeSocket(path string) error {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("refusing to remove non-socket path %s", path)
	}
	return os.Remove(path)
}

func (r *Runtime) startHost(ctx context.Context) error {
	if r.config.Supervision == "systemd" {
		invocation, err := r.deps.startUnit(ctx, HostUnitName)
		if invocation.reference != nil || invocation.ID != "" {
			r.mu.Lock()
			r.hostUnit = invocation
			r.mu.Unlock()
		}
		if err != nil {
			return err
		}
		return nil
	}
	binary := r.config.HostBinary
	if binary == "" {
		executable, err := os.Executable()
		if err != nil {
			return fmt.Errorf("resolve Harness Host sibling binary: %w", err)
		}
		binary = filepath.Join(filepath.Dir(executable), "agentplane-harness-host")
	}
	hostMode := "subprocess"
	command := []string{
		binary, "--config", r.config.ConfigPath, "--launch-file", r.config.LaunchPath,
		"--runtime-socket", r.config.RuntimeSocket, "--listen-socket", r.config.HostSocket,
		"--supervision", hostMode,
	}
	if r.config.CodexExecServer != nil {
		command = append(command, "--execution-socket", r.config.ExecutionSocket)
	}
	process, err := r.deps.startChild(r.supervisor, supervision.ProcessConfig{Command: command, Dir: r.config.StateDir, Env: inheritedEnvironment()})
	if err != nil {
		return err
	}
	if err := process.Start(ctx); err != nil {
		return errors.Join(err, process.Stop(context.Background(), 30*time.Second))
	}
	r.mu.Lock()
	r.hostProcess = process
	r.hostExit = make(chan childExit, 1)
	r.mu.Unlock()
	for _, output := range []io.Reader{process.Stdout(), process.Stderr()} {
		if output == nil {
			continue
		}
		r.processOutputWait.Add(1)
		go func(reader io.Reader) {
			defer r.processOutputWait.Done()
			_, _ = io.Copy(os.Stderr, reader)
		}(output)
	}
	go func() {
		code, err := process.Wait(context.Background())
		exit := childExit{code: code, err: err}
		r.mu.Lock()
		exitChannel := r.hostExit
		closing := r.closing
		r.mu.Unlock()
		if exitChannel != nil {
			exitChannel <- exit
		}
		if !closing {
			if err != nil {
				r.fail(fmt.Errorf("Harness Host process exited before Runtime shutdown (status=%d): %w", code, err))
			} else {
				r.fail(fmt.Errorf("Harness Host process exited before Runtime shutdown (status=%d)", code))
			}
		}
	}()
	return nil
}

func (r *Runtime) waitForInitialHost(ctx context.Context) error {
	for {
		r.mu.Lock()
		if r.failed {
			r.mu.Unlock()
			return errors.New("Runtime failed while starting Harness Host")
		}
		if r.host != nil && r.host.connected && r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_INACTIVE &&
			r.host.report != nil && r.host.report.Ready && r.host.report.NativeStopped &&
			protoEqualBindings(r.host.report.Binding, r.intent.Binding) {
			r.mu.Unlock()
			return nil
		}
		changed := r.changes
		exitChannel := r.hostExit
		r.mu.Unlock()
		select {
		case <-changed:
		case exit := <-exitChannel:
			return fmt.Errorf("Harness Host exited before initial lifecycle acknowledgement (status=%d): %w", exit.code, exit.err)
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func protoEqualBindings(left, right *runtimepb.Binding) bool {
	return proto.Equal(left, right)
}
