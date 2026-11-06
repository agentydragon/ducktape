package main

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestSVIDListenerKeepsInboxOwnershipAndRejectsDeletedSandbox(t *testing.T) {
	workload := workloadauthtesting.NewWorkload(t, kube, testNamespace, "svid-"+uuid.NewString())
	other := workloadauthtesting.NewWorkload(t, kube, testNamespace, "svid-"+uuid.NewString())
	ca := environmentruntimetransporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.NotificationsURI, x509.ExtKeyUsageServerAuth)
	service := start(t, func(c *config) { c.WorkloadTLS = &serving; c.WorkloadPort = 8082 })
	inboxes := store.New(service.db, defaultConfig().Quotas)
	ref := model.DestinationRef{Namespace: testNamespace, Name: workload.Sandbox.Name, UID: string(workload.Sandbox.UID)}
	if err := inboxes.OpenInbox(t.Context(), model.ServiceAccount{Namespace: testNamespace, Name: workload.Sandbox.Name}, ref, "svid-session", workload.Principal.ServiceAccountUID); err != nil {
		t.Fatal(err)
	}
	for i, caller := range []workloadauthtesting.Workload{workload, other} {
		config := ca.Issue(t, caller.URI)
		credentials, err := config.ClientCredentials(environment_runtime_transport.NotificationsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.workloadAddress, grpc.WithTransportCredentials(credentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
		defer cancel()
		ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+sandboxToken, "x-agentplane-subject", testNamespace+"/"+workload.Sandbox.Name)
		response, err := notificationspb.NewNotificationsClient(conn).ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
		if err != nil {
			t.Fatal(err)
		}
		want := 1 - i
		if len(response.GetInboxes()) != want {
			t.Fatalf("caller %s sees %d inboxes, want %d", caller.Sandbox.Name, len(response.GetInboxes()), want)
		}
		tlsConfig, err := config.ClientTLSConfig(environment_runtime_transport.NotificationsURI)
		if err != nil {
			t.Fatal(err)
		}
		transport := &http.Transport{TLSClientConfig: tlsConfig, ForceAttemptHTTP2: true}
		defer transport.CloseIdleConnections()
		client := &http.Client{Transport: transport, Timeout: callDeadline}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://"+service.workloadAddress+"/v1/inboxes", nil)
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Authorization", "Bearer "+sandboxToken)
		result, err := client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		var body struct {
			Inboxes []json.RawMessage `json:"inboxes"`
		}
		err = json.NewDecoder(result.Body).Decode(&body)
		result.Body.Close()
		if err != nil || result.StatusCode != http.StatusOK || len(body.Inboxes) != want {
			t.Fatalf("HTTP caller %s status=%d inboxes=%d err=%v", caller.Sandbox.Name, result.StatusCode, len(body.Inboxes), err)
		}
		if i == 0 {
			if err := caller.Objects.Delete(t.Context(), caller.Sandbox); err != nil {
				t.Fatal(err)
			}
			denied, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			_, err = notificationspb.NewNotificationsClient(conn).ListInboxes(denied, &notificationspb.ListInboxesRequest{})
			cancel()
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox authenticated on existing TLS connection: %v", err)
			}
		}
	}
}
