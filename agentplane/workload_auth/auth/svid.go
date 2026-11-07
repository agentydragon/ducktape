package auth

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// SVIDResolver resolves an already verified workload leaf to the Sandbox's own
// ServiceAccount. Its reader must read the API server directly: a certificate
// never preserves the authority of a deleted Sandbox or an old credential.
type SVIDResolver struct {
	objects    client.Reader
	namespaces map[string]struct{}
}

func NewSVIDResolver(objects client.Reader, namespaces []string) (*SVIDResolver, error) {
	if objects == nil || len(namespaces) == 0 {
		return nil, errors.New("Sandbox reader and allowed namespaces are required")
	}
	allowed := make(map[string]struct{}, len(namespaces))
	for _, namespace := range namespaces {
		if namespace == "" {
			return nil, errors.New("allowed namespace must not be empty")
		}
		allowed[namespace] = struct{}{}
	}
	return &SVIDResolver{objects: objects, namespaces: allowed}, nil
}

// NewKubernetesSVIDResolver creates an uncached reader for a service's SVID boundary.
func NewKubernetesSVIDResolver(config *rest.Config, namespaces []string) (*SVIDResolver, error) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		return nil, err
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return nil, err
	}
	objects, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		return nil, err
	}
	return NewSVIDResolver(objects, namespaces)
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

// ResolveRequest authenticates an HTTP request whose TLS listener verified the
// client chain. It derives identity only from the verified peer certificate;
// request headers and bodies cannot select a principal.
func (r *SVIDResolver) ResolveRequest(request *http.Request) (Principal, error) {
	if request == nil || request.TLS == nil || len(request.TLS.PeerCertificates) == 0 {
		return Principal{}, ErrInvalidBearer
	}
	if _, err := environment_runtime_transport.PeerIdentityFromTLSState(*request.TLS, x509.ExtKeyUsageClientAuth); err != nil {
		return Principal{}, ErrInvalidBearer
	}
	return r.ResolveVerifiedLeaf(request.Context(), request.TLS.PeerCertificates[0])
}

// ResolveVerifiedLeaf takes a leaf whose signature and trust chain the TLS
// listener has verified. It rechecks validity and the live Sandbox on each call.
// Headers, metadata and request bodies never select the subject.
func (r *SVIDResolver) ResolveVerifiedLeaf(ctx context.Context, leaf *x509.Certificate) (Principal, error) {
	if leaf == nil || time.Now().Before(leaf.NotBefore) || time.Now().After(leaf.NotAfter) {
		return Principal{}, ErrInvalidBearer
	}
	clientUsage := false
	for _, usage := range leaf.ExtKeyUsage {
		clientUsage = clientUsage || usage == x509.ExtKeyUsageClientAuth
	}
	if !clientUsage {
		return Principal{}, ErrInvalidBearer
	}
	id, err := x509svid.IDFromCert(leaf)
	if err != nil {
		return Principal{}, ErrInvalidBearer
	}
	namespace, uid, credentialID, err := environment_runtime_transport.ParseEnvironmentRuntimeURI(id.String())
	if err != nil {
		return Principal{}, ErrInvalidBearer
	}
	if _, allowed := r.namespaces[namespace]; !allowed {
		return Principal{}, ErrInvalidBearer
	}
	reader, err := sandboxreader.New(namespace, r.objects)
	if err != nil {
		return Principal{}, err
	}
	view, account, found, err := reader.RunnerCredentialAccount(ctx, uid, credentialID)
	if err != nil {
		return Principal{}, fmt.Errorf("%w: read Sandbox credential: %w", ErrUnavailable, err)
	}
	if !found {
		return Principal{}, ErrInvalidBearer
	}
	podName, podUID, bound := view.RunnerPod()
	if !bound {
		return Principal{}, ErrInvalidBearer
	}
	return Principal{Account: Account{Namespace: namespace, Name: view.Resource.Name}, ServiceAccountUID: account.GetUid(), PodName: podName, PodUID: podUID}, nil
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

// WithSVID adds certificate authentication to the direct caller path. The
// existing token resolver and its delegation policy still serve backend callers.
// A TLS peer is always resolved by its certificate; no bearer can replace it.
func (r *Resolver) WithSVID(svid *SVIDResolver) *Resolver {
	copy := *r
	copy.svid = svid
	return &copy
}

// WithListenerAuthentication binds this resolver to the explicit authentication modes on one
// listener. A bearer mode must use this Resolver's audience; certificate authentication requires
// a live Sandbox resolver. Callers and methods remain the responsibility of the existing Rules.
func (r *Resolver) WithListenerAuthentication(config listener.Config, svid *SVIDResolver) (*Resolver, error) {
	if err := config.Validate(); err != nil {
		return nil, fmt.Errorf("listener authentication: %w", err)
	}
	if config.Has(listener.SPIFFE) && svid == nil {
		return nil, errors.New("SVID resolver is required when SPIFFE authentication is enabled")
	}
	if config.Has(listener.KubernetesBearer) && config.BearerAudience() != r.audience {
		return nil, errors.New("listener bearer audience must match the TokenReview resolver audience")
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
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return false
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	if !ok {
		return false
	}
	if r.listenerAuth == nil {
		return r.svid != nil
	}
	return len(info.State.PeerCertificates) > 0
}

func (r *Resolver) resolveSVID(ctx context.Context) (Principal, bool, error) {
	remote, ok := peer.FromContext(ctx)
	var info credentials.TLSInfo
	if ok {
		info, ok = remote.AuthInfo.(credentials.TLSInfo)
	}
	if r.listenerAuth != nil {
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
	if r.svid == nil || !ok {
		return Principal{}, false, nil
	}
	principal, err := r.svid.Resolve(ctx)
	return principal, true, err
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
