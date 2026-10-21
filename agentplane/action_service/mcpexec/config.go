package mcpexec

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"golang.org/x/oauth2"
)

// server is the MCP server a group's binding configures, reached over MCP's streamable HTTP transport. Its fields and
// their meaning are the Python service's (McpServerConfig in mcp_executor.py), which reads the same settings and also
// accepts a stdio command, which no deployment configures.
type server struct {
	URL string `json:"url"`
	// Headers are sent on every request beside the credential, such as the toolset selection a server keys on.
	Headers map[string]string `json:"headers"`
	// credential is Authorization's: none, the linked account's token, or a mounted bearer token.
	credential credential
}

// credential is how a server is authorized: noCredential, linkedAccount or mountedBearer.
type credential interface{ isCredential() }

type noCredential struct{}

// linkedAccount is the token of the account an operator linked to the server serverID through OAuth.
type linkedAccount struct{ serverID string }

// mountedBearer is a bearer token read from a file.
type mountedBearer struct{ path string }

func (noCredential) isCredential()  {}
func (linkedAccount) isCredential() {}
func (mountedBearer) isCredential() {}

// linkedAccountOf is the linked account that authorizes s, if one does.
func linkedAccountOf(s server) (linkedAccount, bool) {
	account, linked := s.credential.(linkedAccount)
	return account, linked
}

// parseServer reads an MCP binding's config, refusing what the Python service refuses: an unknown field, a null
// where a value is required, and an Authorization header, which the credential owns. Its errors name fields, never
// a value: the config can hold a credential.
func parseServer(config json.RawMessage) (server, error) {
	var tag struct {
		Transport *string `json:"transport"`
		Auth      *string `json:"auth"`
	}
	if err := json.Unmarshal(config, &tag); err != nil || tag.Transport == nil || *tag.Transport != "streamable-http" {
		return server{}, errors.New("transport must be streamable-http")
	}
	if tag.Auth == nil {
		return server{}, errors.New("auth must be none, oauth or static_bearer")
	}
	var fields struct {
		server
		Transport  string  `json:"transport"`
		Auth       string  `json:"auth"`
		ServerID   *string `json:"server_id"`
		BearerFile *string `json:"bearer_file"`
	}
	if err := decodeStrict(config, &fields, "server_id", "bearer_file"); err != nil {
		return server{}, err
	}
	s := fields.server
	switch *tag.Auth {
	case "none":
		if fields.ServerID != nil || fields.BearerFile != nil {
			return server{}, errors.New("auth none takes no server_id or bearer_file")
		}
		s.credential = noCredential{}
	case "oauth":
		if fields.ServerID == nil || !catalog.IsKey(*fields.ServerID) || fields.BearerFile != nil {
			return server{}, errors.New("auth oauth takes a server_id that is a key, and no bearer_file")
		}
		s.credential = linkedAccount{serverID: *fields.ServerID}
	case "static_bearer":
		if fields.BearerFile == nil || fields.ServerID != nil {
			return server{}, errors.New("auth static_bearer takes a bearer_file, and no server_id")
		}
		s.credential = mountedBearer{path: *fields.BearerFile}
	default:
		return server{}, errors.New("auth must be none, oauth or static_bearer")
	}
	if err := checkEndpoint(s.URL); err != nil {
		return server{}, err
	}
	for name := range s.Headers {
		if strings.EqualFold(name, "Authorization") {
			return server{}, errors.New("headers must not set Authorization; that is owned by auth")
		}
	}
	return s, nil
}

// decodeStrict decodes config into fields, refusing a field fields does not have and a null in any field but those
// nullable names.
func decodeStrict(config json.RawMessage, fields any, nullable ...string) error {
	var values map[string]json.RawMessage
	if err := json.Unmarshal(config, &values); err != nil {
		return errors.New("config must be a JSON object")
	}
	for name, value := range values {
		if string(value) == "null" && !slices.Contains(nullable, name) {
			return fmt.Errorf("%s must not be null", name)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(config))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(fields); err != nil {
		// The decoder's errors name the field; a type error can quote the value.
		var typeError *json.UnmarshalTypeError
		if errors.As(err, &typeError) {
			// The field is named within the decoded struct, which embeds the server's own.
			return fmt.Errorf("%s has the wrong type", strings.TrimPrefix(typeError.Field, "server."))
		}
		return errors.New("config holds a field this transport does not take")
	}
	return nil
}

// checkEndpoint refuses a URL the Python service's HttpEndpointUrl refuses: one that is not http or https, or has no
// host, or carries credentials, a query or a fragment.
func checkEndpoint(endpoint string) error {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		return errors.New("url must be an http or https URL with a host")
	}
	if u.User != nil || u.RawQuery != "" || u.ForceQuery || strings.Contains(endpoint, "#") {
		return errors.New("url must not carry credentials, a query or a fragment")
	}
	return nil
}

// transport reaches the server for one session. A credential it needs is read here, so that one mounted after the
// service started is used by the next attempt; lifecycle bounds each read of a linkage.
func (s server) transport(linkages Linkages, lifecycle time.Duration) (mcp.Transport, error) {
	transport := &mcp.StreamableClientTransport{
		Endpoint:   s.URL,
		HTTPClient: &http.Client{Transport: headers{values: s.Headers, next: http.DefaultTransport}},
	}
	switch credential := s.credential.(type) {
	case linkedAccount:
		transport.OAuthHandler = linkedToken{linkages: linkages, serverID: credential.serverID, bound: lifecycle}
	case mountedBearer:
		token, err := os.ReadFile(credential.path)
		if err != nil {
			return nil, errors.New("configured MCP static bearer file is unavailable")
		}
		if len(bytes.TrimSpace(token)) == 0 {
			return nil, errors.New("configured MCP static bearer file is empty")
		}
		transport.OAuthHandler = staticToken{token: strings.TrimSpace(string(token))}
	}
	return transport, nil
}

// headers sets the configured headers on each request.
type headers struct {
	values map[string]string
	next   http.RoundTripper
}

func (h headers) RoundTrip(request *http.Request) (*http.Response, error) {
	if len(h.values) == 0 {
		return h.next.RoundTrip(request)
	}
	request = request.Clone(request.Context())
	for name, value := range h.values {
		request.Header.Set(name, value)
	}
	return h.next.RoundTrip(request)
}

// staticToken authorizes every request with a mounted bearer token.
type staticToken struct{ token string }

func (s staticToken) TokenSource(context.Context) (oauth2.TokenSource, error) {
	return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: s.token}), nil
}

func (staticToken) Authorize(_ context.Context, _ *http.Request, response *http.Response) error {
	return refused(response)
}

// linkedToken authorizes each request with the token the server's linkage holds when the request is sent, so a
// refreshed token is used from the next request on.
type linkedToken struct {
	linkages Linkages
	serverID string
	// bound bounds the read: go-sdk asks with the session's context, which has no deadline.
	bound time.Duration
}

func (l linkedToken) TokenSource(ctx context.Context) (oauth2.TokenSource, error) {
	ctx, cancel := context.WithTimeout(ctx, l.bound)
	defer cancel()
	token, err := requestToken(ctx, l.linkages, l.serverID)
	if err != nil {
		return nil, err
	}
	return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: token}), nil
}

func (linkedToken) Authorize(_ context.Context, _ *http.Request, response *http.Response) error {
	return refused(response)
}

// refused is the end of a request the server refused as unauthorized: this service runs no authorization flow of
// its own, so the credential stays refused until it is replaced.
func refused(response *http.Response) error {
	response.Body.Close()
	return fmt.Errorf("the MCP server refused the credential: %s", response.Status)
}
