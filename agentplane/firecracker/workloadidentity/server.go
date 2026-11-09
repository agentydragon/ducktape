package workloadidentity

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const maxPayloadBytes = 1 << 20

const workloadMetadataKey = "workload.spiffe.io"

// Server serves one EnvironmentRuntime X.509 identity and its trust bundles.
// The caller controls the listener and any transport-level authorization.
type Server struct {
	workload.UnimplementedSpiffeWorkloadAPIServer

	mu          sync.Mutex
	identity    string
	snapshot    *workload.X509SVIDResponse
	subscribers map[*subscriber]struct{}
}

type subscriber struct {
	updates chan *workload.X509SVIDResponse
}

var _ workload.SpiffeWorkloadAPIServer = (*Server)(nil)

// New creates a Workload API server with no identity loaded.
func New() *Server {
	return &Server{subscribers: make(map[*subscriber]struct{})}
}

// Set validates and atomically replaces the complete X.509 identity snapshot.
// The identity is pinned on the first successful call and must remain stable
// across later certificate rotations.
func (s *Server) Set(response *workload.X509SVIDResponse) error {
	if response == nil {
		return errors.New("X.509 SVID response is nil")
	}
	candidate := proto.Clone(response).(*workload.X509SVIDResponse)
	identity, err := validateResponse(candidate)
	if err != nil {
		return err
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.identity != "" && s.identity != identity {
		return fmt.Errorf("EnvironmentRuntime SPIFFE ID changed from %q to %q", s.identity, identity)
	}
	if s.snapshot != nil && proto.Equal(s.snapshot, candidate) {
		return nil
	}
	s.identity = identity
	s.snapshot = candidate
	for subscriber := range s.subscribers {
		replacePending(subscriber.updates, proto.Clone(candidate).(*workload.X509SVIDResponse))
	}
	return nil
}

// Snapshot returns a detached copy of the current complete X.509 response.
func (s *Server) Snapshot() (*workload.X509SVIDResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.snapshot == nil {
		return nil, errors.New("X.509 identity has not been set")
	}
	return proto.Clone(s.snapshot).(*workload.X509SVIDResponse), nil
}

// FetchX509SVID streams the current identity and every later complete rotation.
func (s *Server) FetchX509SVID(_ *workload.X509SVIDRequest, stream workload.SpiffeWorkloadAPI_FetchX509SVIDServer) error {
	return s.stream(stream.Context(), func(response *workload.X509SVIDResponse) error {
		if err := validateForServing(response); err != nil {
			return err
		}
		return stream.Send(proto.Clone(response).(*workload.X509SVIDResponse))
	})
}

// FetchX509Bundles streams the current complete bundle set and every later
// rotation. Each local and federated bundle comes from the same atomic snapshot
// as the X.509 SVID stream.
func (s *Server) FetchX509Bundles(_ *workload.X509BundlesRequest, stream workload.SpiffeWorkloadAPI_FetchX509BundlesServer) error {
	return s.stream(stream.Context(), func(response *workload.X509SVIDResponse) error {
		if err := validateForServing(response); err != nil {
			return err
		}
		return stream.Send(x509BundlesResponse(response))
	})
}

func (s *Server) stream(ctx context.Context, send func(*workload.X509SVIDResponse) error) error {
	if err := requireWorkloadMetadata(ctx); err != nil {
		return err
	}
	updates, unsubscribe, err := s.subscribe()
	if err != nil {
		return status.Error(codes.Unavailable, err.Error())
	}
	defer unsubscribe()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case response := <-updates:
			if err := send(response); err != nil {
				return err
			}
		}
	}
}

func (s *Server) subscribe() (<-chan *workload.X509SVIDResponse, func(), error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.snapshot == nil {
		return nil, nil, errors.New("X.509 identity has not been set")
	}
	subscriber := &subscriber{updates: make(chan *workload.X509SVIDResponse, 1)}
	subscriber.updates <- proto.Clone(s.snapshot).(*workload.X509SVIDResponse)
	s.subscribers[subscriber] = struct{}{}
	return subscriber.updates, func() {
		s.mu.Lock()
		delete(s.subscribers, subscriber)
		s.mu.Unlock()
	}, nil
}

func replacePending(updates chan *workload.X509SVIDResponse, response *workload.X509SVIDResponse) {
	select {
	case updates <- response:
		return
	default:
	}
	select {
	case <-updates:
	default:
	}
	updates <- response
}

func requireWorkloadMetadata(ctx context.Context) error {
	incoming, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return status.Error(codes.InvalidArgument, "missing workload.spiffe.io: true metadata")
	}
	values := incoming.Get(workloadMetadataKey)
	if len(values) != 1 || values[0] != "true" {
		return status.Error(codes.InvalidArgument, "missing workload.spiffe.io: true metadata")
	}
	return nil
}

func validateForServing(response *workload.X509SVIDResponse) error {
	if _, err := validateResponse(response); err != nil {
		return status.Errorf(codes.Unavailable, "current X.509 identity is invalid: %v", err)
	}
	return nil
}

func validateResponse(response *workload.X509SVIDResponse) (string, error) {
	if size := proto.Size(response); size > maxPayloadBytes {
		return "", fmt.Errorf("X.509 SVID response is %d bytes, over the %d-byte limit", size, maxPayloadBytes)
	}
	if len(response.GetSvids()) != 1 {
		return "", fmt.Errorf("X.509 SVID response must contain exactly one SVID; got %d", len(response.GetSvids()))
	}
	entry := response.GetSvids()[0]
	if entry == nil || entry.GetSpiffeId() == "" || len(entry.GetX509Svid()) == 0 || len(entry.GetX509SvidKey()) == 0 || len(entry.GetBundle()) == 0 {
		return "", errors.New("X.509 SVID response is missing a required SVID field")
	}
	if err := validateEnvironmentRuntimeID(entry.GetSpiffeId()); err != nil {
		return "", err
	}

	svid, err := x509svid.ParseRaw(entry.GetX509Svid(), entry.GetX509SvidKey())
	if err != nil {
		return "", fmt.Errorf("parse X.509 SVID and private key: %w", err)
	}
	if svid.ID.String() != entry.GetSpiffeId() {
		return "", fmt.Errorf("X.509 SVID ID %q does not match response ID %q", svid.ID, entry.GetSpiffeId())
	}
	if err := requireKeyUsage(svid.Certificates[0], x509.ExtKeyUsageServerAuth); err != nil {
		return "", fmt.Errorf("X.509 SVID server usage: %w", err)
	}

	trustDomain := svid.ID.TrustDomain()
	bundle, err := x509bundle.ParseRaw(trustDomain, entry.GetBundle())
	if err != nil {
		return "", fmt.Errorf("parse X.509 SVID bundle: %w", err)
	}
	verifiedID, _, err := x509svid.Verify(svid.Certificates, bundle)
	if err != nil {
		return "", fmt.Errorf("verify X.509 SVID chain: %w", err)
	}
	if verifiedID != svid.ID {
		return "", fmt.Errorf("verified X.509 SVID ID %q does not match response ID %q", verifiedID, entry.GetSpiffeId())
	}

	for trustDomainID, rawBundle := range response.GetFederatedBundles() {
		federatedTrustDomain, err := spiffeid.TrustDomainFromString(trustDomainID)
		if err != nil || federatedTrustDomain.IDString() != trustDomainID {
			return "", fmt.Errorf("federated bundle key %q is not a canonical SPIFFE trust-domain ID", trustDomainID)
		}
		if federatedTrustDomain == trustDomain {
			return "", fmt.Errorf("federated bundle duplicates local trust domain %q", trustDomainID)
		}
		if len(rawBundle) == 0 {
			return "", fmt.Errorf("federated bundle for %q is empty", trustDomainID)
		}
		if _, err := x509bundle.ParseRaw(federatedTrustDomain, rawBundle); err != nil {
			return "", fmt.Errorf("parse federated bundle for %q: %w", trustDomainID, err)
		}
	}
	for index, rawCRL := range response.GetCrl() {
		if _, err := x509.ParseRevocationList(rawCRL); err != nil {
			return "", fmt.Errorf("parse CRL %d: %w", index, err)
		}
	}
	return entry.GetSpiffeId(), nil
}

func validateEnvironmentRuntimeID(rawID string) error {
	if _, err := environment_runtime_transport.ParseWorkloadURI(rawID); err != nil {
		return fmt.Errorf("invalid workload SPIFFE ID %q: %w", rawID, err)
	}
	return nil
}

func requireKeyUsage(certificate *x509.Certificate, required x509.ExtKeyUsage) error {
	for _, usage := range certificate.ExtKeyUsage {
		if usage == required {
			return nil
		}
	}
	return fmt.Errorf("certificate does not allow %v", required)
}

func x509BundlesResponse(response *workload.X509SVIDResponse) *workload.X509BundlesResponse {
	entry := response.GetSvids()[0]
	id, _ := spiffeid.FromString(entry.GetSpiffeId())
	bundles := make(map[string][]byte, len(response.GetFederatedBundles())+1)
	bundles[id.TrustDomain().IDString()] = append([]byte(nil), entry.GetBundle()...)
	for trustDomainID, rawBundle := range response.GetFederatedBundles() {
		bundles[trustDomainID] = append([]byte(nil), rawBundle...)
	}
	crls := make([][]byte, len(response.GetCrl()))
	for index, rawCRL := range response.GetCrl() {
		crls[index] = append([]byte(nil), rawCRL...)
	}
	return &workload.X509BundlesResponse{Bundles: bundles, Crl: crls}
}
