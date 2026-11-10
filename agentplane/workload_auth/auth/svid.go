package auth

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// SVIDResolver resolves an already verified workload leaf to the subject its WorkloadCredential
// names. Its reader must read the API server directly: a certificate never preserves the authority
// of a deleted record or a replaced credential ID.
type SVIDResolver struct {
	records *resolver.Resolver
}

func NewSVIDResolver(objects client.Reader, namespaces []string, identities *resolver.Identities) (*SVIDResolver, error) {
	records, err := resolver.New(objects, namespaces, identities)
	if err != nil {
		return nil, err
	}
	return &SVIDResolver{records: records}, nil
}

// NewKubernetesSVIDResolver creates an uncached reader for a service's SVID boundary.
func NewKubernetesSVIDResolver(config *rest.Config, namespaces []string, identities *resolver.Identities) (*SVIDResolver, error) {
	records, err := resolver.NewKubernetes(config, namespaces, identities)
	if err != nil {
		return nil, err
	}
	return &SVIDResolver{records: records}, nil
}

// Resolve authenticates a gRPC TLS peer. The listener must use the transport
// package's workload server credentials, which verify its chain and client EKU.
func (r *SVIDResolver) Resolve(ctx context.Context) (Principal, error) {
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return Principal{}, status.Error(codes.Unauthenticated, "workload TLS peer is missing")
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	if !ok || len(info.State.PeerCertificates) == 0 {
		return Principal{}, status.Error(codes.Unauthenticated, "workload client certificate is missing")
	}
	principal, err := r.ResolveVerifiedLeaf(ctx, info.State.PeerCertificates[0])
	return principal, svidStatus(ctx, err)
}

// ResolveVerifiedLeaf takes a leaf whose signature and trust chain the TLS listener has verified.
// It rechecks validity and the live WorkloadCredential on each call. Headers, metadata and request
// bodies never select the subject. The accepted incarnation's Pod, when its owner has named one, is
// the Principal's Pod.
func (r *SVIDResolver) ResolveVerifiedLeaf(ctx context.Context, leaf *x509.Certificate) (Principal, error) {
	resolved, err := r.records.Resolve(ctx, leaf)
	if errors.Is(err, resolver.ErrUnavailable) {
		return Principal{}, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	if err != nil {
		return Principal{}, ErrInvalidBearer
	}
	// CLEANUP(added 2026-11-10): an Identity subject yields an Identity Principal in
	// SERVICES_IDENTITY_PRINCIPAL (agentplane/plans/task_dag.md); until then no service authorizes one.
	account, ok := resolved.Subject.(resolver.ServiceAccount)
	if !ok {
		return Principal{}, ErrInvalidBearer
	}
	principal := Principal{
		Account:           Account{Namespace: account.Namespace, Name: account.Name},
		ServiceAccountUID: string(account.UID),
	}
	if resolved.Pod != nil {
		principal.PodName, principal.PodUID = resolved.Pod.Name, string(resolved.Pod.UID)
	}
	return principal, nil
}

func svidStatus(ctx context.Context, err error) error {
	switch {
	case err == nil:
		return nil
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case errors.Is(err, ErrUnavailable):
		return status.Error(codes.Unavailable, "workload SVID authentication unavailable")
	default:
		return status.Error(codes.Unauthenticated, "invalid workload SVID")
	}
}

// WithListenerAuthentication binds this resolver to the explicit authentication modes on one
// listener. A bearer mode must use this Resolver's audience; certificate authentication requires
// a live WorkloadCredential resolver. Callers and methods remain the responsibility of the existing Rules.
func (r *Resolver) WithListenerAuthentication(config listener.Config, svid *SVIDResolver) (*Resolver, error) {
	if err := config.Validate(); err != nil {
		return nil, fmt.Errorf("listener authentication: %w", err)
	}
	if config.Has(listener.SPIFFE) && svid == nil {
		return nil, errors.New("SVID resolver is required when SPIFFE authentication is enabled")
	}
	if config.Has(listener.KubernetesBearer) && !config.AllowsBearerAudience(r.audience) {
		return nil, errors.New("listener bearer audiences must include the TokenReview resolver audience")
	}
	copy := *r
	config.Auth = append([]listener.Authentication(nil), config.Auth...)
	if config.TLS != nil {
		tlsConfig := *config.TLS
		config.TLS = &tlsConfig
	}
	copy.listenerAuth = &config
	copy.svid = svid
	return &copy, nil
}

func (r *Resolver) usesSVID(ctx context.Context) bool {
	if r.listenerAuth == nil {
		return false
	}
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return false
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	if !ok {
		return false
	}
	return len(info.State.PeerCertificates) > 0
}

// requireBearerSelection applies configured listener policy to a bearer that is
// verified outside Resolve, such as a delegated actor token. An unconfigured
// resolver retains its existing TokenReview contract.
func (r *Resolver) requireBearerSelection(ctx context.Context) error {
	if r.listenerAuth == nil {
		return nil
	}
	mode, err := r.listenerAuth.Select(r.usesSVID(ctx), true)
	if err != nil || mode != listener.KubernetesBearer {
		return status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	return nil
}

func (r *Resolver) resolveSVID(ctx context.Context) (Principal, bool, error) {
	if r.listenerAuth == nil {
		return Principal{}, false, nil
	}
	remote, ok := peer.FromContext(ctx)
	var info credentials.TLSInfo
	if ok {
		info, ok = remote.AuthInfo.(credentials.TLSInfo)
	}
	if hasDelegationCredential(ctx) {
		return Principal{}, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	hasCertificate := ok && len(info.State.PeerCertificates) > 0
	mode, err := r.listenerAuth.Select(hasCertificate, hasDirectBearerCredential(ctx))
	if err != nil {
		return Principal{}, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	if mode != listener.SPIFFE {
		return Principal{}, false, nil
	}
	if r.svid == nil {
		return Principal{}, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	principal, err := r.svid.Resolve(ctx)
	return principal, true, err
}

// ResolveRequest uses the HTTP listener's verified TLS peer and the same
// authentication selection as native RPCs. Identity is never read from headers.
func (r *Resolver) ResolveRequest(request *http.Request) (Principal, error) {
	ctx := metadata.NewIncomingContext(request.Context(), metadata.MD{
		"authorization":       request.Header.Values("Authorization"),
		DelegationMetadataKey: request.Header.Values(DelegationMetadataKey),
	})
	if request.TLS != nil {
		ctx = peer.NewContext(ctx, &peer.Peer{AuthInfo: credentials.TLSInfo{State: *request.TLS}})
	}
	return r.Resolve(ctx)
}

func hasDirectBearerCredential(ctx context.Context) bool {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return false
	}
	return len(md.Get("authorization")) > 0
}

func hasDelegationCredential(ctx context.Context) bool {
	md, ok := metadata.FromIncomingContext(ctx)
	return ok && len(md.Get(DelegationMetadataKey)) > 0
}
