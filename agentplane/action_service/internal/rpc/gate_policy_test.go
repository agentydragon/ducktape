package rpc

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func TestHealthAndSingleUseLinkageMethodsReachTheirOwnHandlersWithoutBearer(t *testing.T) {
	s := serve(t)
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	health, err := s.health.Check(ctx, &healthpb.HealthCheckRequest{})
	if err != nil || health.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("unauthenticated health check returned %v, %v", health, err)
	}
	_, err = s.linkageRPC.CompleteCallback(ctx, &linkage_servicepb.CompleteCallbackRequest{})
	wantCode(t, "the public linkage callback", err, codes.InvalidArgument)
	_, err = s.linkageRPC.GetClientMetadata(ctx, &linkage_servicepb.GetClientMetadataRequest{})
	wantCode(t, "the public metadata document", err, codes.NotFound)
}
