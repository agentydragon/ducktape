package gateway

import (
	"context"
	"crypto/subtle"
	"strings"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// GRPCServer serves the same management service using a dedicated owner bearer.
// The caller must serve it over TLS or a trusted local transport.
func GRPCServer(service *Service, ownerToken string, options ...grpc.ServerOption) *grpc.Server {
	check := func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		values := metadata.ValueFromIncomingContext(ctx, "authorization")
		if len(values) != 1 || len(ownerToken) < 32 {
			return nil, status.Error(codes.Unauthenticated, "owner authentication required")
		}
		scheme, token, ok := strings.Cut(values[0], " ")
		if !ok || !strings.EqualFold(scheme, "Bearer") || subtle.ConstantTimeCompare([]byte(token), []byte(ownerToken)) != 1 {
			return nil, status.Error(codes.Unauthenticated, "owner authentication required")
		}
		return handler(ctx, req)
	}
	options = append(options, grpc.MaxRecvMsgSize(maxSessionBytes*2), grpc.ChainUnaryInterceptor(check))
	server := grpc.NewServer(options...)
	managementpb.RegisterSessionManagementServer(server, service)
	return server
}
