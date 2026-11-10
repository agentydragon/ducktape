package main

import (
	"context"
	"crypto/x509"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
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
	service := start(t, func(c *config) {
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
	})
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
		conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(credentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
		defer cancel()
		response, err := notificationspb.NewNotificationsClient(conn).ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
		if err != nil {
			t.Fatal(err)
		}
		_, mixedErr := notificationspb.NewNotificationsClient(conn).ListInboxes(
			metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+sandboxToken), &notificationspb.ListInboxesRequest{},
		)
		if status.Code(mixedErr) != codes.Unauthenticated {
			t.Fatalf("SVID plus bearer authenticated: %v", mixedErr)
		}
		want := 1 - i
		if len(response.GetInboxes()) != want {
			t.Fatalf("caller %s sees %d inboxes, want %d", caller.Sandbox.Name, len(response.GetInboxes()), want)
		}
		if i == 0 {
			caller.Delete(t)
			denied, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			_, err = notificationspb.NewNotificationsClient(conn).ListInboxes(denied, &notificationspb.ListInboxesRequest{})
			cancel()
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox authenticated on existing TLS connection: %v", err)
			}
		}
	}
}

// An Identity caller owns the inboxes of its Sandbox's Sessions the Session authority recorded as
// executing as its Identity incarnation; creating subscriptions and messaging still need a
// ServiceAccount.
func TestSVIDIdentityCallerOwnsItsSessionsInboxes(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-identity-"+uuid.NewString(), testNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, "svid-identity-"+uuid.NewString(), testNamespace)
	ca := environmentruntimetransporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.NotificationsURI, x509.ExtKeyUsageServerAuth)
	service := start(t, func(c *config) {
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
	})
	inboxes := store.New(service.db, defaultConfig().Quotas)
	ref := model.DestinationRef{Namespace: testNamespace, Name: workload.Sandbox.Name, UID: string(workload.Sandbox.UID)}
	if err := inboxes.OpenInbox(t.Context(), model.ServiceAccount{Namespace: testNamespace, Name: workload.Sandbox.Name}, ref, "svid-identity-session", workload.Principal.ServiceAccountUID); err != nil {
		t.Fatal(err)
	}
	if err := inboxes.EstablishInboxIdentity(t.Context(), ref, "svid-identity-session", model.Identity{Name: workload.Identity.Name, UID: string(workload.Identity.UID)}); err != nil {
		t.Fatal(err)
	}
	connect := func(caller workloadauthtesting.Workload) notificationspb.NotificationsClient {
		credentials, err := ca.Issue(t, caller.URI).ClientCredentials(environment_runtime_transport.NotificationsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(credentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		return notificationspb.NewNotificationsClient(conn)
	}
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	for caller, want := range map[*workloadauthtesting.IdentityWorkload]int{&workload: 1, &other: 0} {
		response, err := connect(caller.Workload).ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
		if err != nil {
			t.Fatal(err)
		}
		if got := len(response.GetInboxes()); got != want {
			t.Fatalf("Identity %s sees %d inboxes, want %d", caller.Identity.Name, got, want)
		}
	}
	_, err := connect(workload.Workload).SendMessage(ctx, &notificationspb.SendMessageRequest{SessionId: "svid-identity-session", Body: "test"})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("an Identity caller sent a message: %v", err)
	}
}
