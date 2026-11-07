// Package workloadfacade preserves a service-verified TLS peer through its
// generated HTTP facade without forwarding an identity over any socket or header.
package workloadfacade

import (
	"context"
	"crypto/x509"
	"errors"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/fullstorydev/grpchan/inprocgrpc"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

// Registration binds one service implementation and its generated HandlerClient.
type Registration struct {
	Server func(grpc.ServiceRegistrar)
	HTTP   func(context.Context, *runtime.ServeMux, grpc.ClientConnInterface) error
}

// Interceptors are the service's existing admission boundary, shared with native
// gRPC. Custom gates and auth.Methods retain their own authorization semantics.
type Interceptors struct {
	Unary  grpc.UnaryServerInterceptor
	Stream grpc.StreamServerInterceptor
}

// NewHandler is for the service's own workload TLS listener. The listener must
// use WorkloadServerTLSConfig. Its verified peer lives only in a typed context;
// the in-process channel runs the same method interceptors as native gRPC,
// including streaming admission and revalidation. Backend token listeners retain
// their existing facade and transport.
func NewHandler(ctx context.Context, interceptors Interceptors, options facade.HandlerOptions, registration Registration) (http.Handler, error) {
	return newHandler(ctx, interceptors, options, registration, false)
}

// NewHandlerWithOptionalClientCertificate is for a TLS listener whose auth policy
// explicitly permits a bearer without a client certificate. A certificate, when
// present, has already passed the listener's TLS verifier and is preserved for
// the same method interceptors; the configured auth resolver must choose or
// reject the presented credentials before admitting the call.
func NewHandlerWithOptionalClientCertificate(ctx context.Context, interceptors Interceptors, options facade.HandlerOptions, registration Registration) (http.Handler, error) {
	return newHandler(ctx, interceptors, options, registration, true)
}

func newHandler(ctx context.Context, interceptors Interceptors, options facade.HandlerOptions, registration Registration, allowMissingClientCertificate bool) (http.Handler, error) {
	if interceptors.Unary == nil || interceptors.Stream == nil || registration.Server == nil || registration.HTTP == nil {
		return nil, errors.New("workload facade requires unary and stream admission and both registrations")
	}
	channel := (&inprocgrpc.Channel{}).
		WithServerUnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			admitted, err := withHTTPPeer(ctx)
			if err != nil {
				return nil, err
			}
			return interceptors.Unary(admitted, request, info, handler)
		}).
		WithServerStreamInterceptor(func(server any, incoming grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			ctx, err := withHTTPPeer(incoming.Context())
			if err != nil {
				return err
			}
			return interceptors.Stream(server, contextStream{ServerStream: incoming, ctx: ctx}, info, handler)
		})
	registration.Server(channel)
	handler, err := facade.NewLocalHandler(ctx, options, func(ctx context.Context, mux *runtime.ServeMux) error {
		return registration.HTTP(ctx, mux, channel)
	})
	if err != nil {
		return nil, err
	}
	return http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.TLS == nil {
			http.Error(w, "workload client TLS required", http.StatusUnauthorized)
			return
		}
		if len(request.TLS.PeerCertificates) == 0 && !allowMissingClientCertificate {
			http.Error(w, "workload client TLS certificate required", http.StatusUnauthorized)
			return
		}
		if len(request.TLS.PeerCertificates) > 0 {
			if _, err := environment_runtime_transport.PeerIdentityFromTLSState(*request.TLS, x509.ExtKeyUsageClientAuth); err != nil {
				http.Error(w, "invalid workload client certificate", http.StatusUnauthorized)
				return
			}
		}
		tlsPeer := &peer.Peer{AuthInfo: credentials.TLSInfo{State: *request.TLS, CommonAuthInfo: credentials.CommonAuthInfo{SecurityLevel: credentials.PrivacyAndIntegrity}}}
		handler.ServeHTTP(w, request.WithContext(peer.NewContext(request.Context(), tlsPeer)))
	}), nil
}

func withHTTPPeer(ctx context.Context) (context.Context, error) {
	original := inprocgrpc.ClientContext(ctx)
	if original == nil {
		return nil, status.Error(codes.Unauthenticated, "HTTP TLS context missing")
	}
	caller, ok := peer.FromContext(original)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "HTTP TLS peer missing")
	}
	if _, ok := caller.AuthInfo.(credentials.TLSInfo); !ok {
		return nil, status.Error(codes.Unauthenticated, "HTTP TLS peer missing")
	}
	return peer.NewContext(ctx, caller), nil
}

type contextStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s contextStream) Context() context.Context { return s.ctx }
