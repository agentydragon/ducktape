package mcpserver

import (
	"context"
	_ "embed"
	"encoding/json"
	"maps"
	"reflect"
	"strconv"
	"sync"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// golden.json is what the Python tools answer; `//agentplane/action_service:test_mcp_golden` regenerates and
// checks it.
//
//go:embed golden.json
var goldenJSON []byte

type goldenVectors struct {
	Server struct {
		Name         string `json:"name"`
		Instructions []struct {
			MaxWaitSeconds float64 `json:"max_wait_seconds"`
			Text           string  `json:"text"`
		} `json:"instructions"`
	} `json:"server"`
	Catalog json.RawMessage `json:"catalog"`
	Calls   []goldenCall    `json:"calls"`
	Floats  []struct {
		Value     float64 `json:"value"`
		Text      string  `json:"text"`
		JSONDumps string  `json:"json_dumps"`
	} `json:"floats"`
	Limits []struct {
		Value float64 `json:"value"`
		G     string  `json:"g"`
	} `json:"limits"`
	Strings []struct {
		Value     string `json:"value"`
		Text      string `json:"text"`
		JSONDumps string `json:"json_dumps"`
		Repr      string `json:"repr"`
	} `json:"strings"`
}

type goldenCall struct {
	Name           string           `json:"name"`
	Tool           string           `json:"tool"`
	Arguments      json.RawMessage  `json:"arguments"`
	MaxWaitSeconds float64          `json:"max_wait_seconds"`
	Core           []goldenExchange `json:"core"`
	Result         json.RawMessage  `json:"result"`
}

// goldenExchange is one core RPC a call makes: its request, and its response or the status it fails with.
type goldenExchange struct {
	RPC      string          `json:"rpc"`
	Request  json.RawMessage `json:"request"`
	Response json.RawMessage `json:"response"`
	Code     string          `json:"code"`
	Message  string          `json:"message"`
}

func loadGolden(t *testing.T) goldenVectors {
	t.Helper()
	var vectors goldenVectors
	if err := json.Unmarshal(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

func (v goldenVectors) catalog(t *testing.T) *pb.ListActionGroupsResponse {
	t.Helper()
	catalog := &pb.ListActionGroupsResponse{}
	if err := protojson.Unmarshal(v.Catalog, catalog); err != nil {
		t.Fatal(err)
	}
	return catalog
}

var rpcMessages = map[string]struct{ request, response func() proto.Message }{
	"SubmitActionRequest": {
		func() proto.Message { return &pb.SubmitActionRequestRequest{} },
		func() proto.Message { return &pb.SubmitActionRequestResponse{} },
	},
	"ListActionRequests": {
		func() proto.Message { return &pb.ListActionRequestsRequest{} },
		func() proto.Message { return &pb.ListActionRequestsResponse{} },
	},
	"GetActionRequest": {
		func() proto.Message { return &pb.GetActionRequestRequest{} },
		func() proto.Message { return &pb.GetActionRequestResponse{} },
	},
	"CancelActionRequest": {
		func() proto.Message { return &pb.CancelActionRequestRequest{} },
		func() proto.Message { return &pb.CancelActionRequestResponse{} },
	},
	"ListActionRequestEvents": {
		func() proto.Message { return &pb.ListActionRequestEventsRequest{} },
		func() proto.Message { return &pb.ListActionRequestEventsResponse{} },
	},
	"GetActionPolicy": {
		func() proto.Message { return &pb.GetActionPolicyRequest{} },
		func() proto.Message { return &pb.GetActionPolicyResponse{} },
	},
}

// catalogAction answers GetAction from the catalog as the core would.
func catalogAction(catalog *pb.ListActionGroupsResponse, request *pb.GetActionRequest) (proto.Message, error) {
	for _, group := range catalog.GetActionGroups() {
		if group.GetKey() != request.GetGroupKey() {
			continue
		}
		if !group.GetAvailable() {
			return nil, status.Error(codes.Unavailable, "test group unavailable")
		}
		for _, action := range group.GetActions() {
			if action.GetName() == request.GetActionKey() {
				return &pb.GetActionResponse{Action: action}, nil
			}
		}
	}
	return nil, status.Error(codes.NotFound, "test unknown Action")
}

// scriptedCore answers the catalog reads from catalog and expects exactly the script's other RPCs, in order. It
// reports how many of them arrived.
func scriptedCore(t *testing.T, catalog *pb.ListActionGroupsResponse, script []goldenExchange) (*fakeCore, func() int) {
	var mu sync.Mutex
	arrived := 0
	core := &fakeCore{
		unary: func(_ context.Context, rpc string, request proto.Message) (proto.Message, error) {
			switch rpc {
			case "ListActionGroups":
				return catalog, nil
			case "GetAction":
				return catalogAction(catalog, request.(*pb.GetActionRequest))
			}
			mu.Lock()
			defer mu.Unlock()
			if arrived == len(script) || script[arrived].RPC != rpc {
				t.Errorf("unexpected %s(%v) after %d of the script's RPCs", rpc, request, arrived)
				return nil, status.Error(codes.Internal, "test script ran out")
			}
			exchange := script[arrived]
			arrived++
			messages := rpcMessages[rpc]
			want := messages.request()
			if err := protojson.Unmarshal(exchange.Request, want); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(request, want) {
				t.Errorf("%s request = %v, want %v", rpc, request, want)
			}
			if exchange.Code != "" {
				var code codes.Code
				if err := code.UnmarshalJSON([]byte(strconv.Quote(exchange.Code))); err != nil {
					t.Fatal(err)
				}
				return nil, status.Error(code, exchange.Message)
			}
			response := messages.response()
			if err := protojson.Unmarshal(exchange.Response, response); err != nil {
				t.Fatal(err)
			}
			return response, nil
		},
		watch: func(*pb.WatchActionRequestRequest, grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
			t.Error("unexpected watch")
			return status.Error(codes.Internal, "test script has no watch")
		},
	}
	return core, func() int {
		mu.Lock()
		defer mu.Unlock()
		return arrived
	}
}

// wireValue is a value as its JSON reads, without the framing the two SDKs spell differently: the result type, the
// server's name the 2026-07-28 protocol stamps on each result, and an isError that is false.
func wireValue(t *testing.T, value any) map[string]any {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	delete(decoded, "resultType")
	if meta, ok := decoded["_meta"].(map[string]any); ok {
		delete(meta, mcp.MetaKeyServerInfo)
		if len(meta) == 0 {
			delete(decoded, "_meta")
		}
	}
	if decoded["isError"] == false {
		delete(decoded, "isError")
	}
	return decoded
}

func indented(t *testing.T, value any) string {
	t.Helper()
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func TestToolCallsAnswerAsThePythonToolsDo(t *testing.T) {
	vectors := loadGolden(t)
	catalog := vectors.catalog(t)
	for _, vector := range vectors.Calls {
		t.Run(vector.Name, func(t *testing.T) {
			core, arrived := scriptedCore(t, catalog, vector.Core)
			session := newHarness(t, core, vector.MaxWaitSeconds).connect(t, callerToken)
			result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: vector.Tool, Arguments: vector.Arguments})
			if err != nil {
				t.Fatal(err)
			}
			if arrived() != len(vector.Core) {
				t.Errorf("%d of the script's %d RPCs arrived", arrived(), len(vector.Core))
			}
			var want map[string]any
			if err := json.Unmarshal(vector.Result, &want); err != nil {
				t.Fatal(err)
			}
			want = wireValue(t, want)
			if got := wireValue(t, result); !reflect.DeepEqual(got, want) {
				t.Errorf("answer =\n%s\nwant\n%s", indented(t, got), indented(t, want))
			}
		})
	}
}

func TestToolsAreListedAsFastMCPListsThem(t *testing.T) {
	vectors := loadGolden(t)
	session := newHarness(t, &fakeCore{}, vectors.Server.Instructions[0].MaxWaitSeconds).connect(t, callerToken)
	if got := session.InitializeResult().ServerInfo.Name; got != vectors.Server.Name {
		t.Errorf("server name = %q, want %q", got, vectors.Server.Name)
	}
	if got := session.InitializeResult().Instructions; got != vectors.Server.Instructions[0].Text {
		t.Errorf("instructions = %q, want %q", got, vectors.Server.Instructions[0].Text)
	}
	listed, err := session.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var definitions []map[string]any
	if err := json.Unmarshal(toolsJSON, &definitions); err != nil {
		t.Fatal(err)
	}
	want := map[string]map[string]any{}
	for _, definition := range definitions {
		want[definition["name"].(string)] = definition
	}
	if len(listed.Tools) != len(want) {
		t.Errorf("listed %d tools, want %d", len(listed.Tools), len(want))
	}
	for _, tool := range listed.Tools {
		got := wireValue(t, tool)
		definition := want[tool.Name]
		// The SDK spells out a false hint FastMCP leaves out; both mean the same.
		if gotHints, wantHints := annotations(t, got), annotations(t, definition); gotHints != wantHints {
			t.Errorf("%s annotations = %+v, want %+v", tool.Name, gotHints, wantHints)
		}
		delete(got, "annotations")
		definition = maps.Clone(definition)
		delete(definition, "annotations")
		if !reflect.DeepEqual(got, definition) {
			t.Errorf("%s =\n%s\nwant\n%s", tool.Name, indented(t, got), indented(t, definition))
		}
	}
}

type hints struct {
	readOnly, idempotent   bool
	destructive, openWorld *bool
}

func annotations(t *testing.T, tool map[string]any) hints {
	t.Helper()
	var parsed mcp.ToolAnnotations
	encoded, err := json.Marshal(tool["annotations"])
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(encoded, &parsed); err != nil {
		t.Fatal(err)
	}
	return hints{readOnly: parsed.ReadOnlyHint, idempotent: parsed.IdempotentHint, destructive: parsed.DestructiveHint, openWorld: parsed.OpenWorldHint}
}

func TestInstructionsNameTheConfiguredWaitLimit(t *testing.T) {
	for _, vector := range loadGolden(t).Server.Instructions {
		if got := instructions(vector.MaxWaitSeconds); got != vector.Text {
			t.Errorf("instructions(%v) = %q, want %q", vector.MaxWaitSeconds, got, vector.Text)
		}
	}
}

func TestEncodingsWriteAsPythonDoes(t *testing.T) {
	vectors := loadGolden(t)
	for _, vector := range vectors.Floats {
		if got := pydanticJSON.encode(vector.Value); got != vector.Text {
			t.Errorf("Pydantic JSON of %v = %q, want %q", vector.Value, got, vector.Text)
		}
		if got := pythonDumps.encode(vector.Value); got != vector.JSONDumps {
			t.Errorf("json.dumps of %v = %q, want %q", vector.Value, got, vector.JSONDumps)
		}
	}
	for _, vector := range vectors.Limits {
		if got := pythonG(vector.Value); got != vector.G {
			t.Errorf("format(%v, 'g') = %q, want %q", vector.Value, got, vector.G)
		}
	}
	for _, vector := range vectors.Strings {
		if got := pydanticJSON.encode(vector.Value); got != vector.Text {
			t.Errorf("Pydantic JSON of %q = %q, want %q", vector.Value, got, vector.Text)
		}
		if got := pythonDumps.encode(vector.Value); got != vector.JSONDumps {
			t.Errorf("json.dumps of %q = %q, want %q", vector.Value, got, vector.JSONDumps)
		}
		if got := pythonRepr(vector.Value); got != vector.Repr {
			t.Errorf("repr of %q = %q, want %q", vector.Value, got, vector.Repr)
		}
	}
}
