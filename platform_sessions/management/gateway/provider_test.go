package gateway

import (
	"context"
	"errors"
	"testing"
	"time"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
	"google.golang.org/grpc"
)

func TestProviderCallTimeoutSeparatesStatusFromOperations(t *testing.T) {
	timeouts := ProviderTimeouts{Status: 5 * time.Second, Operation: 2 * time.Minute}
	for _, test := range []struct {
		method string
		want   time.Duration
	}{
		{providerpb.SessionProviderControl_GetInfo_FullMethodName, timeouts.Status},
		{providerpb.SessionProviderControl_GetSession_FullMethodName, timeouts.Status},
		{providerpb.SessionProviderControl_GetImportOperation_FullMethodName, timeouts.Status},
		{providerpb.SessionProviderControl_Probe_FullMethodName, timeouts.Operation},
		{providerpb.SessionProviderControl_ImportSession_FullMethodName, timeouts.Operation},
		{providerpb.SessionProviderControl_VerifyCandidate_FullMethodName, timeouts.Operation},
		{"/future.Method/Call", timeouts.Operation},
	} {
		if got := providerCallTimeout(test.method, timeouts); got != test.want {
			t.Errorf("timeout for %q: got %s, want %s", test.method, got, test.want)
		}
	}
}

func TestProviderStatusDeadlineCancelsDispatchedCall(t *testing.T) {
	interceptor := providerDeadlineInterceptor(ProviderTimeouts{Status: 20 * time.Millisecond, Operation: time.Second})
	calls := 0
	err := interceptor(
		context.Background(), providerpb.SessionProviderControl_GetSession_FullMethodName, nil, nil, nil,
		func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
			calls++
			if _, ok := ctx.Deadline(); !ok {
				t.Fatal("status RPC did not receive a deadline")
			}
			<-ctx.Done()
			return ctx.Err()
		},
	)
	if !errors.Is(err, context.DeadlineExceeded) || calls != 1 {
		t.Fatalf("status call: err=%v dispatches=%d", err, calls)
	}
}

func TestProviderOperationHonorsCallerCancellationWithoutRedispatch(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started := make(chan struct{})
	finished := make(chan error, 1)
	calls := 0
	interceptor := providerDeadlineInterceptor(ProviderTimeouts{Status: time.Second, Operation: time.Minute})
	go func() {
		finished <- interceptor(
			ctx, providerpb.SessionProviderControl_ImportSession_FullMethodName, nil, nil, nil,
			func(callCtx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
				calls++
				close(started)
				<-callCtx.Done()
				return callCtx.Err()
			},
		)
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("provider operation was not dispatched")
	}
	cancel()
	select {
	case err := <-finished:
		if !errors.Is(err, context.Canceled) || calls != 1 {
			t.Fatalf("operation: err=%v dispatches=%d", err, calls)
		}
	case <-time.After(time.Second):
		t.Fatal("caller cancellation did not cancel the provider operation")
	}
}
