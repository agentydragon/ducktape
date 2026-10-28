package stream

import (
	"context"
	"errors"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestRetryStopsOnLocalFailure(t *testing.T) {
	want := errors.New("local failure")
	calls := 0
	err := Retry(context.Background(), "test", func(context.Context) (bool, error) {
		calls++
		return true, want
	}, func(err error) bool { return errors.Is(err, want) })
	if err != want || calls != 1 {
		t.Fatalf("retry returned %v after %d attempts, want local error after one", err, calls)
	}
}

func TestRetryStopsOnCancellationDuringPermissionBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	calls := 0
	err := Retry(ctx, "test", func(context.Context) (bool, error) {
		calls++
		cancel()
		return false, status.Error(codes.PermissionDenied, "revoked")
	}, nil)
	if err != nil || calls != 1 {
		t.Fatalf("cancelled reconnect = %v after %d attempts", err, calls)
	}
}

func TestRetryReconnectsAfterRemoteError(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	calls := 0
	err := Retry(ctx, "test", func(context.Context) (bool, error) {
		calls++
		if calls == 1 {
			return false, status.Error(codes.Unavailable, "disconnected")
		}
		return true, nil
	}, nil)
	if err != nil || calls != 2 {
		t.Fatalf("reconnect = %v after %d attempts", err, calls)
	}
}
