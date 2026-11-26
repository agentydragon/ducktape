package server

import (
	"testing"

	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
)

// The test server hosts the RPCs a case exercises. Start the gateways through the same wiring as Run.
func appFacadesForTest(t *testing.T, address string, config settings.Config) streamhttp.Dependencies {
	t.Helper()
	origin := "http://" + address
	config.ActionFederation.ServiceURL = origin
	config.ConnectionServiceURL = origin
	config.NotificationsURL = origin
	config.EgressAdminURL = origin
	config.SessionDispatcherTarget = address
	config.PolicyServiceTarget = address
	config.HistoryServiceTarget = address
	var streams streamhttp.Dependencies
	if err := configureFacades(t.Context(), config, &streams); err != nil {
		t.Fatal(err)
	}
	return streams
}
