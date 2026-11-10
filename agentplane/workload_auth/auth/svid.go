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

// Resolve authenticates a gRPC TLS peer whose record names a ServiceAccount, and refuses an
// Identity subject: only ResolveSubject's callers serve one.
func (r *SVIDResolver) Resolve(ctx context.Context) (Principal, error) {
	subject, err := r.ResolveSubject(ctx)
	if err != nil {
		return Principal{}, err
	}
	principal, ok := subject.(Principal)
	if !ok {
		return Principal{}, status.Error(codes.Unauthenticated, "invalid workload SVID")
	}
	return principal, nil
}

// ResolveSubject authenticates a gRPC TLS peer. The listener must use the transport
// package's workload server credentials, which verify its chain and client EKU.
func (r *SVIDResolver) ResolveSubject(ctx context.Context) (Subject, error) {
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "workload TLS peer is missing")
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	if !ok || len(info.State.PeerCertificates) == 0 {
		return nil, status.Error(codes.Unauthenticated, "workload client certificate is missing")
	}
	subject, err := r.ResolveVerifiedLeaf(ctx, info.State.PeerCertificates[0])
	return subject, svidStatus(ctx, err)
}

// ResolveVerifiedLeaf takes a leaf whose signature and trust chain the TLS listener has verified.
// It rechecks validity and the live WorkloadCredential on each call. Headers, metadata and request
// bodies never select the subject. The accepted incarnation's Pod, when its owner has named one, is
// the subject's Pod.
func (r *SVIDResolver) ResolveVerifiedLeaf(ctx context.Context, leaf *x509.Certificate) (Subject, error) {
	resolved, err := r.records.Resolve(ctx, leaf)
	if errors.Is(err, resolver.ErrUnavailable) {
		return nil, fmt.Errorf("%w: %w", ErrUnavailable, err)
	}
	if err != nil {
		return nil, ErrInvalidBearer
	}
	var podName, podUID string
	if resolved.Pod != nil {
		podName, podUID = resolved.Pod.Name, string(resolved.Pod.UID)
	}
	switch subject := resolved.Subject.(type) {
	case resolver.ServiceAccount:
		return Principal{
			Account:           Account{Namespace: subject.Namespace, Name: subject.Name},
			ServiceAccountUID: string(subject.UID),
			PodName:           podName, PodUID: podUID,
		}, nil
	case resolver.Identity:
		return IdentityPrincipal{
			Identity: Identity{Name: subject.Name, UID: string(subject.UID)}, Workload: WorkloadOf(resolved),
			PodName: podName, PodUID: podUID,
		}, nil
	default:
		panic(fmt.Sprintf("unknown subject %T", resolved.Subject))
	}
}

// WorkloadOf is the provenance a resolved workload's record carries.
func WorkloadOf(peer resolver.Peer) Workload {
	workload := Workload{Namespace: peer.Workload.Namespace, Name: peer.Workload.Name, Owner: StandingWorkload{}}
	if owner := peer.Owner; owner != nil {
		workload.Owner = ControllerOwner{APIVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name, UID: string(owner.UID)}
	}
	return workload
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

// resolveSVID is the ServiceAccount-only path: a method that has not opted into AnySubject refuses
// an Identity subject.
func (r *Resolver) resolveSVID(ctx context.Context) (Principal, bool, error) {
	subject, secured, err := r.resolveSVIDSubject(ctx)
	if !secured || err != nil {
		return Principal{}, secured, err
	}
	principal, ok := subject.(Principal)
	if !ok {
		return Principal{}, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	return principal, true, nil
}

func (r *Resolver) resolveSVIDSubject(ctx context.Context) (Subject, bool, error) {
	if r.listenerAuth == nil {
		return nil, false, nil
	}
	remote, ok := peer.FromContext(ctx)
	var info credentials.TLSInfo
	if ok {
		info, ok = remote.AuthInfo.(credentials.TLSInfo)
	}
	if hasDelegationCredential(ctx) {
		return nil, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	hasCertificate := ok && len(info.State.PeerCertificates) > 0
	mode, err := r.listenerAuth.Select(hasCertificate, hasDirectBearerCredential(ctx))
	if err != nil {
		return nil, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	if mode != listener.SPIFFE {
		return nil, false, nil
	}
	if r.svid == nil {
		return nil, true, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	subject, err := r.svid.ResolveSubject(ctx)
	return subject, true, err
}

// ResolveSubject is Resolve for a destination that also serves Identity callers: an SVID yields
// its record's subject, a bearer the ServiceAccount Principal TokenReview proves.
func (r *Resolver) ResolveSubject(ctx context.Context) (Subject, error) {
	if subject, secured, err := r.resolveSVIDSubject(ctx); secured {
		return subject, err
	}
	return r.Resolve(ctx)
}

// ResolveRequest uses the HTTP listener's verified TLS peer and the same
// authentication selection as native RPCs. Identity is never read from headers.
func (r *Resolver) ResolveRequest(request *http.Request) (Principal, error) {
	return r.Resolve(requestContext(request))
}

// ResolveSubjectRequest is ResolveSubject for an HTTP request.
func (r *Resolver) ResolveSubjectRequest(request *http.Request) (Subject, error) {
	return r.ResolveSubject(requestContext(request))
}

func requestContext(request *http.Request) context.Context {
	ctx := metadata.NewIncomingContext(request.Context(), metadata.MD{
		"authorization":       request.Header.Values("Authorization"),
		DelegationMetadataKey: request.Header.Values(DelegationMetadataKey),
	})
	if request.TLS != nil {
		ctx = peer.NewContext(ctx, &peer.Peer{AuthInfo: credentials.TLSInfo{State: *request.TLS}})
	}
	return ctx
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
