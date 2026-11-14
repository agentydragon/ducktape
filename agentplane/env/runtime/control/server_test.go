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
					Facts: proto.Clone(facts).(*environmentruntimestatepb.EnvironmentRuntimeFacts),
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

func TestControllerWatchLossDoesNotRevokeCalls(t *testing.T) {
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
				if response.GetEnvironmentRuntimeBootId() != "boot" {
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
		cancelGateway, gatewayDone := start(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "replacement"})
		callCtx := metadata.NewIncomingContext(t.Context(), metadata.Pairs(
			SandboxMetadata, "sandbox", IncarnationMetadata, "replacement", BootMetadata, "boot",
		))
		authorization, err := server.Authorize(callCtx)
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
		if err := server.Validate(authorization); err != nil || authorization.Context().Err() != nil {
			t.Fatalf("passive cancellation changed admitted authorization: validation=%v, context=%v", err, authorization.Context().Err())
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

func TestControllerBindingSurvivesUncertainAdoption(t *testing.T) {
	backendIncarnation := ""
	attempts := 0
	server := New(Config{
		SandboxUID: "sandbox",
		AdoptIncarnation: func(_ context.Context, uid string) error {
			backendIncarnation = uid
			attempts++
			if attempts == 1 {
				return status.Error(codes.Canceled, "adoption caller disconnected")
			}
			return nil
		},
	}, nil)
	observer := func(uid string) *watch {
		t.Helper()
		w, err := server.passiveWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: uid, ObserveOnly: true}, t.Context())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(w.cancel)
		return w
	}
	current := observer("current")
	if err := server.bindControllerWatch(current); status.Code(err) != codes.Canceled {
		t.Fatalf("uncertain adoption = %v", err)
	}
	for _, uid := range []string{"older", "successor"} {
		if err := server.bindControllerWatch(observer(uid)); status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("different Controller UID %s after uncertain adoption = %v", uid, err)
		}
	}
	if _, err := server.directWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "current"}, t.Context()); status.Code(err) != codes.Unavailable {
		t.Fatalf("Gateway admitted before settled binding = %v", err)
	}
	if backendIncarnation != "current" || attempts != 1 {
		t.Fatalf("uncertain binding reversed: uid=%s attempts=%d", backendIncarnation, attempts)
	}
	if err := server.bindControllerWatch(current); err != nil {
		t.Fatal(err)
	}
	if _, err := server.directWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "older"}, t.Context()); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Gateway changed settled binding = %v", err)
	}
}

func TestCallsRemainBoundToIncarnationAcrossIndependentGatewayWatches(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		security, err := (environmentruntimetransport.Security{Mode: environmentruntimetransport.Insecure}).Open(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = security.Close() })
		changed := make(chan struct{})
		server := New(Config{
			Security: security, SandboxUID: "sandbox", BootID: "boot", IsClosing: func() bool { return false },
			AdoptIncarnation: func(context.Context, string) error { return nil },
			Facts: func() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
				return &environmentruntimestatepb.EnvironmentRuntimeFacts{}, nil
			},
			Changes: func() <-chan struct{} { return changed },
		}, nil)
		callContext := func(incarnation, boot string) context.Context {
			return metadata.NewIncomingContext(t.Context(), metadata.Pairs(SandboxMetadata, "sandbox", IncarnationMetadata, incarnation, BootMetadata, boot))
		}
		if _, err := server.Authorize(callContext("pod", "boot")); status.Code(err) != codes.Unavailable {
			t.Fatalf("call before incarnation adoption = %v", err)
		}
		start := func(incarnation string) (context.CancelFunc, <-chan error) {
			t.Helper()
			ctx, cancel := context.WithCancel(t.Context())
			stream := &watchStateTestStream{ctx: ctx, responses: make(chan *environmentruntimepb.WatchStateResponse, 1)}
			done := make(chan error, 1)
			go func() {
				done <- server.WatchState(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: incarnation}, stream)
			}()
			select {
			case <-stream.responses:
			case err := <-done:
				t.Fatalf("watch ended before snapshot: %v", err)
			}
			return cancel, done
		}
		binding, err := server.passiveWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "pod", ObserveOnly: true}, t.Context())
		if err != nil {
			t.Fatal(err)
		}
		defer binding.cancel()
		if err := server.bindControllerWatch(binding); err != nil {
			t.Fatal(err)
		}
		cancelFirst, firstDone := start("pod")
		var admission *Authorization
		if err := server.Admit(callContext("pod", "boot"), func(authorization *Authorization) error { admission = authorization; return nil }); err != nil {
			t.Fatalf("admission = %v", err)
		}
		cancelFirst()
		if err := <-firstDone; status.Code(err) != codes.Canceled {
			t.Fatalf("watch cancellation = %v", err)
		}
		if _, err := server.Authorize(callContext("pod", "boot")); err != nil {
			t.Fatalf("call after watch cancellation = %v", err)
		}
		cancelSecond, secondDone := start("pod")
		cancelThird, thirdDone := start("pod")
		synctest.Wait()
		select {
		case err := <-secondDone:
			t.Fatalf("another Gateway watch canceled its peer: %v", err)
		default:
		}
		if err := server.Validate(admission); err != nil || admission.Context().Err() != nil {
			t.Fatalf("Gateway watch change ended admission: validation=%v context=%v", err, admission.Context().Err())
		}
		for _, key := range []string{SandboxMetadata, IncarnationMetadata, BootMetadata} {
			for _, values := range [][]string{nil, {""}, {"value", "value"}} {
				md := metadata.Pairs(SandboxMetadata, "sandbox", IncarnationMetadata, "pod", BootMetadata, "boot")
				md[key] = values
				if _, err := server.Authorize(metadata.NewIncomingContext(t.Context(), md)); status.Code(err) != codes.Unauthenticated {
					t.Fatalf("invalid %s %v = %v", key, values, err)
				}
			}
		}
		if _, err := server.directWatchContext(&environmentruntimepb.WatchStateRequest{SandboxUid: "sandbox", IncarnationUid: "new-pod"}, t.Context()); status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("Gateway changed immutable binding = %v", err)
		}
		if err := server.Validate(admission); err != nil || admission.Context().Err() != nil {
			t.Fatalf("rejected watch canceled current admission: %v", err)
		}
		for _, ctx := range []context.Context{callContext("new-pod", "boot"), callContext("pod", "old-boot")} {
			if _, err := server.Authorize(ctx); status.Code(err) != codes.Unavailable {
				t.Fatalf("stale identity = %v", err)
			}
		}
		cancelSecond()
		cancelThird()
		for _, done := range []<-chan error{secondDone, thirdDone} {
			if err := <-done; status.Code(err) != codes.Canceled {
				t.Fatalf("independent watch cancellation = %v", err)
			}
		}
	})
}
