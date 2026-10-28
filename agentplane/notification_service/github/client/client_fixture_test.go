package client

import (
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/github/testing" // Shared fake API server; avoids colliding with Go testing.
)

// newTestClient serves fake over HTTP and returns a client for it; edit adjusts the settings first.
func newTestClient(t *testing.T, fake *fixture.FakeGitHub, edit ...func(*settings.Settings)) *Client {
	t.Helper()
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	settings := fixture.Settings(server.URL + fake.Prefix)
	for _, apply := range edit {
		apply(&settings)
	}
	apiClient, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}
	return apiClient
}
