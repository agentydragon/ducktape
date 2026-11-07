package workloadfacade_test

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/facade"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var api *kubeapiserver.Server

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error { api = server; return nil }))
}

func TestGeneratedHTTPFacadeVerifiesTheSVIDBeforeUnaryHandlers(t *testing.T) {
	for _, kind := range []string{"valid", "forged", "expired", "server-only"} {
		t.Run(kind, func(t *testing.T) {
			workload := workloadauthtesting.NewWorkload(t, api, "http-svid-"+uuid.NewString(), "sandbox")
			ca := environmentruntimetransporttesting.NewCA(t)
			service := &notifications{expected: workload.Principal}
			handler, serverTLS := newHandler(t, workload, ca, service)
			server := httptest.NewUnstartedServer(handler)
			server.TLS = serverTLS
			server.EnableHTTP2 = true
			server.StartTLS()
			defer server.Close()
			config := ca.Issue(t, workload.URI)
			clientTLS, err := config.ClientTLSConfig(environment_runtime_transport.HistoryIngressURI)
			if err != nil {
				t.Fatal(err)
			}
			presented := config
			switch kind {
			case "forged":
				presented = environmentruntimetransporttesting.NewCA(t).Issue(t, workload.URI)
			case "expired":
				presented = ca.IssueWithValidity(t, workload.URI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth)
			case "server-only":
				presented = ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageServerAuth)
			}
			pair, err := tls.LoadX509KeyPair(presented.CertFile, presented.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			clientTLS.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &pair, nil }
			transport := &http.Transport{TLSClientConfig: clientTLS, ForceAttemptHTTP2: true}
			defer transport.CloseIdleConnections()
			client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
			request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, server.URL+"/v1/subscriptions", nil)
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Authorization", "Bearer forged")
			request.Header.Set("X-Agentplane-Subject", "test/attacker")
			response, err := client.Do(request)
			if kind != "valid" {
				if response != nil {
					response.Body.Close()
				}
				if err == nil || service.calls.Load() != 0 {
					t.Fatalf("%s leaf reached handler: err=%v calls=%d", kind, err, service.calls.Load())
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			body, err := io.ReadAll(response.Body)
			if err != nil || response.StatusCode != http.StatusOK || service.calls.Load() != 1 {
				t.Fatalf("response=%s body=%q error=%v calls=%d", response.Status, body, err, service.calls.Load())
			}
			// The HTTP boundary cannot be reached through a plaintext listener.
			plain := httptest.NewServer(handler)
			defer plain.Close()
			denied, err := http.Get(plain.URL + "/v1/subscriptions")
			if err != nil {
				t.Fatal(err)
			}
			denied.Body.Close()
			if denied.StatusCode != http.StatusUnauthorized || service.calls.Load() != 1 {
				t.Fatal("plaintext request reached handler")
			}
		})
	}
}

func TestGeneratedWatchKeepsTLSCallerAndRevalidatesLiveSandbox(t *testing.T) {
	workload := workloadauthtesting.NewWorkload(t, api, "http-watch-"+uuid.NewString(), "sandbox")
	ca := environmentruntimetransporttesting.NewCA(t)
	revalidate := make(chan struct{})
	service := &notifications{expected: workload.Principal, revalidate: revalidate}
	handler, serverTLS := newHandler(t, workload, ca, service)
	server := httptest.NewUnstartedServer(handler)
	server.TLS = serverTLS
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()
	config := ca.Issue(t, workload.URI)
	clientTLS, err := config.ClientTLSConfig(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	transport := &http.Transport{TLSClientConfig: clientTLS, ForceAttemptHTTP2: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, server.URL+"/v1/sessions/test/inbox:watch", nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	scanner := bufio.NewScanner(response.Body)
	if !scanner.Scan() || !strings.Contains(scanner.Text(), `"result"`) {
		t.Fatalf("first Watch frame=%q error=%v", scanner.Text(), scanner.Err())
	}
	if err := workload.Objects.Delete(t.Context(), workload.Sandbox); err != nil {
		t.Fatal(err)
	}
	close(revalidate)
	if !scanner.Scan() || !strings.Contains(scanner.Text(), `"code":16`) {
		t.Fatalf("revoked Watch frame=%q error=%v", scanner.Text(), scanner.Err())
	}
	if service.calls.Load() != 1 {
		t.Fatalf("handler calls=%d", service.calls.Load())
	}
}

func newHandler(t *testing.T, workload workloadauthtesting.Workload, ca *environmentruntimetransporttesting.CA, service *notifications) (http.Handler, *tls.Config) {
	t.Helper()
	svid, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{
		notificationspb.Notifications_ListSubscriptions_FullMethodName: auth.AnyAccount(svid),
		notificationspb.Notifications_WatchInbox_FullMethodName:        auth.AnyAccount(svid),
	}
	handler, err := workloadfacade.NewHandler(t.Context(), workloadfacade.Interceptors{Unary: methods.UnaryInterceptor(time.Second), Stream: methods.StreamInterceptor(time.Second)}, facade.HandlerOptions{RejectUnknownJSONFields: true}, workloadfacade.Registration{
		Server: func(registrar grpc.ServiceRegistrar) { notificationspb.RegisterNotificationsServer(registrar, service) },
		HTTP: func(ctx context.Context, mux *runtime.ServeMux, channel grpc.ClientConnInterface) error {
			return notificationspb.RegisterNotificationsHandlerClient(ctx, mux, notificationspb.NewNotificationsClient(channel))
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	config := ca.Issue(t, environment_runtime_transport.HistoryIngressURI)
	serverTLS, err := config.WorkloadServerTLSConfig(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.Certificates = []tls.Certificate{pair} // Prevent httptest's default localhost leaf.
	return handler, serverTLS
}

type notifications struct {
	notificationspb.UnimplementedNotificationsServer
	expected   auth.Principal
	calls      atomic.Int32
	revalidate <-chan struct{}
}

func (s *notifications) check(ctx context.Context) error {
	caller, err := auth.Caller(ctx)
	if err != nil {
		return err
	}
	if caller != s.expected {
		return status.Error(codes.PermissionDenied, fmt.Sprintf("unexpected caller %v", caller.Account))
	}
	s.calls.Add(1)
	return nil
}

func (s *notifications) ListSubscriptions(ctx context.Context, _ *notificationspb.ListSubscriptionsRequest) (*notificationspb.ListSubscriptionsResponse, error) {
	if err := s.check(ctx); err != nil {
		return nil, err
	}
	return &notificationspb.ListSubscriptionsResponse{}, nil
}

func (s *notifications) WatchInbox(_ *notificationspb.WatchInboxRequest, stream grpc.ServerStreamingServer[notificationspb.WatchInboxResponse]) error {
	if err := s.check(stream.Context()); err != nil {
		return err
	}
	if err := stream.Send(&notificationspb.WatchInboxResponse{}); err != nil {
		return err
	}
	select {
	case <-s.revalidate:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	return auth.RevalidateStream(stream.Context())
}
