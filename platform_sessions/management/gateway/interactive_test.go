package gateway

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/managementpb"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type interactiveProvider struct {
	providerpb.SessionProviderControlClient
	available   bool
	finish      *providerpb.FinishInteractiveSignInResponse
	finishCalls int
}

func (f *interactiveProvider) FinishInteractiveSignIn(_ context.Context, _ *providerpb.FinishInteractiveSignInRequest, _ ...grpc.CallOption) (*providerpb.FinishInteractiveSignInResponse, error) {
	f.finishCalls++
	return f.finish, nil
}

func TestFinishInteractiveSignInRequiresVerifiedProviderResult(t *testing.T) {
	ref := &providerpb.InstanceRef{ProviderId: "myquest", InstanceId: "personal"}
	fake := &interactiveProvider{available: true}
	service, err := New([]Provider{{Ref: ref, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	request := &managementpb.InstanceRequest{InstanceId: "personal"}
	result := &providerpb.ProbeResult{Source: providerpb.ProbeSource_PROBE_SOURCE_VERIFY, CompletedAt: timestamppb.Now(), Outcome: &providerpb.ProbeResult_Verified{Verified: &emptypb.Empty{}}}
	fake.finish = &providerpb.FinishInteractiveSignInResponse{Instance: ref, Result: result}
	if got, err := service.FinishInteractiveSignIn(context.Background(), request); err != nil || got.GetResult().GetVerified() == nil {
		t.Fatalf("valid finish: %v %v", got, err)
	}
	fake.finish.Result.Source = providerpb.ProbeSource_PROBE_SOURCE_MANUAL
	if _, err := service.FinishInteractiveSignIn(context.Background(), request); status.Code(err) != codes.Unknown {
		t.Fatalf("wrong source accepted: %v", err)
	}
	fake.finish.Result.Source = providerpb.ProbeSource_PROBE_SOURCE_VERIFY
	fake.finish.Instance = &providerpb.InstanceRef{ProviderId: "myquest", InstanceId: "other"}
	if _, err := service.FinishInteractiveSignIn(context.Background(), request); status.Code(err) != codes.Unknown {
		t.Fatalf("wrong instance accepted: %v", err)
	}
	if fake.finishCalls != 3 {
		t.Fatalf("finish calls = %d", fake.finishCalls)
	}
}

func (f *interactiveProvider) GetInfo(context.Context, *providerpb.GetInfoRequest, ...grpc.CallOption) (*providerpb.GetInfoResponse, error) {
	instance := &providerpb.ProviderInstanceInfo{InstanceId: "personal"}
	if f.available {
		instance.InteractiveSignIn = &providerpb.InteractiveSignIn{}
	}
	return &providerpb.GetInfoResponse{Provider: &providerpb.ProviderInfo{
		ProviderId: "myquest", DisplayName: "MyQuest", Instances: []*providerpb.ProviderInstanceInfo{instance},
	}}, nil
}

func TestInteractiveWebSocketRequiresOwnerOriginAndCapability(t *testing.T) {
	const providerToken = "provider-owner-token-aaaaaaaaaaaaaaaaaaaaaaaa"
	upstreamPaths := make(chan string, 3)
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/interactive-sign-in/personal" || r.Header.Get("Authorization") != "Bearer "+providerToken || r.URL.RawQuery != "" {
			http.Error(w, "incorrect upstream request", http.StatusForbidden)
			return
		}
		ws, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{"binary"}})
		if err != nil {
			return
		}
		defer ws.CloseNow()
		upstreamPaths <- r.URL.Path
		if ws.Subprotocol() != "binary" {
			return
		}
		for {
			kind, payload, err := ws.Read(r.Context())
			if err != nil {
				return
			}
			if ws.Write(r.Context(), kind, payload) != nil {
				return
			}
		}
	}))
	defer upstream.Close()

	caFile := trustedCAFile(t, upstream)
	fake := &interactiveProvider{available: true}
	service, err := New([]Provider{{Ref: &providerpb.InstanceRef{ProviderId: "myquest", InstanceId: "personal"}, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	config := HTTPConfig{
		Owner: testOwnerConfig(),
		Providers: map[string]ProviderHTTPConfig{"personal": {
			ID: "personal", Origin: upstream.URL, OwnerToken: providerToken,
			RouteToken: strings.Repeat("r", 40), CAFile: caFile, OperationTimeout: 10 * time.Millisecond,
		}},
		Bearers: []Bearer{{Token: strings.Repeat("b", 40), Owner: true}},
	}
	handler, err := HTTPServer(context.Background(), service, config)
	if err != nil {
		t.Fatal(err)
	}
	public := httptest.NewServer(handler)
	defer public.Close()
	owner, err := newOwnerAuth(testOwnerConfig())
	if err != nil {
		t.Fatal(err)
	}
	cookie, err := owner.cookie.Encode(ownerCookie, ownerSession{Issuer: owner.cfg.Issuer, Subject: "owner", Username: "user", ExpiresAt: time.Now().Add(time.Hour).Unix()})
	if err != nil {
		t.Fatal(err)
	}
	target := "ws" + strings.TrimPrefix(public.URL, "http") + "/api/instances/personal/interactive-sign-in/ws"
	validHeaders := http.Header{"Origin": []string{owner.cfg.PublicBaseURL}, "Cookie": []string{ownerCookie + "=" + cookie}}
	for _, tc := range []struct {
		name    string
		url     string
		headers http.Header
		status  int
	}{
		{"missing cookie", target, http.Header{"Origin": []string{owner.cfg.PublicBaseURL}}, http.StatusUnauthorized},
		{"bearer alone", target, http.Header{"Origin": []string{owner.cfg.PublicBaseURL}, "Authorization": []string{"Bearer " + strings.Repeat("b", 40)}}, http.StatusUnauthorized},
		{"missing origin", target, http.Header{"Cookie": []string{ownerCookie + "=" + cookie}}, http.StatusForbidden},
		{"foreign origin", target, http.Header{"Origin": []string{"https://evil.invalid"}, "Cookie": []string{ownerCookie + "=" + cookie}}, http.StatusForbidden},
		{"cross site", target, http.Header{"Origin": []string{owner.cfg.PublicBaseURL}, "Cookie": []string{ownerCookie + "=" + cookie}, "Sec-Fetch-Site": []string{"cross-site"}}, http.StatusForbidden},
		{"unknown instance", strings.Replace(target, "/personal/", "/other/", 1), validHeaders, http.StatusNotFound},
		{"query", target + "?target=evil", validHeaders, http.StatusNotFound},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ws, response, err := websocket.Dial(context.Background(), tc.url, &websocket.DialOptions{HTTPHeader: tc.headers, Subprotocols: []string{"binary"}})
			if ws != nil {
				ws.CloseNow()
			}
			if err == nil || response == nil || response.StatusCode != tc.status {
				t.Fatalf("dial: err=%v status=%v want=%d", err, response, tc.status)
			}
		})
	}
	fake.available = false
	_, response, err := websocket.Dial(context.Background(), target, &websocket.DialOptions{HTTPHeader: validHeaders, Subprotocols: []string{"binary"}})
	if err == nil || response == nil || response.StatusCode != http.StatusNotFound {
		t.Fatalf("missing capability: err=%v response=%v", err, response)
	}
	fake.available = true
	ws, response, err := websocket.Dial(context.Background(), target, &websocket.DialOptions{HTTPHeader: validHeaders, Subprotocols: []string{"binary"}})
	if err != nil {
		t.Fatalf("owner dial: %v response=%v", err, response)
	}
	defer ws.CloseNow()
	if ws.Subprotocol() != "binary" {
		t.Fatalf("browser subprotocol = %q", ws.Subprotocol())
	}
	select {
	case path := <-upstreamPaths:
		if path != "/internal/interactive-sign-in/personal" {
			t.Fatalf("upstream path = %q", path)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("provider was not reached")
	}
	// This stream must outlive the 10 ms HTTP operation deadline.
	time.Sleep(30 * time.Millisecond)
	payload := []byte{0, 1, 2, 255}
	if err := ws.Write(context.Background(), websocket.MessageBinary, payload); err != nil {
		t.Fatal(err)
	}
	kind, got, err := ws.Read(context.Background())
	if err != nil || kind != websocket.MessageBinary || string(got) != string(payload) {
		t.Fatalf("binary round trip: kind=%v payload=%v err=%v", kind, got, err)
	}
	// noVNC does not request a browser subprotocol. The provider's websockify
	// connection still requires binary, negotiated separately upstream.
	novnc, _, err := websocket.Dial(context.Background(), target, &websocket.DialOptions{HTTPHeader: validHeaders})
	if err != nil {
		t.Fatal(err)
	}
	defer novnc.CloseNow()
	if novnc.Subprotocol() != "" {
		t.Fatalf("unexpected noVNC subprotocol %q", novnc.Subprotocol())
	}
	if err := novnc.Write(context.Background(), websocket.MessageBinary, []byte("RFB 003.008\n")); err != nil {
		t.Fatal(err)
	}
	kind, got, err = novnc.Read(context.Background())
	if err != nil || kind != websocket.MessageBinary || string(got) != "RFB 003.008\n" {
		t.Fatalf("noVNC frame round trip: kind=%v payload=%v err=%v", kind, got, err)
	}
	shortCookie, err := owner.cookie.Encode(ownerCookie, ownerSession{Issuer: owner.cfg.Issuer, Subject: "owner", Username: "user", ExpiresAt: time.Now().Add(2 * time.Second).Unix()})
	if err != nil {
		t.Fatal(err)
	}
	expiringHeaders := http.Header{"Origin": []string{owner.cfg.PublicBaseURL}, "Cookie": []string{ownerCookie + "=" + shortCookie}}
	expiring, _, err := websocket.Dial(context.Background(), target, &websocket.DialOptions{HTTPHeader: expiringHeaders, Subprotocols: []string{"binary"}})
	if err != nil {
		t.Fatal(err)
	}
	defer expiring.CloseNow()
	readCtx, cancelRead := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancelRead()
	if _, _, err := expiring.Read(readCtx); err == nil || readCtx.Err() != nil {
		t.Fatalf("connection should close on owner session expiry: %v", err)
	}
}
