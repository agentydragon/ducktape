package control

import (
	"context"
	"testing"
	"testing/synctest"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type watchStateTestStream struct {
	grpc.ServerStream
	ctx       context.Context
	responses chan *environmentruntimepb.WatchStateResponse
}

func (s *watchStateTestStream) Context() context.Context { return s.ctx }

func (s *watchStateTestStream) Send(response *environmentruntimepb.WatchStateResponse) error {
	select {
	case s.responses <- proto.Clone(response).(*environmentruntimepb.WatchStateResponse):
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}

func TestWatchStatePublishesChangesAndPeriodicHeartbeat(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		changed := make(chan struct{})
		security, err := (environmentruntimetransport.Security{
			Mode: environmentruntimetransport.Insecure,
		}).Open(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := security.Close(); err != nil {
				t.Errorf("close security: %v", err)
			}
		})
		server := New(Config{
			Security: security,
			Changes:  func() <-chan struct{} { return changed },
		}, nil)
		facts := &environmentruntimestatepb.EnvironmentRuntimeFacts{}
		stream := &watchStateTestStream{
			ctx:       ctx,
			responses: make(chan *environmentruntimepb.WatchStateResponse, 8),
		}
		done := make(chan error, 1)
		go func() {
			done <- server.forwardWatchState(ctx, server.authorizeGatewayPeer, func() (*environmentruntimepb.WatchStateResponse, error) {
				return &environmentruntimepb.WatchStateResponse{
					SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", EnvironmentRuntimeBootId: "environment-runtime-boot-id",
					ConnectionEpoch: 1, Facts: proto.Clone(facts).(*environmentruntimestatepb.EnvironmentRuntimeFacts),
				}, nil
			}, stream.Send)
		}()

		initial := <-stream.responses
		if initial.GetFacts() == nil || initial.GetIncarnationUid() != "pod-uid" {
			t.Fatalf("initial WatchState response = %v", initial)
		}
		synctest.Wait()

		// Internal notifications can be duplicated or unrelated to WatchState facts.
		changed <- struct{}{}
		changed <- struct{}{}
		synctest.Wait()
		select {
		case response := <-stream.responses:
			t.Fatalf("unchanged facts produced another WatchState response: %v", response)
		default:
		}

		facts.SetupsRunning = true
		changed <- struct{}{}
		synctest.Wait()
		updated := <-stream.responses
		if proto.Equal(initial, updated) {
			t.Fatalf("changed facts produced the same response: %v", updated)
		}

		changed <- struct{}{}
		changed <- struct{}{}
		synctest.Wait()
		select {
		case response := <-stream.responses:
			t.Fatalf("unchanged facts after a change produced another response: %v", response)
		default:
		}

		heartbeatTimer := time.NewTimer(forwardHeartbeatInterval)
		<-heartbeatTimer.C
		synctest.Wait()
		heartbeat := <-stream.responses
		if !proto.Equal(updated, heartbeat) {
			t.Fatalf("periodic heartbeat = %v, want unchanged response %v", heartbeat, updated)
		}

		cancel()
		if err := <-done; status.Code(err) != codes.Canceled {
			t.Fatalf("WatchState cancellation error = %v, want Canceled", err)
		}
	})
}

func TestPassiveWatchLifetimeDoesNotReplaceGatewayEpoch(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		security, err := (environmentruntimetransport.Security{Mode: environmentruntimetransport.Insecure}).Open(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = security.Close() })
		changed := make(chan struct{})
		// The callback models an already restored Runtime identity. A fresh control
		// server must let its first observer adopt the replacement Pod's UID.
		incarnation := "restored-incarnation"
		server := New(Config{
			Security: security, SandboxUID: "sandbox", BootID: "boot", IsClosing: func() bool { return false },
			AdoptIncarnation: func(_ context.Context, uid string) error { incarnation = uid; return nil },
			Facts: func() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
				return &environmentruntimestatepb.EnvironmentRuntimeFacts{}, nil
			},
			Changes: func() <-chan struct{} { return changed },
		}, nil)
		start := func(request *environmentruntimepb.WatchStateRequest) (context.CancelFunc, <-chan error) {
			t.Helper()
			ctx, cancel := context.WithCancel(t.Context())
			stream := &watchStateTestStream{ctx: ctx, responses: make(chan *environmentruntimepb.WatchStateResponse, 1)}
			done := make(chan error, 1)
			go func() { done <- server.WatchState(request, stream) }()
			select {
			case response := <-stream.responses:
				if response.GetConnectionEpoch() != request.GetConnectionEpoch() || response.GetEnvironmentRuntimeBootId() != "boot" {
					t.Fatalf("watch response = %v", response)
				}
			case err := <-done:
				t.Fatalf("watch ended before snapshot: %v", err)
			}
			return cancel, done
		}
		cancelInitial, initialDone := start(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "replacement", ObserveOnly: true})
		synctest.Wait()
		if incarnation != "replacement" {
			t.Fatalf("initial passive adoption retained %q", incarnation)
		}
		cancelGateway, gatewayDone := start(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "replacement", ConnectionEpoch: 1})
		callCtx := metadata.NewIncomingContext(t.Context(), metadata.Pairs(
			SandboxMetadata, "sandbox", IncarnationMetadata, "replacement", BootMetadata, "boot", EpochMetadata, "1",
		))
		epoch, err := server.Authorize(callCtx)
		if err != nil {
			t.Fatal(err)
		}
		cancelSecond, secondDone := start(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "replacement", ObserveOnly: true})
		cancelInitial()
		cancelSecond()
		for _, done := range []<-chan error{initialDone, secondDone} {
			if err := <-done; status.Code(err) != codes.Canceled {
				t.Fatalf("passive cancellation error = %v", err)
			}
		}
		synctest.Wait()
		if err := server.Validate(epoch); err != nil || epoch.Context().Err() != nil {
			t.Fatalf("passive cancellation changed admitted epoch: validation=%v, context=%v", err, epoch.Context().Err())
		}
		if _, err := server.Authorize(callCtx); err != nil {
			t.Fatalf("passive cancellation revoked Gateway authorization: %v", err)
		}
		cancelGateway()
		if err := <-gatewayDone; status.Code(err) != codes.Canceled {
			t.Fatalf("Gateway cancellation error = %v", err)
		}
	})
}

func TestPassiveWatchCannotUndoPartiallyCompletedGatewayAdoption(t *testing.T) {
	backendIncarnation := "initial"
	server := New(Config{
		SandboxUID: "sandbox",
		AdoptIncarnation: func(_ context.Context, uid string) error {
			backendIncarnation = uid
			if uid == "replacement" {
				// The coordinator may have changed state before cancellation reaches
				// the caller; only another Gateway adoption may resolve that outcome.
				return status.Error(codes.Canceled, "adoption caller disconnected")
			}
			return nil
		},
	}, nil)
	initial, err := server.passiveWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "initial", ObserveOnly: true}, t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer initial.cancel()
	if err := server.adoptWatch(initial, true); err != nil {
		t.Fatal(err)
	}
	replacement, err := server.directWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "replacement", ConnectionEpoch: 1}, t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if err := server.adoptWatch(replacement, false); status.Code(err) != codes.Canceled {
		t.Fatalf("partial Gateway adoption error = %v", err)
	}
	server.finishWatch(replacement)
	if err := server.adoptWatch(initial, true); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("passive watch reversed partial Gateway adoption: %v", err)
	}
	if backendIncarnation != "replacement" {
		t.Fatalf("passive watch changed Runtime incarnation to %q", backendIncarnation)
	}
}
