package dispatcher

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"
	"k8s.io/apimachinery/pkg/util/sets"
)

// The deployed HTTP facade emits unpopulated fields. Older browser bundles parse ProtoJSON
// strictly, so even a null "command" would invalidate ordinary mutation and watch receipts.
func TestSummaryDispatchJSONRemainsReadableByOlderClients(t *testing.T) {
	wire, err := (protojson.MarshalOptions{EmitUnpopulated: true}).Marshal(&dispatcherpb.Dispatch{})
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(wire, &fields); err != nil {
		t.Fatal(err)
	}
	if value, ok := fields["command"]; ok {
		t.Fatalf("unexpected command field in summary receipt: %s", value)
	}
}

// Exercise the generated JSON stream over native gRPC admission, not a handwritten route.
func TestSessionWatchGeneratedJSONFacade(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 5)
	hub, err := wakeups.Listen(t.Context(), databaseURL, store.SessionNotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	if _, err := createSession(queueStore, admitted(t), "test-facade-unrecorded-session", "test-facade-environment", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	service := &Service{Store: queueStore, Hub: hub, WatchLease: time.Minute, DispatcherServices: sets.New(peertls.AppURI)}
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLSWithTimeout(t, 0, peertls.SessionDispatcherURI, Methods(rule), func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	}, peertls.AppURI)
	client := dispatcherpb.NewSessionDispatcherClient(grpcmethods.AsCaller(connection, peertls.AppURI))
	mux := runtime.NewServeMux()
	if err := dispatcherpb.RegisterSessionDispatcherHandlerClient(t.Context(), mux, client); err != nil {
		t.Fatal(err)
	}
	httpServer := httptest.NewServer(mux)
	t.Cleanup(httpServer.Close)
	httpClient := &http.Client{Timeout: 10 * time.Second}
	response, err := httpClient.Get(httpServer.URL + "/v1/sessions:watch")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("HTTP status = %d", response.StatusCode)
	}
	decoder := json.NewDecoder(response.Body)
	read := func() *dispatcherpb.WatchSessionsResponse {
		t.Helper()
		var envelope struct {
			Result json.RawMessage `json:"result"`
		}
		if err := decoder.Decode(&envelope); err != nil {
			t.Fatal(err)
		}
		frame := &dispatcherpb.WatchSessionsResponse{}
		if err := protojson.Unmarshal(envelope.Result, frame); err != nil {
			t.Fatal(err)
		}
		if frame.GetResumeToken() == "" {
			t.Fatal("missing stream checkpoint")
		}
		return frame
	}
	first := read()
	if first.GetSession().GetSessionId() != "test-facade-unrecorded-session" || first.GetSession().GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING {
		t.Fatalf("first JSON frame = %v", first)
	}
	if frame := read(); frame.GetCaughtUp() == nil {
		t.Fatalf("expected CaughtUp, got %v", frame)
	}
}
