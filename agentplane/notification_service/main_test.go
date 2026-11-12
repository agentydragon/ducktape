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

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	notificationtesting "github.com/agentydragon/ducktape/agentplane/notification_service/testing"
	"github.com/agentydragon/ducktape/agentplane/postgres/probes"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/runtime"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience  = "test-notification-service"
	testNamespace = "test-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second

	sandboxName = "test-sandbox"
	otherName   = "test-other"
	testSession = "test-session"
	webhookKey  = "test-webhook-secret-of-sixteen-or-more"
)

type testActionService struct {
	actionpb.UnimplementedActionServiceServer
}

func (testActionService) GetActionRequest(ctx context.Context, _ *actionpb.GetActionRequestRequest) (*actionpb.GetActionRequestResponse, error) {
	account, err := kube.ServiceAccount(ctx, auth.Account{Namespace: testNamespace, Name: sandboxName})
	if err != nil {
		return nil, err
	}
	uid := string(account.UID)
	return &actionpb.GetActionRequestResponse{ActionRequest: &actionpb.ActionRequest{
		CallerUid: &uid,
		Caller:    &identitypb.ServiceAccount{Namespace: testNamespace, Name: sandboxName},
	}}, nil
}

func (testActionService) ListActionRequestEvents(context.Context, *actionpb.ListActionRequestEventsRequest) (*actionpb.ListActionRequestEventsResponse, error) {
	return &actionpb.ListActionRequestEventsResponse{}, nil
}

// running is a served Notification Service over the shared database, emptied of earlier tests' inboxes.
type running struct {
	notifications notificationspb.NotificationsClient
	health        healthpb.HealthClient
	// url is the service's HTTP listener base URL, used for the signed GitHub webhook.
	url        string
	rpcAddress string
	db         *sql.DB
	// actionsToken is the Action Service token file.
	actionsToken string
	// wait returns what serve returned, once it has; stop asks it to stop first.
	wait, stop func() error
}

func testDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db := postgres.Open(t, integrationDatabase.URL)
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
	actionAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) { actionpb.RegisterActionServiceServer(s, testActionService{}) })
	runnerAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) {
		environmentruntimepb.RegisterEnvironmentRuntimeServer(s, &notificationtesting.ListingRunner{Sessions: []string{testSession}})
	})
	historyAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) {
		historypb.RegisterHistoryServiceServer(s, historypb.UnimplementedHistoryServiceServer{})
	})
	dispatcherAddress := notificationtesting.ServeGRPC(t, func(s *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(s, openingDispatcher{})
	})

	c := defaultConfig()
	c.DatabaseURL = integrationDatabase.URL
	c.Namespace, c.TokenAudience = testNamespace, testAudience
	c.RPC.Auth = []listener.Authentication{&listener.KubernetesBearerAuth{Audiences: []string{testAudience}}}
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
	sandboxes, err := client.New(apiConfig, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	deps := kubeDeps{Reviews: kube.TokenReviews(), Sandboxes: sandboxes, Policies: apiConfig}
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
	security, err := (*environment_runtime_transport.ClientTLS)(nil).Credentials()
	if c.RPC.TLS != nil {
		for _, method := range c.RPC.Auth {
			if spiffe, ok := method.(*listener.SPIFFEAuth); ok {
				security, err = (&environment_runtime_transport.ClientTLS{CAFile: spiffe.CAFile, ServerURI: environment_runtime_transport.NotificationsURI}).Credentials()
			}
		}
	}
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(grpcListener.Addr().String(), grpc.WithTransportCredentials(security))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return running{
		notifications: notificationspb.NewNotificationsClient(connection), health: healthpb.NewHealthClient(connection),
		url: "http://" + httpListener.Addr().String(), rpcAddress: grpcListener.Addr().String(), db: db, actionsToken: c.Actions.TokenFile, wait: wait, stop: stop,
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
		SessionId: testSession, IdempotencyKey: "test-key",
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

func TestOnlyConfiguredGitHubServesTheSignedWebhookBesideGRPC(t *testing.T) {
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
	for _, path := range []string{"/v1/subscriptions", "/openapi.json", "/llms.txt"} {
		response, err := http.Get(service.url + path)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", path, response.StatusCode)
		}
	}
	if got := postWebhook(t, start(t, nil), ""); got != http.StatusNotFound {
		t.Errorf("a ping without GitHub configured = %d, want 404", got)
	}
}
