package auth

import (
	"context"
	"crypto/x509"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// ServicePrincipal is a verified first-party peer. It proves neither an agent
// Identity nor a Kubernetes account, UID or Pod binding.
type ServicePrincipal struct{ SPIFFEID string }

// ServiceAdmission is a direct component call admitted by its method's exact service allowlist.
type ServiceAdmission struct{ Caller ServicePrincipal }

func (ServiceAdmission) isAdmission() {}

// DelegatedServiceIdentityAdmission keeps certificate actor proof separate from
// the live effective Identity and the typed provenance it acts for.
type DelegatedServiceIdentityAdmission struct {
	Actor           ServicePrincipal
	EffectiveCaller Identity
	Provenance      *delegationpb.Delegation
}

func (DelegatedServiceIdentityAdmission) isAdmission() {}

// ServiceDelegator is a method-scoped grant to assert Identities. Direct component
// authority and Connection provenance require their own grants.
type ServiceDelegator struct {
	SPIFFEID             string `mapstructure:"spiffe_id" yaml:"spiffe_id" json:"spiffe_id"`
	ConnectionProvenance bool   `mapstructure:"connection_provenance" yaml:"connection_provenance" json:"connection_provenance"`
}

type serviceDelegationPolicy struct {
	actors     map[string]delegatorGrant
	identities IdentityUIDReader
}

// ValidateServiceCallers checks an explicit service allowlist at the settings boundary.
func ValidateServiceCallers(callers []string) error {
	seen := make(map[string]struct{}, len(callers))
	for _, caller := range callers {
		if !peertls.IsServiceURI(caller) {
			return fmt.Errorf("unsupported service SPIFFE ID %q", caller)
		}
		if _, duplicate := seen[caller]; duplicate {
			return fmt.Errorf("repeated service SPIFFE ID %q", caller)
		}
		seen[caller] = struct{}{}
	}
	return nil
}

// AllowServiceCallers adds exact service identities without widening the Rule's
// existing workload or bearer path. A certificate never grants account/Pod proof.
func (r Rule) AllowServiceCallers(callers []string) (Rule, error) {
	if r.kind == ruleRefuse || r.kind == rulePublic {
		return Rule{}, fmt.Errorf("service callers require an authenticated rule")
	}
	if err := ValidateServiceCallers(callers); err != nil {
		return Rule{}, err
	}
	r.serviceCallers = make(map[string]struct{}, len(callers))
	for _, caller := range callers {
		r.serviceCallers[caller] = struct{}{}
	}
	return r, nil
}

// AllowServiceDelegationFrom grants only service actors, independently of direct
// callers and legacy token actors. The effective caller always remains an Identity.
func (r Rule) AllowServiceDelegationFrom(actors []ServiceDelegator, identities IdentityUIDReader) (Rule, error) {
	if !r.admitsIdentityCallers() || identities == nil || len(actors) == 0 {
		return Rule{}, fmt.Errorf("service delegation requires an Identity-admitting rule, Identity reader and actors")
	}
	callers := make([]string, 0, len(actors))
	for _, actor := range actors {
		callers = append(callers, actor.SPIFFEID)
	}
	if err := ValidateServiceCallers(callers); err != nil {
		return Rule{}, err
	}
	grants := make(map[string]delegatorGrant, len(actors))
	for _, actor := range actors {
		grants[actor.SPIFFEID] = delegatorGrant{connectionProvenance: actor.ConnectionProvenance}
	}
	r.serviceDelegation = &serviceDelegationPolicy{actors: grants, identities: identities}
	return r, nil
}

// admitService recognizes only a verified service certificate. Any credential
// ambiguity or failure is terminal; it never tries the bearer/workload resolver.
func (r Rule) admitService(ctx, checkContext context.Context) (context.Context, bool, error) {
	remote, ok := peer.FromContext(checkContext)
	if !ok {
		return nil, false, nil
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	if !ok || len(info.State.PeerCertificates) == 0 {
		return nil, false, nil
	}
	id, err := peertls.PeerIdentityFromTLSState(info.State, x509.ExtKeyUsageClientAuth)
	if err != nil {
		return nil, true, status.Error(codes.Unauthenticated, "invalid client certificate")
	}
	if !peertls.IsServiceURI(id) {
		return nil, false, nil
	}
	md, _ := metadata.FromIncomingContext(checkContext)
	if len(md.Get("authorization")) != 0 {
		return nil, true, status.Error(codes.Unauthenticated, "service certificate cannot be combined with authorization metadata")
	}
	actor := ServicePrincipal{SPIFFEID: id}
	envelopes := md.Get(DelegationMetadataKey)
	if len(envelopes) > 0 {
		admitted, err := r.admitServiceDelegation(ctx, checkContext, actor, envelopes)
		return admitted, true, err
	}
	if _, allowed := r.serviceCallers[id]; !allowed {
		return nil, true, status.Error(codes.PermissionDenied, "service is not admitted on this method")
	}
	return withAdmission(ctx, ServiceAdmission{Caller: actor}), true, nil
}

func (r Rule) admitServiceDelegation(ctx, checkContext context.Context, actor ServicePrincipal, envelopes []string) (context.Context, error) {
	p := r.serviceDelegation
	if p == nil {
		return nil, status.Error(codes.PermissionDenied, "service delegation is not admitted on this method")
	}
	if len(envelopes) != 1 || len(envelopes[0]) == 0 || len(envelopes[0]) > maxDelegationBytes {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidDelegation.Error())
	}
	envelope := new(delegationpb.Delegation)
	if err := proto.Unmarshal([]byte(envelopes[0]), envelope); err != nil || !validDelegation(envelope) {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidDelegation.Error())
	}
	grant, allowed := p.actors[actor.SPIFFEID]
	if !allowed {
		return nil, status.Error(codes.PermissionDenied, "service delegator is not trusted for this method")
	}
	if _, connection := envelope.GetProvenance().(*delegationpb.Delegation_Connection); connection && !grant.connectionProvenance {
		return nil, status.Error(codes.PermissionDenied, "Connection provenance is not admitted on this method")
	}
	identity, err := resolveDelegatedIdentity(checkContext, p.identities, envelope.GetIdentity())
	if err != nil {
		return nil, err
	}
	return withAdmission(ctx, DelegatedServiceIdentityAdmission{Actor: actor, EffectiveCaller: identity, Provenance: envelope}), nil
}
