package auth

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
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

// ResolveSubject authenticates a gRPC TLS peer. The listener must use the transport
// package's workload server credentials, which verify its chain and client EKU.
func (r *SVIDResolver) ResolveSubject(ctx context.Context) (Subject, error) {
	// This is a direct workload boundary. Service delegation is admitted by the method
	// rule before reaching this resolver; headers never supplement a workload SVID.
	if hasDirectBearerCredential(ctx) || hasDelegationCredential(ctx) {
		return nil, status.Error(codes.Unauthenticated, "ambiguous workload credentials")
	}
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
	return IdentityPrincipal{
		Identity: Identity{Name: resolved.Subject.Name, UID: string(resolved.Subject.UID)}, Workload: WorkloadOf(resolved),
		PodName: podName, PodUID: podUID,
	}, nil
}

// WorkloadOf is the provenance a resolved workload's record carries.
func WorkloadOf(peer resolver.Peer) Workload {
	workload := Workload{Namespace: peer.Workload.Namespace, Name: peer.Workload.Name, Owner: NoController{}}
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

// requestContext carries an HTTP request's credentials as the gRPC middleware reads them. The
// delegation header is base64 on the wire (EncodeDelegationHeader), as gRPC's own `-bin` transport.
func requestContext(request *http.Request) context.Context {
	envelopes := request.Header.Values(DelegationMetadataKey)
	decoded := make([]string, len(envelopes))
	for index, envelope := range envelopes {
		decoded[index] = decodeDelegationHeader(envelope)
	}
	ctx := metadata.NewIncomingContext(request.Context(), metadata.MD{
		"authorization":       request.Header.Values("Authorization"),
		DelegationMetadataKey: decoded,
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
