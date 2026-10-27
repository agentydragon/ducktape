package api

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/structpb"
)

// The catalog reads answer every principal alike, but for McpHealth.detail: it can name backend addresses, so only an
// operator's view carries it.

// errCatalogAction is an Action of the catalog no message can show: a fault of the service, not of the caller.
var errCatalogAction = errors.New("catalog Action cannot be shown")

// showsHealthDetail is whether the call's principal reads McpHealth.detail.
func showsHealthDetail(ctx context.Context) bool {
	_, operator := admitted(ctx).principal.(store.OperatorPrincipal)
	return operator
}

func actionView(group, name string, action catalog.ActionDefinition) (*action_servicepb.Action, error) {
	schema := &structpb.Struct{}
	if err := schema.UnmarshalJSON(action.InputSchema); err != nil {
		return nil, fmt.Errorf("%w: %s/%s input schema: %w", errCatalogAction, group, name, err)
	}
	view := &action_servicepb.Action{
		Group: group, Name: name, Description: action.Description, InputSchema: schema, Title: action.Title,
	}
	if hints := action.Annotations; hints != nil {
		view.Annotations = &action_servicepb.ToolAnnotations{
			ReadOnlyHint: hints.ReadOnlyHint, DestructiveHint: hints.DestructiveHint,
			IdempotentHint: hints.IdempotentHint, OpenWorldHint: hints.OpenWorldHint,
		}
	}
	return view, nil
}

// groupView is the group with its Actions in name order; an unavailable group lists none, and so no direct tool.
func groupView(key string, group *catalog.Group, healthDetail bool) (*action_servicepb.ActionGroup, error) {
	view := &action_servicepb.ActionGroup{
		Key: key, Title: group.Title, Description: group.Description, Available: group.Available,
	}
	switch executor := group.Executor.(type) {
	case catalog.MCPBinding:
		view.ExecutorKind, view.ExecutorDescription = action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP, executor.Description
	case catalog.PodBinding:
		view.ExecutorKind, view.ExecutorDescription = action_servicepb.ExecutorKind_EXECUTOR_KIND_POD, executor.Description
	case catalog.OrchestrationBinding:
		view.ExecutorKind = action_servicepb.ExecutorKind_EXECUTOR_KIND_ORCHESTRATION_V1
		view.ExecutorDescription = executor.Description
	}
	if group.Health != nil {
		view.Health = proto.CloneOf(group.Health)
		if !healthDetail {
			view.Health.Detail = nil
		}
	}
	if !group.Available {
		return view, nil
	}
	for _, name := range slices.Sorted(maps.Keys(group.Actions)) {
		action, err := actionView(key, name, group.Actions[name])
		if err != nil {
			return nil, err
		}
		view.Actions = append(view.Actions, action)
		if _, direct := group.DirectTools[name]; direct {
			view.DirectTools = append(view.DirectTools, name)
		}
	}
	return view, nil
}

// groupViews are the catalog's groups in key order.
func groupViews(groups *catalog.Catalog, healthDetail bool) ([]*action_servicepb.ActionGroup, error) {
	var views []*action_servicepb.ActionGroup
	for key, group := range groups.Groups() {
		view, err := groupView(key, group, healthDetail)
		if err != nil {
			return nil, err
		}
		views = append(views, view)
	}
	return views, nil
}

func (s *Service) ListActionGroups(ctx context.Context, _ *action_servicepb.ListActionGroupsRequest) (*action_servicepb.ListActionGroupsResponse, error) {
	views, err := groupViews(s.catalog, showsHealthDetail(ctx))
	if err != nil {
		return nil, err
	}
	return &action_servicepb.ListActionGroupsResponse{ActionGroups: views}, nil
}

func (s *Service) GetAction(_ context.Context, request *action_servicepb.GetActionRequest) (*action_servicepb.GetActionResponse, error) {
	group, name := request.GetGroupKey(), request.GetActionKey()
	_, definition, err := s.catalog.Resolve(group, name)
	if err != nil {
		return nil, err
	}
	view, err := actionView(group, name, definition)
	if err != nil {
		return nil, err
	}
	return &action_servicepb.GetActionResponse{Action: view}, nil
}

// WatchActionGroups sends the MCP groups, then an upsert of each whose view changes. A change to what the caller does
// not see, such as a workload's view of McpHealth.detail, sends nothing.
func (s *Service) WatchActionGroups(_ *action_servicepb.WatchActionGroupsRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionGroupsResponse]) error {
	ctx := stream.Context()
	healthDetail := showsHealthDetail(ctx)
	// shown is each group as the client holds it; nil until the snapshot is sent.
	var shown map[string]*action_servicepb.ActionGroup
	for {
		changed := s.catalog.Changed()
		views, err := groupViews(s.catalog, healthDetail)
		if err != nil {
			return err
		}
		mcp := slices.DeleteFunc(views, func(view *action_servicepb.ActionGroup) bool {
			return view.GetExecutorKind() != action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP
		})
		if shown == nil {
			if err := s.sendGroupSnapshot(ctx, stream, mcp); err != nil {
				return err
			}
			shown = make(map[string]*action_servicepb.ActionGroup, len(mcp))
			for _, view := range mcp {
				shown[view.GetKey()] = view
			}
		} else {
			for _, view := range mcp {
				if proto.Equal(shown[view.GetKey()], view) {
					continue
				}
				if err := send(ctx, s.gate, stream, &action_servicepb.WatchActionGroupsResponse{
					Frame: &action_servicepb.WatchActionGroupsResponse_Upsert{Upsert: view},
				}); err != nil {
					return err
				}
				shown[view.GetKey()] = view
			}
		}
		if err := (follower{gate: s.gate, updates: changed}).changed(ctx); err != nil {
			if errors.Is(err, errDraining) {
				// A planned renewal, as for WatchActionRequests.
				return send(ctx, s.gate, stream, &action_servicepb.WatchActionGroupsResponse{
					Frame: &action_servicepb.WatchActionGroupsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
				})
			}
			return err
		}
	}
}

// sendGroupSnapshot sends the groups as snapshot frames of at most snapshotChunk groups, then snapshot_complete.
func (s *Service) sendGroupSnapshot(ctx context.Context, stream grpc.ServerStreamingServer[action_servicepb.WatchActionGroupsResponse], groups []*action_servicepb.ActionGroup) error {
	for chunk := range slices.Chunk(groups, snapshotChunk) {
		if err := send(ctx, s.gate, stream, &action_servicepb.WatchActionGroupsResponse{
			Frame: &action_servicepb.WatchActionGroupsResponse_Snapshot{Snapshot: &action_servicepb.ActionGroupChunk{ActionGroups: chunk}},
		}); err != nil {
			return err
		}
	}
	return send(ctx, s.gate, stream, &action_servicepb.WatchActionGroupsResponse{
		Frame: &action_servicepb.WatchActionGroupsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
	})
}
