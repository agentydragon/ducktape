package control

import (
	"context"
	"testing"
	"testing/synctest"
	"time"

	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type watchStateTestStream struct {
	ctx       context.Context
	responses chan *runnerservicepb.WatchStateResponse
}

func (s *watchStateTestStream) Send(response *runnerservicepb.WatchStateResponse) error {
	select {
	case s.responses <- proto.Clone(response).(*runnerservicepb.WatchStateResponse):
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
		server := New(Config{
			Security: runnertransport.Security{Mode: runnertransport.Insecure},
			Changes:  func() <-chan struct{} { return changed },
		}, nil)
		facts := &runnerstatepb.RunnerFacts{}
		stream := &watchStateTestStream{
			ctx:       ctx,
			responses: make(chan *runnerservicepb.WatchStateResponse, 8),
		}
		done := make(chan error, 1)
		go func() {
			done <- server.forwardWatchState(ctx, func() (*runnerservicepb.WatchStateResponse, error) {
				return &runnerservicepb.WatchStateResponse{
					SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", RunnerBootId: "runner-boot-id",
					ConnectionEpoch: 1, Facts: proto.Clone(facts).(*runnerstatepb.RunnerFacts),
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
