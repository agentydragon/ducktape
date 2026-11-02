package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	sessionopeningpb "github.com/agentydragon/ducktape/agentplane/app/session_opening/sessionopeningpb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/danielgtaylor/huma/v2"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestSessionDefaultsMergeExplicitFieldsAndHonorSetupPresence(t *testing.T) {
	config := Config{
		SandboxPresets: map[string]SandboxPreset{"work": {ThreadPreset: "default"}},
		ThreadPresets: map[string]ThreadPreset{
			"default": {
				Harness: "HARNESS_CODEX", Model: "model-a", CWD: "/state/workspaces/{session_id}",
				ReasoningEffort: sessionTestStringPtr("high"), Instructions: "from preset", SetupScript: "setup",
			},
		},
	}
	sandbox := &sandboxespb.Sandbox{Preset: proto.String("work")}
	defaults, setup, err := sessionDefaults(config, sandbox, false, nil)
	if err != nil {
		t.Fatal(err)
	}
	if defaults.GetHarness() != sessionpb.Harness_HARNESS_CODEX || defaults.GetModel() != "model-a" || defaults.GetReasoningEffort() != "high" || setup == nil || *setup != "setup" {
		t.Fatalf("unexpected preset defaults: spec=%v setup=%v", defaults, setup)
	}
	merged, err := bridge.ParseSessionSpec(defaults, map[string]any{"model": "", "instructions": "request"})
	if err != nil {
		t.Fatal(err)
	}
	merged.Cwd = replaceSessionID(merged.GetCwd(), "b351b316-fdc4-4f50-a642-758992ae3323")
	if merged.GetModel() != "" || merged.GetInstructions() != "request" || merged.GetCwd() != "/state/workspaces/b351b316-fdc4-4f50-a642-758992ae3323" {
		t.Fatalf("request did not overlay defaults: %v", merged)
	}
	if _, setup, err = sessionDefaults(config, sandbox, true, nil); err != nil || setup != nil {
		t.Fatalf("explicit null setup_script should clear preset value; setup=%v err=%v", setup, err)
	}
	requested := "alternate"
	if _, setup, err = sessionDefaults(config, sandbox, true, &requested); err != nil || setup == nil || *setup != requested {
		t.Fatalf("explicit setup_script should replace preset value; setup=%v err=%v", setup, err)
	}
	if _, _, err = sessionDefaults(config, &sandboxespb.Sandbox{Preset: proto.String("missing")}, false, nil); err == nil {
		t.Fatal("unknown Sandbox preset was accepted")
	}
}

func sessionTestStringPtr(value string) *string { return &value }

func TestSessionRoutesRequireCallerAuthentication(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	if err := RegisterSessionRoutes(server, Config{}, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(server.Close)
	handler := server.Handler(authentication)
	for _, test := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/sandboxes/demo/sessions"},
		{http.MethodGet, "/sandboxes/demo/sessions/" + uuid.NewString() + "/opening"},
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(test.method, test.path, nil))
		if recorder.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want 401; body: %s", test.method, test.path, recorder.Code, recorder.Body.String())
		}
	}
}

var (
	_ sessionOpener = (*bridge.RunnerBridge)(nil)
	_ threadOpener  = (*threads.Store)(nil)
)

type sessionTestPeer struct {
	sandbox    *sandboxespb.Sandbox
	spec       *sessionpb.SessionSpec
	setup      *string
	id         uuid.UUID
	calls      []string
	failThread bool
	failOpen   error
}

func (p *sessionTestPeer) Get(context.Context, string) (*sandboxespb.Sandbox, error) {
	return p.sandbox, nil
}

func (p *sessionTestPeer) OpenThread(_ context.Context, id uuid.UUID) error {
	p.calls = append(p.calls, "thread")
	p.id = id
	if p.failThread {
		return errors.New("database unavailable")
	}
	return nil
}

func (p *sessionTestPeer) OpenSession(_ context.Context, name, uid string, id uuid.UUID, spec *sessionpb.SessionSpec, setup *string) (*dispatcherpb.SessionOpening, error) {
	p.calls = append(p.calls, "dispatch")
	p.spec, p.setup = spec, setup
	if p.id != id {
		return nil, errors.New("Thread not created before dispatch")
	}
	if p.failOpen != nil {
		return nil, p.failOpen
	}
	return &dispatcherpb.SessionOpening{SessionId: id.String(), Sandbox: name, SandboxUid: uid, State: dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER}, nil
}

func (p *sessionTestPeer) SessionOpening(_ context.Context, name string, id uuid.UUID) (*dispatcherpb.SessionOpening, error) {
	p.calls = append(p.calls, "lookup")
	return &dispatcherpb.SessionOpening{SessionId: id.String(), Sandbox: name, SandboxUid: p.sandbox.GetUid()}, nil
}

type sessionTokenReviewer struct{}

func (sessionTokenReviewer) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: request.Spec.Token == "accepted", Audiences: []string{"agentplane"},
		User: authenticationv1.UserInfo{Username: "system:serviceaccount:tools:session-test"},
	}}, nil
}

func sessionTestGateway(t *testing.T, peer *sessionTestPeer) http.Handler {
	t.Helper()
	authentication, err := auth.New(auth.Config{TokenReviews: sessionTokenReviewer{}, TokenAudience: "agentplane", TokenSubjects: []string{"system:serviceaccount:tools:session-test"}})
	if err != nil {
		t.Fatal(err)
	}
	config := Config{
		SandboxPresets: map[string]SandboxPreset{"work": {ThreadPreset: "default"}},
		ThreadPresets:  map[string]ThreadPreset{"default": {Harness: "HARNESS_CODEX", Model: "preset-model", CWD: "/work/{session_id}", ReasoningEffort: sessionTestStringPtr("high"), Instructions: "preset", SetupScript: "setup"}},
	}
	server := NewHTTPServer(authentication)
	if err := RegisterSessionRoutes(server, config, peer, peer, peer); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(server.Close)
	return server.Handler(authentication)
}

func sessionTestRequest(handler http.Handler, method, path, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	request.Header.Set("Authorization", "Bearer accepted")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func TestSessionOpeningGatewayPreservesPresetPresenceAndWireContract(t *testing.T) {
	id := "b351b316-fdc4-4f50-a642-758992ae3323"
	for _, test := range []struct {
		name, spec, setup                       string
		wantSetup                               *string
		wantHarness                             sessionpb.Harness
		wantModel, wantInstructions, wantEffort string
	}{
		{"omitted", `{}`, "", sessionTestStringPtr("setup"), sessionpb.Harness_HARNESS_CODEX, "preset-model", "preset", "high"},
		{"empty", `{"model":"","instructions":"","reasoning_effort":""}`, `,"setup_script":""`, sessionTestStringPtr(""), sessionpb.Harness_HARNESS_CODEX, "", "", ""},
		{"null", `{"model":null,"instructions":null,"reasoningEffort":null,"harness":null}`, `,"setup_script":null`, nil, sessionpb.Harness_HARNESS_UNSPECIFIED, "", "", ""},
		{"zero", `{"harness":0}`, `,"setup_script":"override"`, sessionTestStringPtr("override"), sessionpb.Harness_HARNESS_UNSPECIFIED, "preset-model", "preset", "high"},
	} {
		t.Run(test.name, func(t *testing.T) {
			peer := &sessionTestPeer{sandbox: &sandboxespb.Sandbox{Uid: "uid", Preset: proto.String("work")}}
			handler := sessionTestGateway(t, peer)
			body := `{"session_id":"` + id + `","sandbox_uid":"uid","spec":` + test.spec + test.setup + `}`
			response := sessionTestRequest(handler, http.MethodPost, "/sandboxes/demo/sessions", body)
			if response.Code != http.StatusCreated {
				t.Fatalf("POST status %d: %s", response.Code, response.Body.String())
			}
			opening := &dispatcherpb.SessionOpening{}
			if err := protojson.Unmarshal(response.Body.Bytes(), opening); err != nil {
				t.Fatal(err)
			}
			if opening.GetSessionId() != id || opening.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
				t.Fatalf("unexpected response %v", opening)
			}
			if peer.spec.GetHarness() != test.wantHarness || peer.spec.GetModel() != test.wantModel || peer.spec.GetInstructions() != test.wantInstructions || peer.spec.GetReasoningEffort() != test.wantEffort || peer.spec.GetCwd() != "/work/"+id || !reflect.DeepEqual(peer.setup, test.wantSetup) {
				t.Fatalf("spec %v setup %v", peer.spec, peer.setup)
			}
			response = sessionTestRequest(handler, http.MethodGet, "/sandboxes/demo/sessions/"+id+"/opening", "")
			if response.Code != http.StatusOK {
				t.Fatalf("GET status %d: %s", response.Code, response.Body.String())
			}
			if err := protojson.Unmarshal(response.Body.Bytes(), opening); err != nil {
				t.Fatal(err)
			}
			if opening.GetSessionId() != id || opening.GetSandbox() != "demo" {
				t.Fatalf("unexpected lookup %v", opening)
			}
			if !reflect.DeepEqual(peer.calls, []string{"thread", "dispatch", "lookup"}) {
				t.Fatalf("calls %v", peer.calls)
			}
		})
	}
}

func TestSessionOpeningGatewayRejectsBeforeEffects(t *testing.T) {
	for _, body := range []string{
		`{"session_id":"invalid","sandbox_uid":"uid","spec":{}}`,
		`{"session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"uid"}`,
		`{"spec":{},"session_id":"b351b316-fdc4-4f50-a642-758992ae3323"}`,
		`{"spec":{"unknown":true},"session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"uid"}`,
		`{"spec":{"model":42},"session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"uid"}`,
		`{"spec":{},"setup_script":false,"session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"uid"}`,
		`{"spec":{},"setup_script":"` + strings.Repeat("x", bridge.MaxSetupScriptCharacters+1) + `","session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"uid"}`,
	} {
		peer := &sessionTestPeer{sandbox: &sandboxespb.Sandbox{Uid: "uid"}}
		response := sessionTestRequest(sessionTestGateway(t, peer), http.MethodPost, "/sandboxes/demo/sessions", body)
		if response.Code != http.StatusUnprocessableEntity || len(peer.calls) != 0 {
			t.Fatalf("status %d effects %v: %s", response.Code, peer.calls, response.Body.String())
		}
	}
}

func TestSessionOpeningGatewayPreservesConflictAndUncertainOutcomes(t *testing.T) {
	for _, test := range []struct {
		uid        string
		failThread bool
		failOpen   error
		wantHTTP   int
		wantCode   codes.Code
		wantCalls  []string
	}{
		{"old-uid", false, nil, 409, codes.Aborted, nil},
		{"uid", true, nil, 503, codes.Unavailable, []string{"thread"}},
		{"uid", false, &bridge.DispatcherRequestTimeoutError{SessionID: uuid.New()}, 504, codes.DeadlineExceeded, []string{"thread", "dispatch"}},
		{"uid", false, &bridge.SessionOpeningNotFoundError{SessionID: uuid.New(), Sandbox: "demo"}, 500, codes.Unknown, []string{"thread", "dispatch"}},
		{"uid", false, &bridge.ServiceError{Code: codes.AlreadyExists, Details: "conflict"}, 409, codes.AlreadyExists, []string{"thread", "dispatch"}},
	} {
		peer := &sessionTestPeer{sandbox: &sandboxespb.Sandbox{Uid: "uid"}, failThread: test.failThread, failOpen: test.failOpen}
		response := sessionTestRequest(sessionTestGateway(t, peer), http.MethodPost, "/sandboxes/demo/sessions", `{"spec":{},"session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"`+test.uid+`"}`)
		var body struct {
			Code   int32
			Detail string
		}
		if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		if response.Code != test.wantHTTP || codes.Code(body.Code) != test.wantCode || body.Detail == "" || !reflect.DeepEqual(peer.calls, test.wantCalls) {
			t.Fatalf("status %d body %+v calls %v", response.Code, body, peer.calls)
		}
	}
}

var _ sessionopeningpb.SessionOpeningServiceServer = (*sessionOpeningService)(nil)

// Frozen pre-facade Huma boundary: exercise the request emitted by the generated browser codec
// against the parser still serving in old app replicas during a rolling deployment.
func TestGeneratedSessionOpeningRequestAcceptedByLegacyHuma(t *testing.T) {
	type legacyBody struct {
		SessionID   uuid.UUID      `json:"session_id" format:"uuid"`
		SandboxUID  string         `json:"sandbox_uid" minLength:"1"`
		Spec        map[string]any `json:"spec"`
		SetupScript *string        `json:"setup_script,omitempty"`
	}
	type legacyInput struct {
		Name string `path:"name"`
		Body legacyBody
	}
	server := NewHTTPServer(nil)
	t.Cleanup(server.Close)
	var received *legacyInput
	huma.Register[legacyInput, struct{}](server.API(), huma.Operation{OperationID: "legacyOpen", Method: http.MethodPost, Path: "/sandboxes/{name}/sessions", DefaultStatus: http.StatusCreated}, func(_ context.Context, input *legacyInput) (*struct{}, error) {
		received = input
		return &struct{}{}, nil
	})
	request := &sessionopeningpb.OpenSessionRequest{}
	if err := protojson.Unmarshal([]byte(`{"session_id":"b351b316-fdc4-4f50-a642-758992ae3323","sandbox_uid":"uid","spec":{"model":"test"},"setup_script":""}`), request); err != nil {
		t.Fatal(err)
	}
	encoded, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	response := sessionTestRequest(server.Handler(nil), http.MethodPost, "/sandboxes/demo/sessions", string(encoded))
	if response.Code != http.StatusCreated || received == nil || received.Name != "demo" || received.Body.Spec["model"] != "test" || received.Body.SetupScript == nil || *received.Body.SetupScript != "" {
		t.Fatalf("legacy status %d body %s received %+v", response.Code, response.Body.String(), received)
	}
}

func TestSessionOpeningCanonicalizesAcceptedUUIDSpelling(t *testing.T) {
	peer := &sessionTestPeer{sandbox: &sandboxespb.Sandbox{Uid: "uid"}}
	handler := sessionTestGateway(t, peer)
	response := sessionTestRequest(handler, http.MethodPost, "/sandboxes/demo/sessions", `{"session_id":"B351B316-FDC4-4F50-A642-758992AE3323","sandbox_uid":"uid","spec":{}}`)
	if response.Code != http.StatusCreated {
		t.Fatalf("status %d: %s", response.Code, response.Body.String())
	}
	response = sessionTestRequest(handler, http.MethodGet, "/sandboxes/demo/sessions/B351B316-FDC4-4F50-A642-758992AE3323/opening", "")
	if response.Code != http.StatusOK {
		t.Fatalf("status %d: %s", response.Code, response.Body.String())
	}
	opening := &dispatcherpb.SessionOpening{}
	if err := protojson.Unmarshal(response.Body.Bytes(), opening); err != nil {
		t.Fatal(err)
	}
	if opening.GetSessionId() != "b351b316-fdc4-4f50-a642-758992ae3323" {
		t.Fatalf("noncanonical backend identity: %v", opening)
	}
}
