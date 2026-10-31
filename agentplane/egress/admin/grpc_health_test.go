package admin

import (
	"context"
	"testing"

	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestGetHealthCarriesTheVerdictAndItsFacts(t *testing.T) {
	available := true
	counters := fakeHistory{
		Accepted: 1, Acknowledged: 2, DroppedOverflow: 3, DroppedUnavailable: 4, DroppedExpired: 5, DroppedShutdown: 6,
		WriteFailures: 7, CleanupFailures: 8, Available: &available, Queued: 9, WriterRunning: true,
	}
	wantCounters := &egressadminpb.DecisionHistoryHealth{
		Accepted: 1, Acknowledged: 2, DroppedOverflow: 3, DroppedUnavailable: 4, DroppedExpired: 5, DroppedShutdown: 6,
		WriteFailures: 7, CleanupFailures: 8, Available: proto.Bool(true), Queued: 9, WriterRunning: true,
	}
	for _, test := range []struct {
		name    string
		index   fakeIndex
		history fakeHistory
		want    *egressadminpb.GetHealthResponse
	}{
		{"healthy", healthyIndex(), counters, &egressadminpb.GetHealthResponse{
			Ready: true, Synced: true, DecisionHistory: wantCounters,
		}},
		{"failing", failingIndex(), counters, &egressadminpb.GetHealthResponse{
			Synced: true, DecisionHistory: wantCounters,
			Failing: map[string]*egressadminpb.KindFailure{
				"secrets": {Error: "test-failure", Since: timestamppb.New(started)},
			},
		}},
		// No write has succeeded or failed yet, which is not the same as the latest having failed.
		{"draining before any write", drainingIndex(), fakeHistory{}, &egressadminpb.GetHealthResponse{
			Synced: true, Draining: true, DecisionHistory: &egressadminpb.DecisionHistoryHealth{},
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			client, _ := serveGRPC(t, ServiceOptions{HistoryHealth: test.history, Index: test.index})

			response, err := client.GetHealth(bearing(readerToken), &egressadminpb.GetHealthRequest{})
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(response, test.want) {
				t.Errorf("got %v, want %v", response, test.want)
			}
		})
	}
}

func TestProbesReportReadinessAndLivenessSeparately(t *testing.T) {
	serving, notServing := healthpb.HealthCheckResponse_SERVING, healthpb.HealthCheckResponse_NOT_SERVING
	for _, test := range []struct {
		name           string
		index          fakeIndex
		healthz, livez healthpb.HealthCheckResponse_ServingStatus
	}{
		{"healthy", healthyIndex(), serving, serving},
		{"failing", failingIndex(), notServing, serving},
		{"draining", drainingIndex(), notServing, serving},
	} {
		t.Run(test.name, func(t *testing.T) {
			probes := NewProbes(test.index)

			for service, want := range map[string]healthpb.HealthCheckResponse_ServingStatus{"": test.healthz, LivenessService: test.livez} {
				response, err := probes.Check(context.Background(), &healthpb.HealthCheckRequest{Service: service})
				if err != nil || response.GetStatus() != want {
					t.Errorf("service %q: %v, %v; want %v", service, response, err, want)
				}
			}
		})
	}
}

func TestProbesRefuseAServiceTheyDoNotKnow(t *testing.T) {
	_, err := NewProbes(healthyIndex()).Check(context.Background(), &healthpb.HealthCheckRequest{Service: "test-unknown"})

	if status.Code(err) != codes.NotFound {
		t.Fatalf("got %v, want NotFound", err)
	}
}
