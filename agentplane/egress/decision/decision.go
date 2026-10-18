// Package decision is the vocabulary of one egress admission: why it was refused, and the record
// of it that history stores and the admin API serves. Names and metadata only, never values.
package decision

import (
	"log/slog"
	"time"

	"github.com/google/uuid"
)

// Reason is the machine-readable half of every refusal, as the `x-agentplane-egress` header
// carries it and as history stores it.
type Reason string

const (
	TokenMissing          Reason = "token-missing"
	TokenRejected         Reason = "token-rejected"
	PodMismatch           Reason = "pod-mismatch"
	NoBinding             Reason = "no-binding"
	NoRule                Reason = "no-rule"
	InvalidBody           Reason = "invalid-body"
	PlaceholderUnresolved Reason = "placeholder-unresolved"
	CredentialUnavailable Reason = "credential-unavailable"
	AddressForbidden      Reason = "address-forbidden"
	HostUnresolved        Reason = "host-unresolved"
	Unavailable           Reason = "unavailable"
)

type Outcome string

const (
	Allow Outcome = "allow"
	Deny  Outcome = "deny"
)

type Phase string

const (
	Connect     Phase = "connect"
	HTTPRequest Phase = "http_request"
)

// Subject is the ServiceAccount a token proved.
type Subject struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

// Record is one admission as the proxy logs and persists it, and as the admin API serves it: the
// JSON names are the wire contract the integration app reads. It is admission metadata captured
// before forwarding, never evidence that the upstream completed. Paths are deliberately absent:
// arbitrary path segments can contain secrets.
type Record struct {
	EventID uuid.UUID `json:"event_id"`
	At      time.Time `json:"at"`
	// ProducerID tells replicas apart; no Agentplane reader uses it.
	ProducerID uuid.UUID `json:"producer_id"`
	// Subject is nil on a refusal that never authenticated.
	Subject *Subject `json:"subject"`
	// SourcePodUID and ConnectionID tell Pods and connections apart for operators querying the table.
	SourcePodUID *string `json:"source_pod_uid"`
	ConnectionID string  `json:"connection_id"`
	Phase        Phase   `json:"phase"`
	Method       string  `json:"method"`
	Host         string  `json:"host"`
	Port         int     `json:"port"`
	Outcome      Outcome `json:"outcome"`
	// Reason is set on a deny.
	Reason *Reason `json:"reason"`
	// Binding, Policy and Rule name the rule that admitted the request.
	Binding *string `json:"binding"`
	Policy  *string `json:"policy"`
	Rule    *int    `json:"rule"`
	// Substituted is whether a credential replaced a placeholder.
	Substituted bool `json:"substituted"`
	// Address is the address the host was pinned to, when admitted.
	Address *string `json:"address"`
}

// LogValue logs the fields an operator reads in a log line; the pointer fields would print as
// addresses.
func (r Record) LogValue() slog.Value {
	attrs := []slog.Attr{
		slog.String("outcome", string(r.Outcome)), slog.String("method", r.Method),
		slog.String("host", r.Host), slog.Int("port", r.Port),
	}
	if r.Reason != nil {
		attrs = append(attrs, slog.String("reason", string(*r.Reason)))
	}
	if r.Subject != nil {
		attrs = append(attrs, slog.String("subject", r.Subject.Namespace+"/"+r.Subject.Name))
	}
	return slog.GroupValue(attrs...)
}
