package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/wait"
)

func TestSVIDListenerPreservesCallerLabelsAndRequestOwnership(t *testing.T) {
	owner := workloadauthtesting.NewWorkload(t, kube, policyNamespace, "svid-"+uuid.NewString())
	other := workloadauthtesting.NewWorkload(t, kube, policyNamespace, "svid-"+uuid.NewString())
	for _, workload := range []workloadauthtesting.Workload{owner, other} {
		setCallerLabel(t, workload.Principal.Account, true)
	}
	ca := transporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.ActionsURI, x509.ExtKeyUsageServerAuth)
	service := startConfigured(t, kube.Config, &catalog.Catalog{}, func(c *config, _ *deps) {
		c.WorkloadTLS, c.WorkloadPort = &serving, 8082
	})
	waitReady(t, service)
	_, err := service.store.Submit(t.Context(), store.CallerPrincipal{
		Account: store.ServiceAccount(owner.Principal.Account), ServiceAccountUID: owner.Principal.ServiceAccountUID,
	}, store.ActionRequestInput{
		IdempotencyKey: "svid-own-request", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{}`), Title: "owned request",
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	for i, workload := range []workloadauthtesting.Workload{owner, other} {
		config := ca.Issue(t, workload.URI)
		credentials, err := config.ClientCredentials(environment_runtime_transport.ActionsURI)
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
		ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken, "x-agentplane-subject", policyNamespace+"/"+owner.Sandbox.Name)
		response, err := action_servicepb.NewActionServiceClient(conn).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
		if err != nil || len(response.GetActionRequests()) != 1-i {
			t.Fatalf("SVID caller %s reads %v, error %v", workload.Sandbox.Name, response, err)
		}
		security, err := config.ClientTLSConfig(environment_runtime_transport.ActionsURI)
		if err != nil {
			t.Fatal(err)
		}
		transport := &http.Transport{TLSClientConfig: security, ForceAttemptHTTP2: true}
		defer transport.CloseIdleConnections()
		client := &http.Client{Transport: transport, Timeout: callDeadline}
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://"+service.workloadAddress+"/v1/action-requests", nil)
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Authorization", "Bearer "+readerToken)
		result, err := client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		var body struct {
			ActionRequests []json.RawMessage `json:"actionRequests"`
		}
		err = json.NewDecoder(result.Body).Decode(&body)
		result.Body.Close()
		if err != nil || result.StatusCode != http.StatusOK || len(body.ActionRequests) != 1-i {
			t.Fatalf("HTTP SVID ownership status=%d body=%v error=%v", result.StatusCode, body, err)
		}
		if i == 0 {
			watch, err := action_servicepb.NewActionServiceClient(conn).WatchActionRequests(ctx, &action_servicepb.WatchActionRequestsRequest{})
			if err != nil {
				t.Fatal(err)
			}
			for {
				frame, err := watch.Recv()
				if err != nil {
					t.Fatal(err)
				}
				if frame.GetSnapshotComplete() != nil {
					break
				}
			}
			if err := workload.Objects.Delete(t.Context(), workload.Sandbox); err != nil {
				t.Fatal(err)
			}
			_, err = action_servicepb.NewActionServiceClient(conn).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox on existing TLS connection: %v", err)
			}
			if _, err := watch.Recv(); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox on existing watch: %v", err)
			}
		} else {
			setCallerLabel(t, workload.Principal.Account, false)
			if err := wait.PollUntilContextTimeout(ctx, 20*time.Millisecond, 5*time.Second, true, func(ctx context.Context) (bool, error) {
				_, err := action_servicepb.NewActionServiceClient(conn).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
				return status.Code(err) == codes.Unauthenticated, nil
			}); err != nil {
				t.Fatal("SVID caller retained access after caller-label removal", err)
			}
		}
	}
	// The server rejects these leaves at TLS, before its ActionService handler or
	// generated HTTP facade can inspect the request or the forged bearer.
	valid := ca.Issue(t, other.URI)
	for name, leaf := range map[string]environment_runtime_transport.Config{
		"forged":  transporttesting.NewCA(t).Issue(t, other.URI),
		"expired": ca.IssueWithValidity(t, other.URI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth),
	} {
		t.Run(name, func(t *testing.T) {
			security, err := valid.ClientTLSConfig(environment_runtime_transport.ActionsURI)
			if err != nil {
				t.Fatal(err)
			}
			pair, err := tls.LoadX509KeyPair(leaf.CertFile, leaf.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			security.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &pair, nil }
			transport := &http.Transport{TLSClientConfig: security, ForceAttemptHTTP2: true}
			defer transport.CloseIdleConnections()
			client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
			request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, "https://"+service.workloadAddress+"/v1/action-requests", nil)
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Authorization", "Bearer "+readerToken)
			response, err := client.Do(request)
			if response != nil {
				response.Body.Close()
			}
			if err == nil {
				t.Fatal("invalid leaf reached HTTP handler")
			}
		})
	}
}
