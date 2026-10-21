package main

import (
	"context"
	"crypto/ecdh"
	"crypto/rand"
	"crypto/x509"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// A configured service pushes a request awaiting a Decision to each subscribed browser, and serves WebPushService
// behind the operator's bearer: no operator is configured here, so it refuses every bearer.
func TestTheServiceDeliversWebPushAndGatesWebPushService(t *testing.T) {
	pushed := make(chan string, 1)
	pushService := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case pushed <- r.URL.Path + " " + r.Header.Get("Content-Encoding"):
		default:
		}
		w.WriteHeader(http.StatusCreated)
	}))
	t.Cleanup(pushService.Close)
	transport := pushService.Client().Transport.(*http.Transport).Clone()
	// The name httptest's certificate is issued for; every push service address reaches the stand-in.
	transport.TLSClientConfig.ServerName = "example.com"
	transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, pushService.Listener.Addr().String())
	}
	vapid, err := ecdh.P256().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(vapid)
	if err != nil {
		t.Fatal(err)
	}
	var databaseURL string
	service := startConfigured(t, kube.Config, &catalog.Catalog{}, func(c *config, d *deps) {
		c.WebPush = push.Settings{
			PrivateKeyPEM: string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})),
			Subject:       "mailto:test-vapid@agentplane.test", PublicBaseURL: "https://app.test/",
			AllowedPushHosts: []string{"push.test"},
		}
		d.PushTransport = transport
		databaseURL = c.DatabaseURL
	})

	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	browser, err := ecdh.P256().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	auth := make([]byte, 16)
	if _, err := rand.Read(auth); err != nil {
		t.Fatal(err)
	}
	if err := push.NewSubscriptions(db).Save(t.Context(), testOperator, push.Registration{
		Endpoint: "https://push.test/test-browser",
		P256dh:   base64.RawURLEncoding.EncodeToString(browser.PublicKey().Bytes()), Auth: base64.RawURLEncoding.EncodeToString(auth),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}},
		store.ActionRequestInput{
			IdempotencyKey: "test-awaiting", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
			Arguments: json.RawMessage(`{}`), Title: "test title",
		}, store.Admission{RequestID: uuid.New()}); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-pushed:
		if got != "/test-browser aes128gcm" {
			t.Errorf("push %q, want an aes128gcm push to /test-browser", got)
		}
	case <-time.After(callDeadline):
		t.Fatal("no push arrived")
	}

	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	connection, err := grpc.NewClient(strings.TrimPrefix(service.url, "http://"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	_, err = web_push_servicepb.NewWebPushServiceClient(connection).GetPushConfig(
		metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken), &web_push_servicepb.GetPushConfigRequest{},
	)
	if status.Code(err) != codes.Unauthenticated {
		t.Errorf("a reader's GetPushConfig over gRPC: %v", err)
	}
	call, err := http.NewRequestWithContext(ctx, http.MethodGet, service.url+"/v1/operator/push/config", nil)
	if err != nil {
		t.Fatal(err)
	}
	call.Header.Set("Authorization", "Bearer "+readerToken)
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Errorf("a reader's GetPushConfig over HTTP: %d", response.StatusCode)
	}
}
