package linkage

import "testing"

func TestValidateRefusesWhatThePythonServiceRefuses(t *testing.T) {
	registered := func() Server {
		return Server{
			ServerID: "test-server", ServerURL: "https://test-mcp.example/mcp", ClientID: new("test-client"),
			RedirectURI: "https://test-actions.example/callback",
		}
	}
	metadata := ClientMetadata{URL: "https://test-actions.example/oauth/client-metadata.json", ClientName: "Test application"}
	if err := Validate(map[string]Server{"test-server": registered()}, ClientMetadata{}); err != nil {
		t.Errorf("a registered client: %v", err)
	}
	shared := registered()
	sharedClient(&shared)
	if err := Validate(map[string]Server{"test-server": shared}, metadata); err != nil {
		t.Errorf("a shared client: %v", err)
	}
	for name, tc := range map[string]struct {
		server   func(*Server)
		metadata ClientMetadata
	}{
		"a server_id that is not the key": {server: func(s *Server) { s.ServerID = "test-other" }},
		"a relative server_url":           {server: func(s *Server) { s.ServerURL = "/mcp" }},
		"a server_url of another scheme":  {server: func(s *Server) { s.ServerURL = "ftp://test-mcp.example/mcp" }},
		"no redirect_uri":                 {server: func(s *Server) { s.RedirectURI = "" }},
		"no client":                       {server: func(s *Server) { s.ClientID = nil }},
		"an empty client_id":              {server: func(s *Server) { s.ClientID = new("") }},
		"both clients":                    {server: func(s *Server) { s.UseSharedCIMD = true }, metadata: metadata},
		"a shared client with a secret": {server: func(s *Server) {
			s.ClientID, s.UseSharedCIMD, s.ClientSecretFile = nil, true, new("/test/secret")
		}, metadata: metadata},
		"a shared client without metadata":     {server: sharedClient},
		"metadata no server uses":              {server: func(*Server) {}, metadata: metadata},
		"metadata without a client_name":       {server: sharedClient, metadata: ClientMetadata{URL: metadata.URL}},
		"metadata at another path":             {server: sharedClient, metadata: ClientMetadata{URL: "https://test-actions.example/client.json", ClientName: "t"}},
		"metadata over plain HTTP":             {server: sharedClient, metadata: ClientMetadata{URL: "http://test-actions.example/oauth/client-metadata.json", ClientName: "t"}},
		"metadata with an explicit port":       {server: sharedClient, metadata: ClientMetadata{URL: "https://test-actions.example:443/oauth/client-metadata.json", ClientName: "t"}},
		"metadata with an upper case host":     {server: sharedClient, metadata: ClientMetadata{URL: "https://Test-Actions.example/oauth/client-metadata.json", ClientName: "t"}},
		"metadata with a query":                {server: sharedClient, metadata: ClientMetadata{URL: metadata.URL + "?v=1", ClientName: "t"}},
		"metadata with credentials in its URL": {server: sharedClient, metadata: ClientMetadata{URL: "https://test-user@test-actions.example/oauth/client-metadata.json", ClientName: "t"}},
	} {
		server := registered()
		tc.server(&server)
		if err := Validate(map[string]Server{"test-server": server}, tc.metadata); err == nil {
			t.Errorf("%s is accepted", name)
		}
	}
	if err := Validate(map[string]Server{"Test_Server": registered()}, ClientMetadata{}); err == nil {
		t.Error("a key that is not a lowercase identifier is accepted")
	}
}

func sharedClient(s *Server) { s.ClientID, s.UseSharedCIMD = nil, true }
