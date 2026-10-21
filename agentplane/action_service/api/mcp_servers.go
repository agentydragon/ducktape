package api

import (
	"context"
	"errors"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

// The MCP server RPCs: each configured server's OAuth linkage, which the linkage Authority holds.

func (s *OperatorService) ListMcpServers(ctx context.Context, _ *operator_action_servicepb.ListMcpServersRequest) (*operator_action_servicepb.ListMcpServersResponse, error) {
	linkages, err := s.linkages.Linkages(ctx)
	if err != nil {
		return nil, err
	}
	return &operator_action_servicepb.ListMcpServersResponse{Linkages: linkages}, nil
}

func (s *OperatorService) GetMcpServerLinkage(ctx context.Context, request *operator_action_servicepb.GetMcpServerLinkageRequest) (*operator_action_servicepb.GetMcpServerLinkageResponse, error) {
	found, err := s.linkages.Linkage(ctx, request.GetServerId())
	if err != nil {
		return nil, err
	}
	return &operator_action_servicepb.GetMcpServerLinkageResponse{Linkage: found}, nil
}

// StartMcpServerLinkage begins a link for the calling operator, whom the linkage then names as its linker.
func (s *OperatorService) StartMcpServerLinkage(ctx context.Context, request *operator_action_servicepb.StartMcpServerLinkageRequest) (*operator_action_servicepb.StartMcpServerLinkageResponse, error) {
	// The Gate admits only operators to this service.
	operator := admitted(ctx).principal.(store.OperatorPrincipal)
	return s.linkages.Start(ctx, request.GetServerId(), request.GetScopes(), operator)
}

func (s *OperatorService) DisconnectMcpServer(ctx context.Context, request *operator_action_servicepb.DisconnectMcpServerRequest) (*operator_action_servicepb.DisconnectMcpServerResponse, error) {
	disconnected, err := s.linkages.Disconnect(ctx, request.GetServerId())
	if err != nil {
		return nil, err
	}
	return &operator_action_servicepb.DisconnectMcpServerResponse{Linkage: disconnected}, nil
}

// WatchMcpServers sends the linkages, then an upsert of each whose view changes: after a commit to a linkage, and once
// a linked token reaches its expiry, which changes its status with no commit.
func (s *OperatorService) WatchMcpServers(_ *operator_action_servicepb.WatchMcpServersRequest, stream grpc.ServerStreamingServer[operator_action_servicepb.WatchMcpServersResponse]) error {
	ctx := stream.Context()
	// The linkages are read after the subscription, so a commit in between is not missed.
	updates, unsubscribe, err := s.updates.Subscribe(linkage.UpdatesChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	// shown is each linkage as the client holds it, by server ID; nil until the snapshot is sent.
	var shown map[string]*operator_action_servicepb.McpServerLinkage
	for {
		linkages, err := s.linkages.Linkages(ctx)
		if err != nil {
			return err
		}
		if shown == nil {
			if err := s.sendLinkageSnapshot(ctx, stream, linkages); err != nil {
				return err
			}
			shown = make(map[string]*operator_action_servicepb.McpServerLinkage, len(linkages))
		}
		for _, current := range linkages {
			if held, sent := shown[current.GetServerId()]; sent && !proto.Equal(held, current) {
				if err := send(ctx, s.gate, stream, &operator_action_servicepb.WatchMcpServersResponse{
					Frame: &operator_action_servicepb.WatchMcpServersResponse_Upsert{Upsert: current},
				}); err != nil {
					return err
				}
			}
			shown[current.GetServerId()] = current
		}
		due, stop := nextExpiry(linkages)
		err = (follower{gate: s.gate, updates: updates, due: due}).changed(ctx)
		stop()
		if errors.Is(err, errDraining) {
			// A planned renewal, as for WatchActionRequests.
			return send(ctx, s.gate, stream, &operator_action_servicepb.WatchMcpServersResponse{
				Frame: &operator_action_servicepb.WatchMcpServersResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
			})
		}
		if err != nil {
			return err
		}
	}
}

// sendLinkageSnapshot sends the linkages as snapshot frames of at most snapshotChunk linkages, then
// snapshot_complete.
func (s *OperatorService) sendLinkageSnapshot(ctx context.Context, stream grpc.ServerStreamingServer[operator_action_servicepb.WatchMcpServersResponse], linkages []*operator_action_servicepb.McpServerLinkage) error {
	for chunk := range slices.Chunk(linkages, snapshotChunk) {
		if err := send(ctx, s.gate, stream, &operator_action_servicepb.WatchMcpServersResponse{
			Frame: &operator_action_servicepb.WatchMcpServersResponse_Snapshot{Snapshot: &operator_action_servicepb.McpServerLinkageChunk{Linkages: chunk}},
		}); err != nil {
			return err
		}
	}
	return send(ctx, s.gate, stream, &operator_action_servicepb.WatchMcpServersResponse{
		Frame: &operator_action_servicepb.WatchMcpServersResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
	})
}

// nextExpiry fires when the first token of a linked linkage expires, until stopped; nil when none is linked with an
// expiry.
func nextExpiry(linkages []*operator_action_servicepb.McpServerLinkage) (due <-chan time.Time, stop func() bool) {
	var first time.Time
	for _, current := range linkages {
		if current.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED || current.GetExpiresAt() == nil {
			continue
		}
		if at := current.GetExpiresAt().AsTime(); first.IsZero() || at.Before(first) {
			first = at
		}
	}
	if first.IsZero() {
		return nil, func() bool { return false }
	}
	expiry := time.NewTimer(time.Until(first))
	return expiry.C, expiry.Stop
}
