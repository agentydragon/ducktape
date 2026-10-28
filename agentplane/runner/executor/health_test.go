package executor

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner/executor/control"
)

func TestHealthUnavailable(t *testing.T) {
	for _, behavior := range []string{"absent", "malformed", "stalled"} {
		t.Run(behavior, func(t *testing.T) {
			socket := filepath.Join(t.TempDir(), "control.sock")
			if behavior != "absent" {
				listener, err := net.Listen("unix", socket)
				if err != nil {
					t.Fatal(err)
				}
				defer listener.Close()
				release := make(chan struct{})
				done := make(chan error, 1)
				go func() {
					connection, err := listener.Accept()
					if err != nil {
						done <- err
						return
					}
					defer connection.Close()
					var request control.Request
					if err := json.NewDecoder(connection).Decode(&request); err != nil {
						done <- err
						return
					}
					if request.Operation != "inspect" || len(request.Environment) != 0 {
						done <- fmt.Errorf("health sent control request %+v", request)
						return
					}
					if behavior == "malformed" {
						_, err = connection.Write([]byte("not-json\n"))
					} else {
						<-release
					}
					done <- err
				}()
				defer func() {
					close(release)
					_ = listener.Close()
					if err := <-done; err != nil {
						t.Error(err)
					}
				}()
			}
			response := httptest.NewRecorder()
			healthHandler(socket).ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))
			if response.Code != http.StatusServiceUnavailable {
				t.Fatalf("health with %s control = %d, want 503", behavior, response.Code)
			}
		})
	}
}

func TestHealthExposesNoControlOperations(t *testing.T) {
	for _, request := range []struct {
		method string
		path   string
		status int
	}{
		{http.MethodPost, "/healthz", http.StatusMethodNotAllowed},
		{http.MethodGet, "/claim", http.StatusNotFound},
		{http.MethodGet, "/fence", http.StatusNotFound},
	} {
		response := httptest.NewRecorder()
		healthHandler("").ServeHTTP(response, httptest.NewRequest(request.method, request.path, nil))
		if response.Code != request.status {
			t.Errorf("%s %s = %d, want %d", request.method, request.path, response.Code, request.status)
		}
	}
}
