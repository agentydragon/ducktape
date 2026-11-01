package probes

import (
	"database/sql"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func TestReadinessNeedsTheWakeupsTheDatabaseAndAServiceThatIsNotDraining(t *testing.T) {
	for name, spoil := range map[string]func(p *Probes){
		"draining":    func(p *Probes) { p.Drain() },
		"no wakeups":  func(p *Probes) { p.hub.Close() },
		"no database": func(p *Probes) { p.db.Close() },
	} {
		t.Run(name, func(t *testing.T) {
			db, err := sql.Open("postgres", integrationDatabase.URL)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { db.Close() })
			hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, "test_probes")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { hub.Close() })
			p := New(db, hub)
			request := &healthpb.HealthCheckRequest{}
			if response, err := p.Check(t.Context(), request); err != nil || response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
				t.Fatalf("a healthy service is %v, %v", response.GetStatus(), err)
			}
			spoil(p)
			if response, err := p.Check(t.Context(), request); err != nil || response.GetStatus() != healthpb.HealthCheckResponse_NOT_SERVING {
				t.Errorf("readiness = %v, %v; want NOT_SERVING", response.GetStatus(), err)
			}
			// Liveness never follows the dependencies, so that waiting out their outage restarts nothing.
			response, err := p.Check(t.Context(), &healthpb.HealthCheckRequest{Service: LivenessService})
			if err != nil || response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
				t.Errorf("liveness = %v, %v; want SERVING", response.GetStatus(), err)
			}
		})
	}
}
