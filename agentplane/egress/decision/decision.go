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
	// ImpersonationForbidden is a request decided by a kubernetesServiceAccount credential that
	// carried impersonation headers of its own.
	ImpersonationForbidden Reason = "impersonation-forbidden"
	AddressForbidden       Reason = "address-forbidden"
	HostUnresolved         Reason = "host-unresolved"
	Unavailable            Reason = "unavailable"
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

// Subject is who a request authenticated as: a ServiceAccount or an Identity.
type Subject interface{ isSubject() }

// ServiceAccount is a namespaced ServiceAccount: one a request authenticated as, or one the proxy
// impersonated for it.
type ServiceAccount struct {
	Namespace string
	Name      string
}

// Identity is an Identity in the proxy's policy namespace, by name. Like a ServiceAccount, a name
// spans every incarnation that has held it.
type Identity struct {
	Name string
}

func (ServiceAccount) isSubject() {}
func (Identity) isSubject()       {}

// Record is one admission as the proxy logs and persists it, and as the admin API serves it. It is
// admission metadata captured before forwarding, never evidence that the upstream completed. Paths
// are deliberately absent: arbitrary path segments can contain secrets.
type Record struct {
	EventID uuid.UUID
	At      time.Time
	// ProducerID tells replicas apart; no Agentplane reader uses it.
	ProducerID uuid.UUID
	// Subject is nil on a refusal that never authenticated.
	Subject Subject
	// SourcePodUID and ConnectionID tell Pods and connections apart for operators querying the table.
	SourcePodUID *string
	ConnectionID string
	Phase        Phase
	Method       string
	Host         string
	Port         int
	Outcome      Outcome
	// Reason is set on a deny.
	Reason *Reason
	// Binding, Policy and Rule name the rule that admitted the request.
	Binding *string
	Policy  *string
	Rule    *int
	// Substituted is whether a credential replaced a placeholder.
	Substituted bool
	// Impersonated is the ServiceAccount the proxy acted as on the API server, beside its own
	// identity, which the API server records as the original user.
	Impersonated *ServiceAccount
	// Address is the address the host was pinned to, when admitted.
	Address *string
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
	switch subject := r.Subject.(type) {
	case ServiceAccount:
		attrs = append(attrs, slog.String("subject", subject.Namespace+"/"+subject.Name))
	case Identity:
		attrs = append(attrs, slog.String("identity", subject.Name))
	}
	if r.Impersonated != nil {
		attrs = append(attrs, slog.String("impersonated", r.Impersonated.Namespace+"/"+r.Impersonated.Name))
	}
	return slog.GroupValue(attrs...)
}
