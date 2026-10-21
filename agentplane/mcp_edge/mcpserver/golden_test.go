package mcpserver

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode"

	"github.com/google/uuid"
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
	Catalog        json.RawMessage `json:"catalog"`
	DirectListings []goldenListing `json:"direct_listings"`
	Calls          []goldenCall    `json:"calls"`
}

// goldenListing is the direct tools Python lists for a caller, after the generic tools.
type goldenListing struct {
	MaxWaitSeconds    float64           `json:"max_wait_seconds"`
	DirectWaitSeconds float64           `json:"direct_wait_seconds"`
	Core              []goldenExchange  `json:"core"`
	Tools             []json.RawMessage `json:"tools"`
}

type goldenCall struct {
	Name              string           `json:"name"`
	Tool              string           `json:"tool"`
	Arguments         json.RawMessage  `json:"arguments"`
	MaxWaitSeconds    float64          `json:"max_wait_seconds"`
	DirectWaitSeconds float64          `json:"direct_wait_seconds"`
	Core              []goldenExchange `json:"core"`
	Result            json.RawMessage  `json:"result"`
}

// freshDirectKey is how a vector names a direct call's idempotency key, a fresh `direct-` UUID each call.
const freshDirectKey = "direct-<uuid4>"

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
			if submitted, ok := request.(*pb.SubmitActionRequestRequest); ok &&
				want.(*pb.SubmitActionRequestRequest).GetIdempotencyKey() == freshDirectKey {
				fresh, found := strings.CutPrefix(submitted.GetIdempotencyKey(), "direct-")
				if id, err := uuid.Parse(fresh); !found || err != nil || id.Version() != 4 {
					t.Errorf("direct call key = %q, want direct- and a random UUID", submitted.GetIdempotencyKey())
				}
				submitted = proto.CloneOf(submitted)
				submitted.IdempotencyKey = freshDirectKey
				request = submitted
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

// decoded is a value as its JSON reads.
func decoded(t *testing.T, value any) map[string]any {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatal(err)
	}
	return fields
}

// answerValue is a tool answer as the comparison reads it: its content and structured content as the values they
// encode, without the framing the two SDKs spell differently (the result type, the server's name the 2026-07-28
// protocol stamps on each result, and an isError that is false). fromPython reads Python's repr quotes in prose as
// Go's %q.
func answerValue(t *testing.T, answer any, fromPython bool) map[string]any {
	t.Helper()
	fields := decoded(t, answer)
	delete(fields, "resultType")
	if meta, ok := fields["_meta"].(map[string]any); ok {
		delete(meta, mcp.MetaKeyServerInfo)
		if len(meta) == 0 {
			delete(fields, "_meta")
		}
	}
	if fields["isError"] == false {
		delete(fields, "isError")
	}
	if structured, ok := fields["structuredContent"]; ok {
		fields["structuredContent"] = jsonValue(structured)
	}
	content, _ := fields["content"].([]any)
	for _, block := range content {
		if block, ok := block.(map[string]any); ok && block["type"] == "text" {
			block["text"] = textValue(block["text"].(string), fromPython)
		}
	}
	return fields
}

// textValue is a text block as the value it encodes: JSON as a JSON value, and prose as its words and the JSON
// values it quotes after a space.
func textValue(text string, fromPython bool) any {
	var whole any
	if json.Unmarshal([]byte(text), &whole) == nil {
		return jsonValue(whole)
	}
	var words strings.Builder
	quoted := []any{}
	for i := 0; i < len(text); i++ {
		if i > 0 && text[i-1] == ' ' && (text[i] == '{' || text[i] == '[') {
			var value any
			decoder := json.NewDecoder(strings.NewReader(text[i:]))
			if decoder.Decode(&value) == nil {
				quoted = append(quoted, jsonValue(value))
				words.WriteString("<json>")
				i += int(decoder.InputOffset()) - 1
				continue
			}
		}
		words.WriteByte(text[i])
	}
	prose := words.String()
	if fromPython {
		prose = goQuotes(prose)
	}
	return []any{prose, quoted}
}

// goQuotes reads Python's repr quotes as %q's: a ' between two letters is an apostrophe, any other one a quote.
func goQuotes(text string) string {
	runes := []rune(text)
	for i, r := range runes {
		if r == '\'' && (i == 0 || i == len(runes)-1 || !unicode.IsLetter(runes[i-1]) || !unicode.IsLetter(runes[i+1])) {
			runes[i] = '"'
		}
	}
	return string(runes)
}

// jsonValue is a decoded JSON value with each timestamp read as the instant it names.
func jsonValue(value any) any {
	switch value := value.(type) {
	case map[string]any:
		for key, field := range value {
			value[key] = jsonValue(field)
		}
	case []any:
		for i, item := range value {
			value[i] = jsonValue(item)
		}
	case string:
		if at, err := time.Parse(time.RFC3339Nano, value); err == nil {
			return at.UTC().Format(time.RFC3339Nano)
		}
	}
	return value
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
			session := newHarness(t, core, Options{
				MaxWaitSeconds:    vector.MaxWaitSeconds,
				DirectWaitSeconds: vector.DirectWaitSeconds,
			}).connect(t, callerToken)
			result, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: vector.Tool, Arguments: vector.Arguments})
			if err != nil {
				t.Fatal(err)
			}
			if arrived() != len(vector.Core) {
				t.Errorf("%d of the script's %d RPCs arrived", arrived(), len(vector.Core))
			}
			want := answerValue(t, vector.Result, true)
			if got := answerValue(t, result, false); !reflect.DeepEqual(got, want) {
				t.Errorf("answer =\n%s\nwant\n%s", indented(t, got), indented(t, want))
			}
		})
	}
}

func TestToolsAreListedAsFastMCPListsThem(t *testing.T) {
	vectors := loadGolden(t)
	catalog := vectors.catalog(t)
	var generic []map[string]any
	if err := json.Unmarshal(toolsJSON, &generic); err != nil {
		t.Fatal(err)
	}
	titles := map[string]string{}
	for _, group := range catalog.GetActionGroups() {
		for _, action := range group.GetActions() {
			if action.Title == nil {
				titles[group.GetKey()+directSeparator+action.GetName()] = group.GetTitle() + ": " + action.GetName()
			}
		}
	}
	for _, listing := range vectors.DirectListings {
		t.Run(fmt.Sprintf("direct wait %vs", listing.DirectWaitSeconds), func(t *testing.T) {
			core, arrived := scriptedCore(t, catalog, listing.Core)
			session := newHarness(t, core, Options{
				MaxWaitSeconds:    listing.MaxWaitSeconds,
				DirectWaitSeconds: listing.DirectWaitSeconds,
			}).connect(t, callerToken)
			if got := session.InitializeResult().ServerInfo.Name; got != vectors.Server.Name {
				t.Errorf("server name = %q, want %q", got, vectors.Server.Name)
			}
			listed, err := session.ListTools(t.Context(), nil)
			if err != nil {
				t.Fatal(err)
			}
			if arrived() != len(listing.Core) {
				t.Errorf("%d of the script's %d RPCs arrived", arrived(), len(listing.Core))
			}
			// The SDK sorts the generic tools by name; the direct tools follow in Python's order.
			want := map[string]any{}
			for _, definition := range generic {
				want[definition["name"].(string)] = toolValue(t, definition)
			}
			direct := []any{}
			for _, tool := range listing.Tools {
				definition := decoded(t, tool)
				// Deliberate difference: FastMCP titles an untitled tool after its name, Go after its group's title
				// and the Action's name.
				if title, ok := titles[definition["name"].(string)]; ok {
					definition["title"] = title
				}
				direct = append(direct, toolValue(t, definition))
			}
			got, gotDirect := map[string]any{}, []any{}
			for _, tool := range listed.Tools {
				if strings.Contains(tool.Name, directSeparator) {
					gotDirect = append(gotDirect, toolValue(t, tool))
				} else {
					got[tool.Name] = toolValue(t, tool)
				}
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("generic tools =\n%s\nwant\n%s", indented(t, got), indented(t, want))
			}
			if !reflect.DeepEqual(gotDirect, direct) {
				t.Errorf("direct tools =\n%s\nwant\n%s", indented(t, gotDirect), indented(t, direct))
			}
		})
	}
}

// toolValue is a listed tool as the comparison reads it: the SDK spells out a false hint FastMCP leaves out, and
// both mean the same.
func toolValue(t *testing.T, tool any) map[string]any {
	t.Helper()
	fields := decoded(t, tool)
	if hints, ok := fields["annotations"]; ok {
		encoded, err := json.Marshal(hints)
		if err != nil {
			t.Fatal(err)
		}
		var parsed mcp.ToolAnnotations
		if err := json.Unmarshal(encoded, &parsed); err != nil {
			t.Fatal(err)
		}
		fields["annotations"] = decoded(t, parsed)
	}
	return fields
}

func TestInstructionsNameTheConfiguredWaitLimit(t *testing.T) {
	for _, vector := range loadGolden(t).Server.Instructions {
		if got := instructions(vector.MaxWaitSeconds); got != vector.Text {
			t.Errorf("instructions(%v) = %q, want %q", vector.MaxWaitSeconds, got, vector.Text)
		}
	}
}
