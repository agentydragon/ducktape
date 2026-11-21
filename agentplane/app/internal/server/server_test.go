package server

import (
	"net"
	"net/http"
	"net/http/httptest"
	"testing"

	sessionhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/sessions"
	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func TestSystemStatusUsesConnectionServiceAppSVID(t *testing.T) {
	plainListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	plainServer := grpc.NewServer()
	plainHealth := health.NewServer()
	plainHealth.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(plainServer, plainHealth)
	go func() { _ = plainServer.Serve(plainListener) }()
	t.Cleanup(func() {
		plainServer.Stop()
		_ = plainListener.Close()
	})

	ca := peertlstesting.NewCA(t)
	connectionServerCertificate := ca.Issue(t, peertls.MCPEdgeURI)
	serverCredentials, err := connectionServerCertificate.ServerCredentials(peertls.AppURI)
	if err != nil {
		t.Fatal(err)
	}
	connectionListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	connectionServer := grpc.NewServer(grpc.Creds(serverCredentials))
	connectionHealth := health.NewServer()
	connectionHealth.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(connectionServer, connectionHealth)
	go func() { _ = connectionServer.Serve(connectionListener) }()
	t.Cleanup(func() {
		connectionServer.Stop()
		_ = connectionListener.Close()
	})

	appCertificate := ca.Issue(t, peertls.AppURI)
	config := settings.Config{
		RunnerGatewayTarget:  plainListener.Addr().String(),
		HistoryServiceTarget: plainListener.Addr().String(),
		ActionFederation: settings.ActionFederationConfig{
			ServiceURL: "http://" + plainListener.Addr().String(),
		},
		ConnectionServiceURL: "http://" + connectionListener.Addr().String(),
		ConnectionServiceTLS: &peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.MCPEdgeURI,
			CertFile: appCertificate.CertFile, KeyFile: appCertificate.KeyFile, ClientURI: peertls.AppURI,
		},
		NotificationsURL: "http://" + plainListener.Addr().String(),
		EgressAdminURL:   "http://" + plainListener.Addr().String(),
	}
	checker, connections, err := newSystemStatus(config, &http.Client{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { closeConnections(connections) })

	for _, result := range checker.CheckAll(t.Context()) {
		if result.Name == "Connection Service" && result.State == "ok" {
			return
		}
		if result.Name == "Connection Service" {
			t.Fatalf("Connection Service health over App SVID = %+v", result)
		}
	}
	t.Fatal("system status did not include the Connection Service probe")
}

func TestHTTPServerHealthAndReadiness(t *testing.T) {
	server := newHTTPServer(nil)
	handler := server.Handler(nil)

	for _, path := range []string{"/healthz", "/readyz"} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, path, nil))
		if recorder.Code != http.StatusNoContent {
			t.Fatalf("GET %s status = %d, want 204", path, recorder.Code)
		}
	}

	server.BeginDrain()
	select {
	case <-server.DrainDone():
	default:
		t.Fatal("server drain signal did not close when drain began")
	}
	server.BeginDrain() // Repeated signal handling must remain safe.
	if server.Ready() {
		t.Fatal("server reports ready after drain began")
	}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("GET /readyz while draining status = %d, want 503", recorder.Code)
	}
}

func TestNonUUIDIDSegmentMatchesNoRoute(t *testing.T) {
	server := newHTTPServer(nil)
	if err := (&sessionhttp.Handler{}).RegisterRoutes(server); err != nil {
		t.Fatal(err)
	}
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{})
	server.RegisterRawRoute("/threads/{thread_id}/sync/{rest...}", http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("non-UUID sync route reached its handler")
	}), false)
	handler := server.Handler(nil)
	for _, request := range []struct{ method, path string }{
		{http.MethodGet, "/threads/form/sync/v1/shape"},
		// uuid.Parse accepts this compact spelling; route converters do not.
		{http.MethodGet, "/threads/9b6d568a225343d7a7d4190e98359594/sync/v1/shape"},
	} {
		t.Run(request.method+" "+request.path, func(t *testing.T) {
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest(request.method, request.path, nil))
			if response.Code != http.StatusNotFound {
				t.Fatalf("%s %s status = %d, want 404; body: %s", request.method, request.path, response.Code, response.Body.String())
			}
		})
	}
	for _, request := range []struct{ method, path string }{
		{http.MethodGet, "/upstream/notifications/v1/sessions/example/inbox:watch"},
	} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(request.method, request.path, nil))
		if response.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want authentication boundary", request.method, request.path, response.Code)
		}
	}
}
