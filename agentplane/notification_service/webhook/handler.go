// Package webhook is the route GitHub delivers webhooks to.
package webhook

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql/driver"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github"
	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/uuid"
)

const (
	// Pattern is the route's ServeMux pattern.
	Pattern = "POST /v1/webhooks/github"

	// ingressBound limits a delivery from its first request byte through the durable commit.
	ingressBound = 10 * time.Second
	// busyRetryAfter is the seconds a saturated replica asks GitHub to wait, which GitHub does not heed.
	busyRetryAfter = "5"
	// maxLoggedEvent bounds the event name a rejection logs; the header is untrusted even under a valid signature.
	maxLoggedEvent = 64

	invalidDelivery = "invalid GitHub delivery"
	backendDown     = "backend temporarily unavailable"
	internalFault   = "internal server error"
)

// Handler accepts signed GitHub webhook deliveries into the store. At most Settings.WebhookConcurrency requests are
// in flight; each holds its slot through the commit, and one more is refused with 503 instead of queued.
type Handler struct {
	appID        int64
	secret       []byte
	maxBodyBytes int64
	slots        chan struct{}
	database     *store.Store
	// bound is ingressBound; a test shortens it.
	bound time.Duration
}

// New is the handler for the App that settings name. The settings are validated by their owner.
func New(settings settings.Settings, database *store.Store) *Handler {
	return &Handler{
		appID:        settings.AppID,
		secret:       []byte(settings.WebhookSecret),
		maxBodyBytes: int64(settings.MaxBodyBytes),
		slots:        make(chan struct{}, settings.WebhookConcurrency),
		database:     database,
		bound:        ingressBound,
	}
}

// receipt is the body of an accepted delivery. A redelivery of a stored one is accepted as a duplicate.
type receipt struct {
	Accepted  bool `json:"accepted"`
	Duplicate bool `json:"duplicate"`
}

// refusal is the body of every refusal. Details are fixed strings: nothing from the request or from an error
// reaches GitHub.
type refusal struct {
	Detail string `json:"detail"`
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	select {
	case h.slots <- struct{}{}:
		defer func() { <-h.slots }()
	default:
		w.Header().Set("Retry-After", busyRetryAfter)
		respond(r.Context(), w, http.StatusServiceUnavailable, refusal{Detail: "GitHub ingress busy"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), h.bound)
	defer cancel()
	deadline, _ := ctx.Deadline()
	// The bound covers a body that never finishes, which no context can interrupt.
	if err := http.NewResponseController(w).SetReadDeadline(deadline); err != nil {
		slog.ErrorContext(ctx, "GitHub webhook body deadline unavailable", "error", err)
		respond(ctx, w, http.StatusInternalServerError, refusal{Detail: internalFault})
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, h.maxBodyBytes+1))
	switch {
	case timedOut(ctx, err):
		respond(ctx, w, http.StatusServiceUnavailable, refusal{Detail: backendDown})
		return
	case err != nil:
		slog.WarnContext(ctx, "GitHub webhook body unreadable", "error", err)
		respond(ctx, w, http.StatusBadRequest, refusal{Detail: "request body unreadable"})
		return
	case int64(len(raw)) > h.maxBodyBytes:
		respond(ctx, w, http.StatusRequestEntityTooLarge, refusal{Detail: "GitHub payload exceeds configured limit"})
		return
	}
	status, body := h.receive(ctx, r.Header, raw)
	respond(ctx, w, status, body)
}

// receive checks and stores one delivery, in this order: the delivery ID, then the signature
// (so that nothing about an unsigned body is learned from its refusal), a ping, and last the payload.
func (h *Handler) receive(ctx context.Context, header http.Header, raw []byte) (int, any) {
	event := header.Get("X-GitHub-Event")
	deliveryID, err := uuid.Parse(header.Get("X-GitHub-Delivery"))
	if err != nil {
		logRejection(ctx, event, nil, "invalid_delivery_id", nil)
		return http.StatusBadRequest, refusal{Detail: invalidDelivery}
	}
	if !h.signed(raw, header.Get("X-Hub-Signature-256")) {
		logRejection(ctx, event, &deliveryID, "invalid_signature", nil)
		return http.StatusUnauthorized, refusal{Detail: "invalid GitHub signature"}
	}
	if event == "ping" {
		return http.StatusAccepted, receipt{Accepted: true}
	}
	ingest, err := github.ParseDelivery(h.appID, event, deliveryID, raw)
	switch {
	case errors.Is(err, github.ErrUnsupportedEvent):
		logRejection(ctx, event, &deliveryID, "unsupported_event", nil)
		return http.StatusBadRequest, refusal{Detail: invalidDelivery}
	case errors.Is(err, github.ErrMissingRepository):
		logRejection(ctx, event, &deliveryID, "missing_repository", nil)
		return http.StatusBadRequest, refusal{Detail: invalidDelivery}
	case errors.Is(err, github.ErrInvalidPayload):
		logRejection(ctx, event, &deliveryID, "payload_validation", err)
		return http.StatusBadRequest, refusal{Detail: invalidDelivery}
	case err != nil:
		slog.ErrorContext(ctx, "GitHub webhook payload unreadable", "error", err)
		return http.StatusInternalServerError, refusal{Detail: internalFault}
	}
	created, err := h.database.IngestGitHub(ctx, ingest)
	switch {
	case errors.Is(err, store.ErrDeliveryReused):
		return http.StatusConflict, refusal{Detail: err.Error()}
	case unavailable(ctx, err):
		return http.StatusServiceUnavailable, refusal{Detail: backendDown}
	case err != nil:
		slog.ErrorContext(ctx, "GitHub webhook ingest failed", "error", err)
		return http.StatusInternalServerError, refusal{Detail: internalFault}
	}
	return http.StatusAccepted, receipt{Accepted: true, Duplicate: !created}
}

func (h *Handler) signed(raw []byte, signature string) bool {
	mac := hmac.New(sha256.New, h.secret)
	mac.Write(raw)
	expected := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(expected), []byte(signature))
}

// timedOut is a failure that came with the request's time running out, whether on its context or on the connection.
func timedOut(ctx context.Context, err error) bool {
	return err != nil && (ctx.Err() != nil || errors.Is(err, os.ErrDeadlineExceeded))
}

// unavailable is a failure of the request's time or of the connection to the database, which a later delivery may
// not meet. Anything else the store reports is a fault.
func unavailable(ctx context.Context, err error) bool {
	var network net.Error
	return timedOut(ctx, err) || errors.Is(err, driver.ErrBadConn) || errors.As(err, &network)
}

// logRejection reports a delivery that was refused for what it carries. The event and delivery ID are untrusted even
// under a valid signature, so the event is bounded (and escaped by the JSON log handler); a malformed delivery ID,
// the signature, the body and the secret are never logged. problem is an ErrInvalidPayload, whose text names fields
// and array positions of the schema and quotes nothing of the body.
func logRejection(ctx context.Context, event string, deliveryID *uuid.UUID, reason string, problem error) {
	logged, truncated := boundedEvent(event)
	attributes := []any{"event", logged, "event_truncated", truncated, "reason", reason}
	if deliveryID != nil {
		attributes = append(attributes, "delivery_id", deliveryID.String())
	}
	if problem != nil {
		attributes = append(attributes, "problem", problem.Error())
	}
	slog.WarnContext(ctx, "GitHub webhook rejected", attributes...)
}

func boundedEvent(event string) (string, bool) {
	if utf8.RuneCountInString(event) <= maxLoggedEvent {
		return event, false
	}
	return string([]rune(event)[:maxLoggedEvent]), true
}

func respond(ctx context.Context, w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		slog.WarnContext(ctx, "GitHub webhook response not written", "error", err)
	}
}
