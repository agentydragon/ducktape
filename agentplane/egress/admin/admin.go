// Package admin is the egress proxy's cluster-internal read side: recent decisions per subject, when
// they change, and health.
package admin

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
)

// Decisions reads a subject's retained decisions; a nil subject selects the refusals that never
// authenticated.
type Decisions interface {
	Recent(ctx context.Context, subject *decision.Subject) ([]decision.Record, error)
}

// Changes follows decision commits; see history.Changes.Follow.
type Changes interface {
	Follow(ctx context.Context, changed func() error) error
}

// HistoryHealth reports the decision writer's counters.
type HistoryHealth interface {
	Health() history.Health
}

// Index is read once per `/healthz` request, so the verdict and the ages it rests on agree.
type Index interface {
	Status(now time.Time) policy.Status
}

// New serves the admin API.
func New(decisions Decisions, changes Changes, historyHealth HistoryHealth, index Index, clock func() time.Time) http.Handler {
	s := &server{decisions: decisions, changes: changes, historyHealth: historyHealth, index: index, clock: clock}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /decisions", s.recent)
	mux.HandleFunc("GET /decisions/changes", s.follow)
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("GET /livez", s.livez)
	return mux
}

type server struct {
	decisions     Decisions
	changes       Changes
	historyHealth HistoryHealth
	index         Index
	clock         func() time.Time
}

// errorBody is FastAPI's error shape, which the egress admin clients already read.
type errorBody struct {
	Detail string `json:"detail"`
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		slog.Warn("admin response not written", "error", err)
	}
}

// recent is `?namespace=&name=` for one subject; neither selects the refusals that never authenticated.
func (s *server) recent(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	if query.Has("namespace") != query.Has("name") {
		writeJSON(w, http.StatusBadRequest, errorBody{"namespace and name are given together or not at all"})
		return
	}
	var subject *decision.Subject
	if query.Has("namespace") {
		subject = &decision.Subject{Namespace: query.Get("namespace"), Name: query.Get("name")}
	}
	records, err := s.decisions.Recent(r.Context(), subject)
	if err != nil {
		// The error can carry connection details; the response and the log name only its type.
		slog.Warn("decision history read failed", "error_type", fmt.Sprintf("%T", err))
		writeJSON(w, http.StatusServiceUnavailable, errorBody{"decision-history-unavailable"})
		return
	}
	writeJSON(w, http.StatusOK, records)
}

// follow sends a `changed` frame whenever decisions may have been committed, and at least every
// heartbeat. It carries nothing: the reader re-reads `/decisions`, which stays the authority.
func (s *server) follow(w http.ResponseWriter, r *http.Request) {
	responses := http.NewResponseController(w)
	started := false
	err := s.changes.Follow(r.Context(), func() error {
		if !started {
			started = true
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Cache-Control", "no-cache")
			// A buffering reverse proxy would hold frames back until the stream ends.
			w.Header().Set("X-Accel-Buffering", "no")
		}
		if _, err := fmt.Fprint(w, "event: changed\ndata: {}\n\n"); err != nil {
			return err
		}
		return responses.Flush()
	})
	switch {
	case !started:
		slog.Warn("decision change stream not started", "error_type", fmt.Sprintf("%T", err))
		writeJSON(w, http.StatusServiceUnavailable, errorBody{"decision-history-unavailable"})
	case r.Context().Err() == nil:
		slog.Info("decision change stream ended", "error_type", fmt.Sprintf("%T", err))
	}
}

type healthBody struct {
	Synced              bool               `json:"synced"`
	Draining            bool               `json:"draining"`
	DecisionHistory     history.Health     `json:"decisionHistory"`
	StaleAfterSeconds   float64            `json:"staleAfterSeconds"`
	RefreshedSecondsAgo map[string]float64 `json:"refreshedSecondsAgo"`
}

// readHealth is ready only while the index is both complete and still moving. `synced` alone says only
// that every kind has listed once, so a proxy whose watches had since wedged would go on answering 200
// while serving a frozen snapshot of the rules; the ages are the facts and the status the verdict.
func readHealth(index Index, historyHealth HistoryHealth, now time.Time) (body healthBody, ready bool) {
	status := index.Status(now)
	body = healthBody{
		Synced: status.Synced, Draining: status.Draining, DecisionHistory: historyHealth.Health(),
		StaleAfterSeconds:   status.StaleAfter.Seconds(),
		RefreshedSecondsAgo: make(map[string]float64, len(status.Ages)),
	}
	for kind, age := range status.Ages {
		body.RefreshedSecondsAgo[string(kind)] = math.Round(age.Seconds()*10) / 10
	}
	return body, status.Available
}

func (s *server) healthz(w http.ResponseWriter, _ *http.Request) {
	body, ready := readHealth(s.index, s.historyHealth, s.clock())
	code := http.StatusOK
	if !ready {
		code = http.StatusServiceUnavailable
	}
	writeJSON(w, code, body)
}

func (s *server) livez(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]bool{"alive": true})
}
