package api

import (
	"crypto/ecdh"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"
)

// otherOperatorSubject is a second operator of the same issuer.
const otherOperatorSubject = "test-other-operator"

// pushServed is WebPushService over the served API's database and gate, on a gRPC server with its HTTP facade, as
// main serves it.
type pushServed struct {
	*served
	push web_push_servicepb.WebPushServiceClient
	// pushURL is the base URL of the HTTP facade.
	pushURL string
	// applicationServerKey is the configured VAPID key's; empty while Web Push is not configured.
	applicationServerKey string
	otherOperatorToken   string
}

// servePush serves WebPushService over s's database and gate, with Web Push configured or not.
func servePush(t *testing.T, s *served, configured bool) *pushServed {
	t.Helper()
	p := &pushServed{served: s, otherOperatorToken: s.issuer.token(t, map[string]any{"sub": otherOperatorSubject})}
	var identity *push.Identity
	if configured {
		private, err := ecdh.P256().GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		der, err := x509.MarshalPKCS8PrivateKey(private)
		if err != nil {
			t.Fatal(err)
		}
		if identity, err = push.NewIdentity(push.Settings{
			PrivateKeyPEM: string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})),
			Subject:       "mailto:test-vapid@agentplane.test", PublicBaseURL: "https://app.test/",
			AllowedPushHosts: []string{"push.test"},
		}); err != nil {
			t.Fatal(err)
		}
		p.applicationServerKey = base64.RawURLEncoding.EncodeToString(private.PublicKey().Bytes())
	}
	updates, err := wakeups.Listen(t.Context(), s.databaseURL, push.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { updates.Close() })
	grpcServer := grpc.NewServer(grpc.ChainUnaryInterceptor(s.gate.UnaryInterceptor), grpc.ChainStreamInterceptor(s.gate.StreamInterceptor))
	web_push_servicepb.RegisterWebPushServiceServer(grpcServer, NewPushService(push.NewSubscriptions(s.db), identity, updates, s.gate))
	grpcListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpFacade, err := facade.NewHandler(t.Context(), grpcListener.Addr().String(), 0, web_push_servicepb.RegisterWebPushServiceHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: httpFacade, ReadHeaderTimeout: callDeadline}
	go func() { _ = grpcServer.Serve(grpcListener) }()
	t.Cleanup(grpcServer.Stop)
	go func() { _ = server.Serve(httpListener) }()
	t.Cleanup(func() { server.Close() })
	connection, err := grpc.NewClient(grpcListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	p.push = web_push_servicepb.NewWebPushServiceClient(connection)
	p.pushURL = "http://" + httpListener.Addr().String()
	return p
}

// subscription is a browser's registration at https://push.test/<name>, with keys of the generation.
func subscription(name string, generation int) *web_push_servicepb.CreatePushSubscriptionRequest {
	return &web_push_servicepb.CreatePushSubscriptionRequest{
		Endpoint: "https://push.test/" + name,
		P256Dh:   fmt.Sprintf("test-p256dh-%d", generation), Auth: fmt.Sprintf("test-auth-%d", generation),
	}
}

func (p *pushServed) subscribe(t *testing.T, token string, request *web_push_servicepb.CreatePushSubscriptionRequest) {
	t.Helper()
	if _, err := p.push.CreatePushSubscription(as(t, token), request); err != nil {
		t.Fatal(err)
	}
}

func (p *pushServed) list(t *testing.T, token string) []*web_push_servicepb.PushSubscription {
	t.Helper()
	listed, err := p.push.ListPushSubscriptions(as(t, token), &web_push_servicepb.ListPushSubscriptionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return listed.GetPushSubscriptions()
}

func endpoints(subscriptions []*web_push_servicepb.PushSubscription) []string {
	var listed []string
	for _, subscription := range subscriptions {
		listed = append(listed, subscription.GetEndpoint())
	}
	return listed
}

func TestAnOperatorHoldsOnlyThePushSubscriptionsItRegistered(t *testing.T) {
	p := servePush(t, serve(t), true)
	held := subscription("test-held", 1)
	p.subscribe(t, p.operatorToken, held)
	registered := p.list(t, p.operatorToken)
	if got := endpoints(registered); len(got) != 1 || got[0] != held.GetEndpoint() || registered[0].UserAgent != nil {
		t.Fatalf("subscriptions %v, want %s with no user agent over gRPC", registered, held.GetEndpoint())
	}

	if got := p.list(t, p.otherOperatorToken); len(got) != 0 {
		t.Errorf("another operator lists %v", got)
	}
	_, err := p.push.CreatePushSubscription(as(t, p.otherOperatorToken), subscription("test-held", 2))
	wantCode(t, "registering another operator's endpoint", err, codes.AlreadyExists)
	_, err = p.push.DeletePushSubscription(as(t, p.otherOperatorToken), &web_push_servicepb.DeletePushSubscriptionRequest{Endpoint: held.GetEndpoint()})
	wantCode(t, "deleting another operator's subscription", err, codes.NotFound)

	// Registering again replaces the keys and keeps the subscription.
	p.subscribe(t, p.operatorToken, subscription("test-held", 2))
	if got := p.list(t, p.operatorToken); len(got) != 1 || !got[0].GetCreatedAt().AsTime().Equal(registered[0].GetCreatedAt().AsTime()) {
		t.Errorf("subscriptions %v after registering again, want %v", got, registered)
	}
	var keys string
	if err := p.db.QueryRowContext(t.Context(), `SELECT p256dh || ' ' || auth FROM action_push_subscription WHERE endpoint = $1`,
		held.GetEndpoint()).Scan(&keys); err != nil || keys != "test-p256dh-2 test-auth-2" {
		t.Errorf("keys %q, %v; want the second registration's", keys, err)
	}

	if _, err := p.push.DeletePushSubscription(as(t, p.operatorToken), &web_push_servicepb.DeletePushSubscriptionRequest{Endpoint: held.GetEndpoint()}); err != nil {
		t.Fatal(err)
	}
	_, err = p.push.DeletePushSubscription(as(t, p.operatorToken), &web_push_servicepb.DeletePushSubscriptionRequest{Endpoint: held.GetEndpoint()})
	wantCode(t, "deleting a subscription again", err, codes.NotFound)
}

// call is the HTTP facade's answer to the operator's call of method on path, with the body as JSON when it is not nil.
func (p *pushServed) call(t *testing.T, method, path string, body any, header http.Header) (int, []byte) {
	t.Helper()
	var content io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		content = strings.NewReader(string(encoded))
	}
	request, err := http.NewRequestWithContext(as(t, p.operatorToken), method, p.pushURL+path, content)
	if err != nil {
		t.Fatal(err)
	}
	for name, values := range header {
		request.Header[name] = values
	}
	request.Header.Set("Authorization", "Bearer "+p.operatorToken)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	answer, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, answer
}

func TestTheHTTPFacadeKeepsTheBrowsersUserAgentAndDeletesByQuery(t *testing.T) {
	p := servePush(t, serve(t), true)
	code, body := p.call(t, http.MethodGet, "/v1/operator/push/config", nil, nil)
	config := &web_push_servicepb.GetPushConfigResponse{}
	if err := protojson.Unmarshal(body, config); code != http.StatusOK || err != nil || config.GetApplicationServerKey() != p.applicationServerKey {
		t.Errorf("config: %d %s, %v; want key %s", code, body, err, p.applicationServerKey)
	}

	endpoint := "https://push.test/test-browser?test-query=1"
	agent := strings.Repeat("test-agent/", 30)
	code, body = p.call(t, http.MethodPost, "/v1/operator/push/subscriptions",
		map[string]string{"endpoint": endpoint, "p256dh": "test-p256dh", "auth": "test-auth"},
		http.Header{"User-Agent": {agent}})
	if code != http.StatusOK {
		t.Fatalf("create: %d %s", code, body)
	}
	if got := p.list(t, p.operatorToken); len(got) != 1 || got[0].GetUserAgent() != agent[:userAgentLimit] {
		t.Errorf("subscriptions %v, want the user agent cut to %d characters", got, userAgentLimit)
	}

	code, body = p.call(t, http.MethodDelete, "/v1/operator/push/subscriptions?endpoint="+url.QueryEscape(endpoint), nil, nil)
	if code != http.StatusOK {
		t.Fatalf("delete: %d %s", code, body)
	}
	if got := p.list(t, p.operatorToken); len(got) != 0 {
		t.Errorf("subscriptions %v after the delete", got)
	}
}

func TestWebPushRefusesWhatItCannotServe(t *testing.T) {
	s := serve(t)
	configured, unconfigured := servePush(t, s, true), servePush(t, s, false)

	_, err := configured.push.ListPushSubscriptions(as(t, callerToken), &web_push_servicepb.ListPushSubscriptionsRequest{})
	wantCode(t, "a workload's call", err, codes.Unauthenticated)
	for name, request := range map[string]*web_push_servicepb.CreatePushSubscriptionRequest{
		"no endpoint":           {P256Dh: "test-p256dh", Auth: "test-auth"},
		"an endpoint too long":  {Endpoint: "https://push.test/" + strings.Repeat("é", 2048), P256Dh: "test-p256dh", Auth: "test-auth"},
		"no p256dh":             {Endpoint: "https://push.test/test-browser", Auth: "test-auth"},
		"a p256dh too long":     {Endpoint: "https://push.test/test-browser", P256Dh: strings.Repeat("k", 201), Auth: "test-auth"},
		"no auth":               {Endpoint: "https://push.test/test-browser", P256Dh: "test-p256dh"},
		"an auth too long":      {Endpoint: "https://push.test/test-browser", P256Dh: "test-p256dh", Auth: strings.Repeat("a", 201)},
		"an unreviewed service": {Endpoint: "https://unreviewed.test/test-browser", P256Dh: "test-p256dh", Auth: "test-auth"},
		"an endpoint not HTTPS": {Endpoint: "http://push.test/test-browser", P256Dh: "test-p256dh", Auth: "test-auth"},
	} {
		_, err := configured.push.CreatePushSubscription(as(t, s.operatorToken), request)
		wantCode(t, name, err, codes.InvalidArgument)
	}

	_, err = unconfigured.push.CreatePushSubscription(as(t, s.operatorToken), subscription("test-browser", 1))
	wantCode(t, "a subscription while Web Push is not configured", err, codes.FailedPrecondition)
	for p, want := range map[*pushServed]string{configured: configured.applicationServerKey, unconfigured: ""} {
		config, err := p.push.GetPushConfig(as(t, s.operatorToken), &web_push_servicepb.GetPushConfigRequest{})
		if err != nil || (config.ApplicationServerKey != nil) != (want != "") || config.GetApplicationServerKey() != want {
			t.Errorf("config %v, %v; want key %q", config, err, want)
		}
	}
}

type pushWatch = grpc.ServerStreamingClient[web_push_servicepb.WatchPushSubscriptionsResponse]

func (p *pushServed) watch(t *testing.T, token string) pushWatch {
	t.Helper()
	stream, err := p.push.WatchPushSubscriptions(as(t, token), &web_push_servicepb.WatchPushSubscriptionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

func nextPushFrame(t *testing.T, stream pushWatch) *web_push_servicepb.WatchPushSubscriptionsResponse {
	t.Helper()
	frame, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	return frame
}

func TestASubscriptionWatchSendsTheOperatorsSubscriptionsThenEachChange(t *testing.T) {
	p := servePush(t, serve(t), true)
	subscriptions := push.NewSubscriptions(p.db)
	held := snapshotChunk + 1
	for i := range held {
		if err := subscriptions.Save(t.Context(), p.issuer.operator(), push.Registration{
			Endpoint: fmt.Sprintf("https://push.test/test-held-%03d", i), P256dh: "test-p256dh", Auth: "test-auth",
		}); err != nil {
			t.Fatal(err)
		}
	}
	p.subscribe(t, p.otherOperatorToken, subscription("test-elsewhere", 1))

	stream := p.watch(t, p.operatorToken)
	var chunks []int
	for snapshot := true; snapshot; {
		switch frame := nextPushFrame(t, stream).GetFrame().(type) {
		case *web_push_servicepb.WatchPushSubscriptionsResponse_Snapshot:
			for _, subscription := range frame.Snapshot.GetPushSubscriptions() {
				if !strings.HasPrefix(subscription.GetEndpoint(), "https://push.test/test-held-") {
					t.Errorf("the snapshot shows %s", subscription.GetEndpoint())
				}
			}
			chunks = append(chunks, len(frame.Snapshot.GetPushSubscriptions()))
		case *web_push_servicepb.WatchPushSubscriptionsResponse_SnapshotComplete:
			snapshot = false
		default:
			t.Fatalf("a %T frame before snapshot_complete", frame)
		}
	}
	if len(chunks) != 2 || chunks[0] != snapshotChunk || chunks[1] != 1 {
		t.Errorf("snapshot chunks of %v subscriptions, want %d then 1", chunks, snapshotChunk)
	}

	// Each change the operator cannot see sends nothing, so the frame after them is the change it can.
	p.subscribe(t, p.otherOperatorToken, subscription("test-elsewhere", 2))
	p.subscribe(t, p.operatorToken, subscription("test-added", 1))
	if got := nextPushFrame(t, stream).GetUpsert(); got.GetEndpoint() != "https://push.test/test-added" || got.GetCreatedAt() == nil {
		t.Errorf("upsert %v, want test-added", got)
	}
	p.subscribe(t, p.operatorToken, subscription("test-added", 2))
	if _, err := p.push.DeletePushSubscription(as(t, p.operatorToken), &web_push_servicepb.DeletePushSubscriptionRequest{
		Endpoint: "https://push.test/test-held-000",
	}); err != nil {
		t.Fatal(err)
	}
	if got := nextPushFrame(t, stream).GetRemovedEndpoint(); got != "https://push.test/test-held-000" {
		t.Errorf("removed %q, want test-held-000", got)
	}

	p.gate.Drain()
	if frame := nextPushFrame(t, stream); frame.GetReconnectRequired() == nil {
		t.Errorf("frame %v, want reconnect_required", frame)
	}
	wantEnd(t, "the watch after reconnect_required", stream, codes.OK)
}

func TestASubscriptionWatchEndsUnauthenticatedOnceItsBearerExpires(t *testing.T) {
	p := servePush(t, serve(t), true)
	// Past its expiry, but within the clock skew for a few seconds more.
	lapsing := p.issuer.token(t, map[string]any{"exp": time.Now().Add(-clockSkew + 5*time.Second).Unix()})
	stream := p.watch(t, lapsing)
	if frame := nextPushFrame(t, stream); frame.GetSnapshotComplete() == nil {
		t.Fatalf("frame %v, want snapshot_complete", frame)
	}
	wantEnd(t, "a lapsed operator token's watch", stream, codes.Unauthenticated)
}
