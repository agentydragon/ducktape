package admin

import (
	"context"
	"testing"
	"time"

	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
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
			Ready: true, Synced: true, DecisionHistory: wantCounters, StaleAfterSeconds: 900,
			RefreshedSecondsAgo: map[string]float64{"secrets": 0},
		}},
		{"stale", staleIndex(), counters, &egressadminpb.GetHealthResponse{
			Synced: true, DecisionHistory: wantCounters, StaleAfterSeconds: 900,
			RefreshedSecondsAgo: map[string]float64{"secrets": 1200},
		}},
		{"ages rounded to tenths", rounded(), fakeHistory{}, &egressadminpb.GetHealthResponse{
			Ready: true, Synced: true, DecisionHistory: &egressadminpb.DecisionHistoryHealth{}, StaleAfterSeconds: 900,
			RefreshedSecondsAgo: map[string]float64{"secrets": 1.2},
		}},
		// No write has succeeded or failed yet, which is not the same as the latest having failed.
		{"draining before any write", drainingIndex(), fakeHistory{}, &egressadminpb.GetHealthResponse{
			Synced: true, Draining: true, DecisionHistory: &egressadminpb.DecisionHistoryHealth{}, StaleAfterSeconds: 60,
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			client, _ := serveGRPC(t, ServiceOptions{HistoryHealth: test.history, Index: test.index, Clock: clockAtStart})

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

func rounded() fakeIndex {
	index := healthyIndex()
	index.Ages = map[reader.Kind]time.Duration{policy.Secrets: 1234 * time.Millisecond}
	return index
}

func TestProbesReportReadinessAndLivenessSeparately(t *testing.T) {
	serving, notServing := healthpb.HealthCheckResponse_SERVING, healthpb.HealthCheckResponse_NOT_SERVING
	for _, test := range []struct {
		name           string
		index          fakeIndex
		healthz, livez healthpb.HealthCheckResponse_ServingStatus
	}{
		{"healthy", healthyIndex(), serving, serving},
		{"stale", staleIndex(), notServing, serving},
		{"draining", drainingIndex(), notServing, serving},
	} {
		t.Run(test.name, func(t *testing.T) {
			probes := NewProbes(test.index, clockAtStart)

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
	_, err := NewProbes(healthyIndex(), clockAtStart).Check(context.Background(), &healthpb.HealthCheckRequest{Service: "test-unknown"})

	if status.Code(err) != codes.NotFound {
		t.Fatalf("got %v, want NotFound", err)
	}
}
