package runtime

import (
	"context"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/execution/service"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func TestSupervisedExecutionFenceProofSurvivesCleanupRetry(t *testing.T) {
	conn, err := grpc.NewClient("passthrough:///127.0.0.1:2", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}

	managerOpen := true
	fenceCalls := 0
	handle := &executionServiceHandle{
		conn: conn,
		fence: func(context.Context) error {
			fenceCalls++
			if !managerOpen {
				return errors.New("manager connection is already closed")
			}
			managerOpen = false
			return nil
		},
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("first fence: %v", err)
	}
	if err := handle.Close(); !errors.Is(err, grpc.ErrClientConnClosing) {
		t.Fatalf("cleanup close error = %v, want grpc.ErrClientConnClosing", err)
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("retry after cleanup error: %v", err)
	}
	if fenceCalls != 1 {
		t.Fatalf("fence calls = %d, want cached proof after one successful attempt", fenceCalls)
	}
}

func TestEmbeddedExecutionBackendLossRequiresFencedResult(t *testing.T) {
	for _, test := range []struct {
		name string
		err  error
		code int
	}{
		{name: "fenced backend loss", err: service.ErrFencedBackendLoss, code: 0},
		{name: "unproven backend loss", err: errors.New("native fence failed"), code: 1},
		{name: "unrequested clean exit", code: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			handle := newExecutionServiceHandle(service.Config{}, func(context.Context, service.Config) error { return test.err })
			exit := <-handle.ProcessExit()
			if exit.code != test.code {
				t.Fatalf("exit code = %d, want %d (error: %v)", exit.code, test.code, exit.err)
			}
			if exit.code == 0 && exit.err != nil {
				t.Fatalf("fenced exit returned error: %v", exit.err)
			}
			if exit.code != 0 && exit.err == nil {
				t.Fatal("unproven exit returned no error")
			}
		})
	}
}

func TestEmbeddedExecutionStopJoinsServiceBeforeFenceProof(t *testing.T) {
	allowFence := make(chan struct{})
	handle := newExecutionServiceHandle(service.Config{}, func(ctx context.Context, _ service.Config) error {
		<-ctx.Done()
		<-allowFence
		return nil
	})
	probe, cancel := context.WithCancel(context.Background())
	cancel()
	if err := handle.StopAndFence(probe); !errors.Is(err, context.Canceled) {
		t.Fatalf("unfinished service wait = %v, want cancellation", err)
	}
	close(allowFence)
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("joined service fence = %v, want clean fence", err)
	}
}

func TestSupervisedExecutionFenceRetriesAfterUnprovenAttempt(t *testing.T) {
	fenceErr := errors.New("unit process domain remains populated")
	fenceCalls := 0
	handle := &executionServiceHandle{fence: func(context.Context) error {
		fenceCalls++
		if fenceCalls == 1 {
			return fenceErr
		}
		return nil
	}}
	if err := handle.StopAndFence(context.Background()); !errors.Is(err, fenceErr) {
		t.Fatalf("first fence = %v, want %v", err, fenceErr)
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("retry fence: %v", err)
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("cached successful fence: %v", err)
	}
	if fenceCalls != 2 {
		t.Fatalf("fence calls = %d, want failed attempt plus successful proof", fenceCalls)
	}
}
