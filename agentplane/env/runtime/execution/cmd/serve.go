package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/backend"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexshim"
	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/settings"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedbackend"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

type remoteCommand struct{ *supervision.RemoteCommand }

func (p remoteCommand) Wait(ctx context.Context) (int, error) {
	result, err := p.RemoteCommand.Wait(ctx)
	return result.Code, err
}

func remoteLauncher(socket string) native.CommandLauncher {
	return func(ctx context.Context, cmd *exec.Cmd, tty bool) (native.ManagedCommand, error) {
		stdin, _ := cmd.Stdin.(*os.File)
		stdout, _ := cmd.Stdout.(*os.File)
		stderr, _ := cmd.Stderr.(*os.File)
		command, err := supervision.StartRemoteCommand(ctx, socket, supervision.CommandConfig{
			Path: cmd.Path, Argv: cmd.Args, Dir: cmd.Dir, Environment: cmd.Environ(), TTY: tty,
		}, supervision.CommandFiles{Stdin: stdin, Stdout: stdout, Stderr: stderr})
		if err != nil {
			return nil, err
		}
		return remoteCommand{command}, nil
	}
}

// runServer hosts both APIs in one supervised execution process and session registry.
func runServer(args []string) int {
	if os.Getpid() == 1 {
		fmt.Fprintln(os.Stderr, "execution backend must not run as PID 1; its supervisor owns orphan reaping")
		return 1
	}
	flags := flag.NewFlagSet("agentplane-execution-service serve", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	ws := flags.String("listen", "", "Codex WebSocket URL")
	limitsJSON := flags.String("limits-json", "", "execution limit overrides as JSON")
	commandSocket := flags.String("command-socket", "", "Runtime-owned command launcher socket")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 {
		return 64
	}
	if err := backend.ValidateURL(*ws); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	limits, err := settings.Parse(*limitsJSON)
	if err != nil {
		fmt.Fprintln(os.Stderr, "execution limits:", err)
		return 64
	}
	var launcher native.CommandLauncher
	if *commandSocket != "" {
		launcher = remoteLauncher(*commandSocket)
	}
	core, err := native.NewWithLauncher(limits.Native, launcher)
	if err != nil {
		fmt.Fprintln(os.Stderr, "execution limits:", err)
		return 64
	}
	defer core.Close()
	endpoint, _ := url.Parse(*ws)
	listener, err := net.Listen("tcp", endpoint.Host)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer listener.Close()
	rpc := grpc.NewServer()
	pb.RegisterExecutorServer(rpc, typedbackend.New(core))
	healthService := health.NewServer()
	healthService.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(rpc, healthService)
	shim := codexshim.Handler{Server: core, Limits: limits.Codex}
	web := &http.Server{Handler: h2c.NewHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			rpc.ServeHTTP(w, r)
			return
		}
		shim.ServeHTTP(w, r)
	}), &http2.Server{})}
	done := make(chan error, 1)
	go func() { done <- web.Serve(listener) }()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	select {
	case <-ctx.Done():
	case err := <-core.Fatal():
		fmt.Fprintln(os.Stderr, "execution process ownership lost:", err)
		healthService.Shutdown()
		rpc.Stop()
		_ = web.Close()
		return 1
	case err := <-done:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, err)
			rpc.Stop()
			_ = web.Close()
			return 1
		}
	}
	healthService.Shutdown()
	rpc.Stop()
	_ = web.Close()
	return 0
}
