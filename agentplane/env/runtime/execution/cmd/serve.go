package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexshim"
	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedbackend"
	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// limitsFile contains optional overrides; omitted fields use the defaults.
type limitsFile struct {
	Native native.Limits    `json:"native"`
	Codex  codexshim.Limits `json:"codex"`
}

func readLimits(path string) (limitsFile, error) {
	limits := limitsFile{Native: native.DefaultLimits(), Codex: codexshim.DefaultLimits()}
	if path != "" {
		if !filepath.IsAbs(path) {
			return limitsFile{}, errors.New("limits file path must be absolute")
		}
		f, err := os.Open(path)
		if err != nil {
			return limitsFile{}, err
		}
		defer f.Close()
		decoder := json.NewDecoder(io.LimitReader(f, 1<<20))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&limits); err != nil {
			return limitsFile{}, err
		}
		if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
			if err == nil {
				return limitsFile{}, errors.New("limits file must contain one JSON object")
			}
			return limitsFile{}, err
		}
	}
	if err := limits.Native.Validate(); err != nil {
		return limitsFile{}, err
	}
	if err := limits.Codex.Validate(); err != nil {
		return limitsFile{}, err
	}
	return limits, nil
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
	limitsPath := flags.String("limits-file", "", "absolute path to execution limits JSON")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 {
		return 64
	}
	if err := codexexecserver.ValidateURL(*ws); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	limits, err := readLimits(*limitsPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "execution limits:", err)
		return 64
	}
	core, err := native.NewWithLimits(limits.Native)
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
