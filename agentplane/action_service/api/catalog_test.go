package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// testCatalog holds an MCP group whose direct tools name an Action it offers and one it does not, an MCP group that
// is unavailable, and a sandbox group.
const testCatalog = `{
	"test-mcp": {"title": "test MCP group", "description": "test MCP group description",
		"executor": {"kind": "mcp", "description": "test MCP executor", "config": {"url": "https://mcp.test.example/mcp"}},
		"actions": {
			"test-read": {"description": "test read description", "title": "Test read",
				"input_schema": {"type": "object", "properties": {"test-path": {"type": "string"}}, "required": ["test-path"]},
				"annotations": {"readOnlyHint": true, "openWorldHint": false}},
			"test-list": {"description": "test list description"},
			"test-delete": {"description": "test delete description"}},
		"direct_tools": ["test-read", "test-undiscovered"]},
	"test-offline": {"title": "test offline group", "description": "test offline group description",
		"executor": {"kind": "mcp", "description": "test offline executor"}, "available": false,
		"actions": {"test-read": {"description": "test read description"}}, "direct_tools": ["test-read"]},
	"test-sandbox": {"title": "test sandbox group", "description": "test sandbox group description",
		"executor": {"kind": "pod", "description": "test pod executor", "namespace": "test-namespace",
			"templates": {"test-template": "a test box"}}}
}`

// offlineHealth is the test-offline group's connection as an MCP executor would report it.
func offlineHealth(detail string) *action_servicepb.McpHealth {
	return &action_servicepb.McpHealth{
		State:    action_servicepb.McpLifecycle_MCP_LIFECYCLE_DISCONNECTED,
		Reason:   action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_CONNECT_FAILED.Enum(),
		Detail:   &detail,
		RetryAt:  timestamppb.New(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)),
		Failures: 3,
	}
}

func testReadAction(t *testing.T) *action_servicepb.Action {
	t.Helper()
	schema, err := structpb.NewStruct(map[string]any{
		"type": "object", "properties": map[string]any{"test-path": map[string]any{"type": "string"}}, "required": []any{"test-path"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return &action_servicepb.Action{
		Group: "test-mcp", Name: "test-read", Description: "test read description", InputSchema: schema,
		Title:       proto.String("Test read"),
		Annotations: &action_servicepb.ToolAnnotations{ReadOnlyHint: proto.Bool(true), OpenWorldHint: proto.Bool(false)},
	}
}

// testCatalogViews are the groups of testCatalog, with the test-offline group's health, as an operator reads them.
func testCatalogViews(t *testing.T, health *action_servicepb.McpHealth) []*action_servicepb.ActionGroup {
	t.Helper()
	return []*action_servicepb.ActionGroup{
		{
			Key: "test-mcp", Title: "test MCP group", Description: "test MCP group description",
			ExecutorKind: action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP, ExecutorDescription: "test MCP executor",
			Available: true,
			Actions: []*action_servicepb.Action{
				{Group: "test-mcp", Name: "test-delete", Description: "test delete description", InputSchema: &structpb.Struct{}},
				{Group: "test-mcp", Name: "test-list", Description: "test list description", InputSchema: &structpb.Struct{}},
				testReadAction(t),
			},
			// The group does not offer test-undiscovered.
			DirectTools: []string{"test-read"},
		},
		{
			Key: "test-offline", Title: "test offline group", Description: "test offline group description",
			ExecutorKind: action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP, ExecutorDescription: "test offline executor",
			Health: health,
		},
		{
			Key: "test-sandbox", Title: "test sandbox group", Description: "test sandbox group description",
			ExecutorKind: action_servicepb.ExecutorKind_EXECUTOR_KIND_POD, ExecutorDescription: "test pod executor",
			Available: true,
		},
	}
}

// withoutDetail is the views as a workload reads them.
func withoutDetail(views []*action_servicepb.ActionGroup) []*action_servicepb.ActionGroup {
	workload := make([]*action_servicepb.ActionGroup, len(views))
	for i, view := range views {
		workload[i] = proto.CloneOf(view)
		if workload[i].Health != nil {
			workload[i].Health.Detail = nil
		}
	}
	return workload
}

func equalGroups(got, want []*action_servicepb.ActionGroup) bool {
	return slices.EqualFunc(got, want, func(a, b *action_servicepb.ActionGroup) bool { return proto.Equal(a, b) })
}

func TestTheCatalogReadsTheSameForEveryPrincipalButForHealthDetail(t *testing.T) {
	groups := newCatalog(t, testCatalog)
	health := offlineHealth("test detail naming https://mcp.test.example")
	groups.Update("test-offline", func(group *catalog.Group) { group.Health = health })
	s := serveWith(t, kube.TokenReviews(), groups)
	operatorView := testCatalogViews(t, health)
	for name, read := range map[string]struct {
		token string
		want  []*action_servicepb.ActionGroup
	}{
		"a caller":         {callerToken, withoutDetail(operatorView)},
		"a service reader": {readerToken, withoutDetail(operatorView)},
		"an operator":      {s.operatorToken, operatorView},
	} {
		got, err := s.client.ListActionGroups(as(t, read.token), &action_servicepb.ListActionGroupsRequest{})
		if err != nil || !equalGroups(got.GetActionGroups(), read.want) {
			t.Errorf("%s reads %v, %v\nwant %v", name, got, err, read.want)
		}
	}
	_, err := s.client.ListActionGroups(as(t, unlabeledToken), &action_servicepb.ListActionGroupsRequest{})
	wantCode(t, "an unlabeled account's read", err, codes.Unauthenticated)
}

func TestGetActionAnswersEveryPrincipalAndRefusesWhatTheCatalogDoesNotOffer(t *testing.T) {
	s := serveWith(t, kube.TokenReviews(), newCatalog(t, testCatalog))
	for name, token := range map[string]string{"a caller": callerToken, "a service reader": readerToken, "an operator": s.operatorToken} {
		got, err := s.client.GetAction(as(t, token), &action_servicepb.GetActionRequest{GroupKey: "test-mcp", ActionKey: "test-read"})
		if err != nil || !proto.Equal(got.GetAction(), testReadAction(t)) {
			t.Errorf("%s reads %v, %v", name, got, err)
		}
	}
	for name, refused := range map[string]struct {
		group, action string
		code          codes.Code
	}{
		"an unknown group":                              {"test-unknown", "test-read", codes.NotFound},
		"an Action the group does not offer":            {"test-mcp", "test-unknown", codes.NotFound},
		"an unavailable group's Action":                 {"test-offline", "test-read", codes.Unavailable},
		"an Action an unavailable group does not offer": {"test-offline", "test-unknown", codes.Unavailable},
	} {
		_, err := s.client.GetAction(as(t, callerToken), &action_servicepb.GetActionRequest{GroupKey: refused.group, ActionKey: refused.action})
		wantCode(t, name, err, refused.code)
	}
	_, err := s.client.GetAction(as(t, unlabeledToken), &action_servicepb.GetActionRequest{GroupKey: "test-mcp", ActionKey: "test-read"})
	wantCode(t, "an unlabeled account's read", err, codes.Unauthenticated)
}

func TestTheHTTPFacadeServesTheCatalogReads(t *testing.T) {
	s := serveWith(t, kube.TokenReviews(), newCatalog(t, testCatalog))
	code, body := s.get(t, "/v1/action-groups", callerToken)
	listed := &action_servicepb.ListActionGroupsResponse{}
	if err := protojson.Unmarshal(body, listed); code != http.StatusOK || err != nil || !equalGroups(listed.GetActionGroups(), testCatalogViews(t, nil)) {
		t.Errorf("the catalog: %d %s, %v", code, body, err)
	}
	code, body = s.get(t, "/v1/action-groups/test-mcp/actions/test-read", callerToken)
	action := &action_servicepb.GetActionResponse{}
	if err := protojson.Unmarshal(body, action); code != http.StatusOK || err != nil || !proto.Equal(action.GetAction(), testReadAction(t)) {
		t.Errorf("an Action: %d %s, %v", code, body, err)
	}
	for path, want := range map[string]int{
		"/v1/action-groups/test-mcp/actions/test-unknown":  http.StatusNotFound,
		"/v1/action-groups/test-offline/actions/test-read": http.StatusServiceUnavailable,
	} {
		if code, body := s.get(t, path, callerToken); code != want {
			t.Errorf("%s: %d %s, want %d", path, code, body, want)
		}
	}
}

type groupStream = grpc.ServerStreamingClient[action_servicepb.WatchActionGroupsResponse]

func (s *served) watchGroups(t *testing.T, ctx context.Context) groupStream {
	t.Helper()
	stream, err := s.client.WatchActionGroups(ctx, &action_servicepb.WatchActionGroupsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

// groupSnapshot reads the frames up to snapshot_complete and returns the chunks they carried.
func groupSnapshot(t *testing.T, stream groupStream) [][]*action_servicepb.ActionGroup {
	t.Helper()
	var chunks [][]*action_servicepb.ActionGroup
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		switch frame := frame.GetFrame().(type) {
		case *action_servicepb.WatchActionGroupsResponse_Snapshot:
			chunks = append(chunks, frame.Snapshot.GetActionGroups())
		case *action_servicepb.WatchActionGroupsResponse_SnapshotComplete:
			return chunks
		default:
			t.Fatalf("a %T frame before snapshot_complete", frame)
		}
	}
}

// wantGroupUpsert reads the next frame, which must be an upsert of the group as want shows it.
func wantGroupUpsert(t *testing.T, what string, stream groupStream, want *action_servicepb.ActionGroup) {
	t.Helper()
	frame, err := stream.Recv()
	if err != nil {
		t.Fatalf("%s: %v", what, err)
	}
	if !proto.Equal(frame.GetUpsert(), want) {
		t.Errorf("%s: frame %v\nwant an upsert of %v", what, frame, want)
	}
}

func TestAGroupWatchSendsTheMCPGroupsThenEachChangeItsPrincipalSees(t *testing.T) {
	groups := newCatalog(t, testCatalog)
	s := serveWith(t, kube.TokenReviews(), groups)
	workload := s.watchGroups(t, as(t, callerToken))
	operator := s.watchGroups(t, as(t, s.operatorToken))
	// The MCP groups as ListActionGroups shows them; the sandbox group is not streamed.
	mcpGroups := testCatalogViews(t, nil)[:2]
	for name, stream := range map[string]groupStream{"the workload's": workload, "the operator's": operator} {
		if chunks := groupSnapshot(t, stream); len(chunks) != 1 || !equalGroups(chunks[0], mcpGroups) {
			t.Errorf("%s snapshot: %v", name, chunks)
		}
	}

	first := testCatalogViews(t, offlineHealth("test first detail"))
	groups.Update("test-offline", func(group *catalog.Group) { group.Health = first[1].Health })
	wantGroupUpsert(t, "the workload's upsert of the new health", workload, withoutDetail(first)[1])
	wantGroupUpsert(t, "the operator's upsert of the new health", operator, first[1])
	// A change only the operator sees, then one of a group no watch streams.
	second := testCatalogViews(t, offlineHealth("test second detail"))
	groups.Update("test-offline", func(group *catalog.Group) { group.Health = second[1].Health })
	wantGroupUpsert(t, "the operator's upsert of the new detail", operator, second[1])
	groups.Update("test-sandbox", func(group *catalog.Group) { group.Available = false })
	// The next change both see is the next frame of each.
	groups.Update("test-mcp", func(group *catalog.Group) { group.Available = false })
	unavailable := proto.CloneOf(second[0])
	unavailable.Available, unavailable.Actions, unavailable.DirectTools = false, nil, nil
	wantGroupUpsert(t, "the workload's upsert of an unavailable group", workload, unavailable)
	wantGroupUpsert(t, "the operator's upsert of an unavailable group", operator, unavailable)
}

// holdDeadline is the client's deadline for a watch of a catalog that does not change, which ends it.
const holdDeadline = 3 * time.Second

func TestAGroupWatchOfACatalogThatDoesNotChangeHoldsUntilTheClientsDeadline(t *testing.T) {
	s := serveWith(t, kube.TokenReviews(), newCatalog(t, testCatalog))
	ctx, cancel := context.WithTimeout(as(t, callerToken), holdDeadline)
	defer cancel()
	stream := s.watchGroups(t, ctx)
	groupSnapshot(t, stream)
	wantEnd(t, "a watch with nothing more to send", stream, codes.DeadlineExceeded)
}

func TestAGroupWatchSendsItsSnapshotInKeyOrderAndBoundedChunks(t *testing.T) {
	settings := map[string]any{"test-sandbox": map[string]any{
		"title": "t", "description": "d",
		"executor": map[string]any{"kind": "pod", "description": "e", "namespace": "test-namespace", "templates": map[string]any{"test-template": "t"}},
	}}
	empty, err := json.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}
	var keys []string
	for i := range snapshotChunk + 1 {
		key := fmt.Sprintf("test-group-%03d", i)
		keys = append(keys, key)
		settings[key] = map[string]any{"title": "t", "description": "d", "executor": map[string]any{"kind": "mcp", "description": "e"}}
	}
	full, err := json.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}

	s := serveWith(t, kube.TokenReviews(), newCatalog(t, string(full)))
	chunks := groupSnapshot(t, s.watchGroups(t, as(t, s.operatorToken)))
	var got []string
	for _, chunk := range chunks {
		for _, group := range chunk {
			got = append(got, group.GetKey())
		}
	}
	if len(chunks) != 2 || len(chunks[0]) != snapshotChunk || !slices.Equal(got, keys) {
		t.Errorf("%d chunks of %v, want %d MCP groups in key order in 2", len(chunks), got, len(keys))
	}

	// A catalog without MCP groups completes its snapshot alone.
	s = serveWith(t, kube.TokenReviews(), newCatalog(t, string(empty)))
	if chunks := groupSnapshot(t, s.watchGroups(t, as(t, s.operatorToken))); len(chunks) != 0 {
		t.Errorf("the snapshot of no MCP group: %v", chunks)
	}
}

func TestAGroupWatchRenewsOnDrainAndEndsOnceItsBearerIsRefused(t *testing.T) {
	groups := newCatalog(t, testCatalog)
	s := serveWith(t, kube.TokenReviews(), groups)
	// An account of this test's own, so that changing it leaves the other tests' alone.
	delabeled := auth.Account{Namespace: testNamespace, Name: "test-group-watch-delabeled-caller"}
	if err := setCallerLabel(t.Context(), delabeled, true); err != nil {
		t.Fatal(err)
	}
	delabeledToken, err := kube.WorkloadToken(t.Context(), delabeled, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	refused := s.watchGroups(t, as(t, delabeledToken))
	groupSnapshot(t, refused)
	if err := setCallerLabel(t.Context(), delabeled, false); err != nil {
		t.Fatal(err)
	}
	// A change is not sent once the bearer is refused.
	groups.Update("test-mcp", func(group *catalog.Group) { group.Available = false })
	wantEnd(t, "an unlabeled account's watch", refused, codes.Unauthenticated)

	renewed := s.watchGroups(t, as(t, callerToken))
	groupSnapshot(t, renewed)
	s.gate.Drain()
	if frame, err := renewed.Recv(); err != nil || frame.GetReconnectRequired() == nil {
		t.Errorf("frame %v, %v; want reconnect_required", frame, err)
	}
	wantEnd(t, "the watch after reconnect_required", renewed, codes.OK)
}
