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
	readers map[string]*sandboxreader.Reader
}

func NewSVIDResolver(objects client.Reader, namespaces []string) (*SVIDResolver, error) {
	if objects == nil || len(namespaces) == 0 {
		return nil, errors.New("Sandbox reader and allowed namespaces are required")
	}
	readers := make(map[string]*sandboxreader.Reader, len(namespaces))
	for _, namespace := range namespaces {
		if namespace == "" {
			return nil, errors.New("allowed namespace must not be empty")
		}
		reader, err := sandboxreader.New(namespace, objects)
		if err != nil {
			return nil, err
		}
		readers[namespace] = reader
	}
	return &SVIDResolver{readers: readers}, nil
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
	reader := r.readers[namespace]
	if reader == nil {
		return Principal{}, ErrInvalidBearer
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
