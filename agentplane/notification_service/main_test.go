package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"database/sql"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testAudience  = "test-notification-service"
	testNamespace = "test-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second

	sandboxName = "test-sandbox"
	sandboxUID  = "test-sandbox-uid"
	otherName   = "test-other"
	testSession = "test-session"
	webhookKey  = "test-webhook-secret-of-sixteen-or-more"
)

// serveGRPC serves a fake peer on a loopback port and returns its address.
func serveGRPC(t *testing.T, register func(*grpc.Server)) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	register(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	return listener.Addr().String()
}

// listingRunner is a Sandbox's runner, as the control Service reaches it.
type listingRunner struct {
	runnerservicepb.UnimplementedRunnerServer
}

func (listingRunner) ListSessions(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	return &runnerpb.ListSessionsResponse{Sessions: []*runnerpb.SessionSummary{{SessionId: testSession}}}, nil
}

type testActionService struct {
	actionpb.UnimplementedActionServiceServer
}

func (testActionService) GetActionRequest(context.Context, *actionpb.GetActionRequestRequest) (*actionpb.GetActionRequestResponse, error) {
	return &actionpb.GetActionRequestResponse{ActionRequest: &actionpb.ActionRequest{
		Caller: &sandboxespb.ServiceAccount{Namespace: testNamespace, Name: sandboxName},
	}}, nil
}

func (testActionService) ListActionRequestEvents(context.Context, *actionpb.ListActionRequestEventsRequest) (*actionpb.ListActionRequestEventsResponse, error) {
	return &actionpb.ListActionRequestEventsResponse{}, nil
}

// running is a served Notification Service over the shared database, emptied of earlier tests' inboxes.
type running struct {
	notifications notificationspb.NotificationsClient
	health        healthpb.HealthClient
	// url is the base URL of the service's HTTP routes.
	url string
	db  *sql.DB
	// actionsToken is the Action Service token file.
	actionsToken string
	// wait returns what serve returned, once it has; stop asks it to stop first.
	wait, stop func() error
}

func testDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// start serves over a database emptied of earlier tests' inboxes, with settings for it and fake peers; tune
// adjusts the rest.
func start(t *testing.T, tune func(*config)) running {
	t.Helper()
	if _, err := testDatabase(t).ExecContext(t.Context(), `TRUNCATE inbox CASCADE`); err != nil {
		t.Fatal(err)
	}
	return restart(t, tune)
}

// restart serves over the rows the database holds: a new process, which only they carry over to.
func restart(t *testing.T, tune func(*config)) running {
	t.Helper()
	db := testDatabase(t)
	tokens := t.TempDir()
	tokenFile := func(name string) string {
		path := filepath.Join(tokens, name)
		if err := os.WriteFile(path, []byte("test-peer-token\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	actionAddress := serveGRPC(t, func(s *grpc.Server) { actionpb.RegisterActionServiceServer(s, testActionService{}) })
	runnerAddress := serveGRPC(t, func(s *grpc.Server) { runnerservicepb.RegisterRunnerServer(s, listingRunner{}) })
	historyAddress := serveGRPC(t, func(s *grpc.Server) {
		historypb.RegisterHistoryServiceServer(s, historypb.UnimplementedHistoryServiceServer{})
	})
	dispatcherAddress := serveGRPC(t, func(s *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(s, dispatcherpb.UnimplementedSessionDispatcherServer{})
	})

	c := defaultConfig()
	c.DatabaseURL = integrationDatabase.URL
	c.Namespace, c.TokenAudience = testNamespace, testAudience
	c.Actions = actionPeer{URL: "http://" + actionAddress, TokenFile: tokenFile("actions"), RequestTimeoutSeconds: 5}
	c.RunnerGateway = runnerGateway{ControlTarget: runnerAddress, TokenFile: tokenFile("gateway"), RequestTimeoutSeconds: 5}
	c.HistoryService = peer{Target: historyAddress, TokenFile: tokenFile("history"), RequestTimeoutSeconds: 5}
	c.SessionDispatcher = peer{Target: dispatcherAddress, TokenFile: tokenFile("dispatcher"), RequestTimeoutSeconds: 5}
	if tune != nil {
		tune(&c)
	}
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	deps := kubeDeps{
		Reviews: kube.TokenReviews(),
		Sandboxes: clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(
			&sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sandboxName, UID: sandboxUID}},
		).Build(),
		Policies: apiConfig,
	}
	grpcListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- serve(ctx, c, deps, grpcListener, httpListener) }()
	var result error
	var received sync.Once
	wait := func() error {
		received.Do(func() {
			select {
			case result = <-served:
			case <-time.After(callDeadline):
				result = errors.New("the service did not stop")
			}
		})
		return result
	}
	stop := func() error {
		cancel()
		return wait()
	}
	t.Cleanup(func() { _ = stop() })
	connection, err := grpc.NewClient(grpcListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return running{
		notifications: notificationspb.NewNotificationsClient(connection), health: healthpb.NewHealthClient(connection),
		url: "http://" + httpListener.Addr().String(), db: db, actionsToken: c.Actions.TokenFile, wait: wait, stop: stop,
	}
}

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

func checkHealth(t *testing.T, client healthpb.HealthClient, service string) (healthpb.HealthCheckResponse_ServingStatus, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	response, err := client.Check(ctx, &healthpb.HealthCheckRequest{Service: service})
	return response.GetStatus(), err
}

func TestTheServiceAnswersHealthChecksAndCallsAndStopsWhenTold(t *testing.T) {
	service := start(t, nil)
	for _, check := range []struct {
		service string
		want    healthpb.HealthCheckResponse_ServingStatus
	}{{"", healthpb.HealthCheckResponse_SERVING}, {probes.LivenessService, healthpb.HealthCheckResponse_SERVING}} {
		if got, err := checkHealth(t, service.health, check.service); err != nil || got != check.want {
			t.Errorf("health of %q = %v, %v; want %v", check.service, got, err, check.want)
		}
	}
	if _, err := checkHealth(t, service.health, "test-unknown-service"); status.Code(err) != codes.NotFound {
		t.Errorf("health of an unknown service: %v", err)
	}
	// A call is admitted by its bearer and served from the database.
	if _, err := service.notifications.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{}); err != nil {
		t.Errorf("list subscriptions: %v", err)
	}
	_, err := service.notifications.ListSubscriptions(bearing(t, "test-token-unknown"), &notificationspb.ListSubscriptionsRequest{})
	if status.Code(err) != codes.Unauthenticated {
		t.Errorf("list subscriptions with an unknown bearer: %v", err)
	}
	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Errorf("serve returned %v, want the cancellation that stopped it", err)
	}
	if _, err := checkHealth(t, service.health, ""); err == nil {
		t.Error("the service answered after it stopped")
	}
}

func TestAWorkerThatStopsEndsTheServiceWithoutSayingWhy(t *testing.T) {
	service := start(t, nil)
	_, err := service.notifications.CreateSubscription(bearing(t, sandboxToken), &notificationspb.CreateSubscriptionRequest{
		DestinationRef: &notificationspb.DestinationRef{Namespace: testNamespace, Name: sandboxName, Uid: sandboxUID},
		SessionId:      testSession, IdempotencyKey: "test-key",
		Source: &notificationspb.Source{Provider: &notificationspb.Source_Actions{
			Actions: &notificationspb.ActionsSource{RequestId: uuid.NewString()},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	// A token the Action Service cannot be read with is no failure of the source to show on the subscription: it
	// stops the worker.
	if err := os.Remove(service.actionsToken); err != nil {
		t.Fatal(err)
	}
	for _, query := range []string{`UPDATE inbox SET next_attempt = now()`, `UPDATE subscription SET next_attempt = now()`} {
		if _, err := service.db.ExecContext(t.Context(), query); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := service.db.ExecContext(t.Context(), `SELECT pg_notify('agentplane_notification_work', '')`); err != nil {
		t.Fatal(err)
	}
	var stopped workerStopped
	err = service.wait()
	if !errors.As(err, &stopped) {
		t.Fatalf("serve returned %v, want a worker that stopped", err)
	}
	if strings.Contains(err.Error(), service.actionsToken) || !strings.Contains(err.Error(), "*worker.UnclassifiedProviderError") {
		t.Errorf("the failure names the token path or not the source failure's type: %v", err)
	}
	if _, err := checkHealth(t, service.health, ""); err == nil {
		t.Error("the service answered after a worker stopped")
	}
}

// webhookServer starts the service with GitHub configured by an App whose key is generated for the test.
func webhookServer(t *testing.T) running {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return start(t, func(c *config) {
		c.GitHub.AppID, c.GitHub.WebhookSecret = 42, webhookKey
		c.GitHub.PrivateKey = string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
	})
}

func postWebhook(t *testing.T, service running, signature string) int {
	t.Helper()
	body := []byte(`{}`)
	if signature == "" {
		mac := hmac.New(sha256.New, []byte(webhookKey))
		mac.Write(body)
		signature = "sha256=" + hex.EncodeToString(mac.Sum(nil))
	}
	request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, service.url+"/v1/webhooks/github", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("X-GitHub-Event", "ping")
	request.Header.Set("X-GitHub-Delivery", uuid.NewString())
	request.Header.Set("X-Hub-Signature-256", signature)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	return response.StatusCode
}

func TestTheWebhookIsServedBesideGRPCOnlyWhenGitHubIsConfigured(t *testing.T) {
	service := webhookServer(t)
	if got := postWebhook(t, service, ""); got != http.StatusAccepted {
		t.Errorf("a signed ping = %d, want 202", got)
	}
	if got := postWebhook(t, service, "sha256="+strings.Repeat("0", 64)); got != http.StatusUnauthorized {
		t.Errorf("a ping with another signature = %d, want 401", got)
	}
	if got, err := checkHealth(t, service.health, ""); err != nil || got != healthpb.HealthCheckResponse_SERVING {
		t.Errorf("gRPC beside the webhook: %v, %v", got, err)
	}
	if got := postWebhook(t, start(t, nil), ""); got != http.StatusNotFound {
		t.Errorf("a ping without GitHub configured = %d, want 404", got)
	}
}
