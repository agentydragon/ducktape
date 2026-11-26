package client

import (
	"net/http/httptest"
	"testing"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/settings"
	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/testing" // Shared fake API server; avoids colliding with Go testing.
)

func testRevision(sha string) githubmodel.HeadRevision {
	return githubmodel.HeadRevision{Repository: githubmodel.Repository{ID: 100, Name: "owner/repo"}, SHA: sha}
}

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
