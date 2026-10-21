package linkage

import (
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

// Server is an MCP server an operator links an account to: an entry of `mcp_servers`, with the keys of the Python
// service's McpOAuthServer (mcp_settings.py).
type Server struct {
	ServerID  string `mapstructure:"server_id"`
	ServerURL string `mapstructure:"server_url"`
	// AuthorizationEndpoint and TokenEndpoint are used where discovery finds no authorization server metadata.
	AuthorizationEndpoint *string `mapstructure:"authorization_endpoint"`
	TokenEndpoint         *string `mapstructure:"token_endpoint"`
	// ClientID is the client registered with the server's authorization server; nil when it uses the shared Client
	// ID Metadata Document instead.
	ClientID      *string `mapstructure:"client_id"`
	UseSharedCIMD bool    `mapstructure:"use_shared_cimd"`
	// ClientSecretFile holds the registered client's secret, read at each token request; nil for a public client.
	ClientSecretFile *string `mapstructure:"client_secret_file"`
	RedirectURI      string  `mapstructure:"redirect_uri"`
	// Scopes are those a link may request; empty takes them from the server's metadata.
	Scopes []string `mapstructure:"scopes"`
	// Resource is the RFC 8707 resource requested where the server's protected resource metadata names none that
	// covers the server; nil requests the server's URL.
	Resource *string `mapstructure:"resource"`
}

// ClientMetadata is the client identity the servers that use the shared Client ID Metadata Document share:
// `mcp_client_metadata`. The zero value is none.
type ClientMetadata struct {
	// URL is the document's own URL, which is its client_id: https://<host>/oauth/client-metadata.json.
	URL        string `mapstructure:"url"`
	ClientName string `mapstructure:"client_name"`
}

// Validate refuses settings the Python service refuses, and a server whose key is not its server_id or whose URL is
// not an http(s) URL. Its errors name keys, never a value.
func Validate(servers map[string]Server, metadata ClientMetadata) error {
	sharesCIMD := false
	for key, server := range servers {
		switch {
		case !catalog.IsKey(key):
			return errors.New("mcp_servers: a server key is not a lowercase identifier")
		case server.ServerID != key:
			return fmt.Errorf("mcp_servers.%s.server_id must be the server's key", key)
		case !httpURL(server.ServerURL):
			return fmt.Errorf("mcp_servers.%s.server_url must be an http(s) URL", key)
		case server.RedirectURI == "":
			return fmt.Errorf("mcp_servers.%s.redirect_uri is required", key)
		case (server.ClientID != nil) == server.UseSharedCIMD:
			return fmt.Errorf("mcp_servers.%s: configure exactly one of client_id or use_shared_cimd", key)
		case server.ClientID != nil && *server.ClientID == "":
			return fmt.Errorf("mcp_servers.%s.client_id must not be empty", key)
		case server.UseSharedCIMD && server.ClientSecretFile != nil:
			return fmt.Errorf("mcp_servers.%s: a shared CIMD client authenticates publicly and has no client secret", key)
		}
		sharesCIMD = sharesCIMD || server.UseSharedCIMD
	}
	if sharesCIMD != (metadata != ClientMetadata{}) {
		return errors.New("configure mcp_client_metadata exactly when an MCP server uses the shared CIMD")
	}
	if sharesCIMD {
		if !canonicalDocumentURL(metadata.URL) {
			return fmt.Errorf("mcp_client_metadata.url must be the canonical HTTPS URL of %s, without a port", ClientMetadataPath)
		}
		if metadata.ClientName == "" {
			return errors.New("mcp_client_metadata.client_name is required")
		}
	}
	return nil
}

func httpURL(s string) bool {
	u, err := url.Parse(s)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != ""
}

// canonicalDocumentURL is whether s is https://<host>/oauth/client-metadata.json written as Python's URL type writes
// it back: the client_id an authorization server fetches must be the document's own URL, byte for byte.
func canonicalDocumentURL(s string) bool {
	u, err := url.Parse(s)
	return err == nil && u.Scheme == "https" && u.User == nil && u.Host != "" && u.Port() == "" &&
		u.Host == strings.ToLower(u.Host) && u.Path == ClientMetadataPath && u.RawQuery == "" && !u.ForceQuery &&
		u.Fragment == "" && u.String() == s
}
