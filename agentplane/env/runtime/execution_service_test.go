package runtime

import (
	"context"
	"errors"
	"testing"

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
	handle := &supervisedExecutionHandle{
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

func TestSupervisedExecutionFenceRetriesAfterUnprovenAttempt(t *testing.T) {
	fenceErr := errors.New("unit process domain remains populated")
	fenceCalls := 0
	handle := &supervisedExecutionHandle{fence: func(context.Context) error {
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
