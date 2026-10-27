package github

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	testAppID         = 42
	testSHA           = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testBaseSHA       = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	installationToken = "fixture-installation-token"
	tokenPath         = "/app/installations/11/access_tokens"
)

var testKey = sync.OnceValue(func() *rsa.PrivateKey {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		panic(err)
	}
	return key
})

func testKeyPEM() string {
	der, err := x509.MarshalPKCS8PrivateKey(testKey())
	if err != nil {
		panic(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
}

type fakeResponse struct {
	status int
	header map[string]string
	body   string
}

// fakeGitHub serves the endpoints the client reads, and fails the test on any request that does not carry the
// headers and credentials GitHub requires: a valid App JWT on the installation and token endpoints, the
// installation token on everything else, and an all-read permission set when minting.
type fakeGitHub struct {
	t          *testing.T
	apiVersion string
	// prefix is the path the API is served under, as for GitHub Enterprise.
	prefix string
	// stall holds the response to a path until the client gives up.
	stall string

	// handler, when set, answers the requests that pass the checks and have no override, ahead of the default
	// endpoints. It returns false for a request it leaves to them.
	handler func(w http.ResponseWriter, r *http.Request, path string) bool

	mu       sync.Mutex
	requests []string
	// uris are the request targets as the client sent them, with escapes intact.
	uris      []string
	responses map[string]fakeResponse
}

func newFake(t *testing.T) *fakeGitHub {
	t.Helper()
	return &fakeGitHub{t: t, apiVersion: "2022-11-28", responses: map[string]fakeResponse{}}
}

// respond overrides what path answers, for every method.
func (f *fakeGitHub) respond(path string, status int, header map[string]string, body string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.responses[path] = fakeResponse{status: status, header: header, body: body}
}

func (f *fakeGitHub) restore(path string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.responses, path)
}

func (f *fakeGitHub) count(path string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, requested := range f.requests {
		if requested == path {
			n++
		}
	}
	return n
}

func (f *fakeGitHub) requestURIs() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.uris)
}

// forget drops the requests recorded so far.
func (f *fakeGitHub) forget() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests, f.uris = nil, nil
}

func (f *fakeGitHub) total() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.requests)
}

func (f *fakeGitHub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path, ok := strings.CutPrefix(r.URL.Path, f.prefix)
	if !ok {
		f.t.Errorf("request %q is outside the API prefix %q", r.URL.Path, f.prefix)
	}
	if got := r.Header.Get("Accept"); got != "application/vnd.github+json" {
		f.t.Errorf("%s: Accept = %q", path, got)
	}
	if got := r.Header.Get("X-GitHub-Api-Version"); got != f.apiVersion {
		f.t.Errorf("%s: X-GitHub-Api-Version = %q, want %q", path, got, f.apiVersion)
	}
	f.mu.Lock()
	f.requests = append(f.requests, path)
	f.uris = append(f.uris, r.RequestURI)
	override, overridden := f.responses[path]
	f.mu.Unlock()
	// A server only notices that the client hung up once it has read the request body.
	payload, err := io.ReadAll(r.Body)
	if err != nil {
		f.t.Errorf("%s: reading the request body: %v", path, err)
	}
	if path == f.stall {
		<-r.Context().Done()
		return
	}
	if strings.HasSuffix(path, "/installation") || strings.HasSuffix(path, "/access_tokens") {
		f.requireAppJWT(path, r)
	} else if got := r.Header.Get("Authorization"); got != "token "+installationToken {
		f.t.Errorf("%s: Authorization is not the installation token", path)
	}
	if overridden {
		for name, value := range override.header {
			w.Header().Set(name, value)
		}
		w.WriteHeader(override.status)
		fmt.Fprint(w, override.body)
		return
	}
	if f.handler != nil && f.handler(w, r, path) {
		return
	}
	f.serve(w, r, path, payload)
}

func (f *fakeGitHub) serve(w http.ResponseWriter, r *http.Request, path string, payload []byte) {
	reply := func(status int, body string) {
		w.WriteHeader(status)
		fmt.Fprint(w, body)
	}
	last := path[strings.LastIndex(path, "/")+1:]
	switch {
	case strings.HasSuffix(path, "/installation"):
		reply(http.StatusOK, `{"id": 11}`)
	case strings.HasSuffix(path, "/access_tokens"):
		var body struct{ Permissions map[string]string }
		if err := json.Unmarshal(payload, &body); err != nil {
			f.t.Errorf("token request body: %v", err)
		}
		if len(body.Permissions) != 4 {
			f.t.Errorf("token permissions = %v, want metadata, contents, pull_requests and issues", body.Permissions)
		}
		for name, level := range body.Permissions {
			if level != "read" {
				f.t.Errorf("token permission %s = %q, want read", name, level)
			}
		}
		reply(http.StatusCreated, fmt.Sprintf(`{"token": %q, "expires_at": %q}`,
			installationToken, time.Now().Add(time.Hour).UTC().Format(time.RFC3339)))
	case path == "/repos/owner/repo" || path == "/repositories/100":
		reply(http.StatusOK, `{"id": 100, "full_name": "owner/repo"}`)
	case path == "/repos/owner/repo/pulls/7":
		reply(http.StatusOK, fmt.Sprintf(`{"number": 7, "state": "open", "merged": false, "base": {"ref": "main", "sha": %q}, "mergeable": true, "head": {"sha": %q, "repo": {"id": 200, "full_name": "fork/repo"}}}`, testBaseSHA, testSHA))
	case path == "/repos/owner/repo/issues/8":
		reply(http.StatusOK, `{"number": 8}`)
	case path == "/repos/owner/repo/git/ref/heads/devel", path == "/repos/owner/repo/git/ref/heads/feature/x":
		reply(http.StatusOK, fmt.Sprintf(`{"object": {"sha": %q}}`, testSHA))
	case strings.HasPrefix(path, "/repos/owner/repo/git/ref/heads/"):
		reply(http.StatusNotFound, `{"message": "Not Found"}`)
	case strings.HasPrefix(path, "/repos/owner/repo/commits/"):
		reply(http.StatusOK, fmt.Sprintf(`{"sha": %q}`, last))
	default:
		f.t.Errorf("unexpected request %s %s", r.Method, path)
		reply(http.StatusInternalServerError, "")
	}
}

// requireAppJWT checks the RS256 signature and the claims GitHub requires of an App JWT.
func (f *fakeGitHub) requireAppJWT(path string, r *http.Request) {
	token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	parts := strings.Split(token, ".")
	if !ok || len(parts) != 3 {
		f.t.Errorf("%s: Authorization is not a bearer JWT", path)
		return
	}
	var header struct{ Alg string }
	var claims struct {
		Iss string
		Iat int64
		Exp int64
	}
	for i, target := range []any{&header, &claims} {
		raw, err := base64.RawURLEncoding.DecodeString(parts[i])
		if err == nil {
			err = json.Unmarshal(raw, target)
		}
		if err != nil {
			f.t.Errorf("%s: JWT part %d: %v", path, i, err)
			return
		}
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		f.t.Errorf("%s: JWT signature: %v", path, err)
		return
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(&testKey().PublicKey, crypto.SHA256, digest[:], signature); err != nil {
		f.t.Errorf("%s: JWT signature does not verify: %v", path, err)
	}
	now := time.Now().Unix()
	if header.Alg != "RS256" || claims.Iss != fmt.Sprint(testAppID) || claims.Iat > now || claims.Exp <= now {
		f.t.Errorf("%s: JWT header %+v claims %+v", path, header, claims)
	}
}

func testSettings(apiURL string) Settings {
	settings := DefaultSettings()
	settings.APIURL = apiURL
	settings.AppID = testAppID
	settings.PrivateKey = testKeyPEM()
	settings.WebhookSecret = "test-webhook-secret!"
	return settings
}

// newTestClient serves fake over HTTP and returns a client for it; edit adjusts the settings first.
func newTestClient(t *testing.T, fake *fakeGitHub, edit ...func(*Settings)) *Client {
	t.Helper()
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	settings := testSettings(server.URL + fake.prefix)
	for _, apply := range edit {
		apply(&settings)
	}
	client, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

// asError requires err to be, or wrap, an E, and returns it.
func asError[E error](t *testing.T, err error) E {
	t.Helper()
	var target E
	if !errors.As(err, &target) {
		t.Fatalf("error = %v (%T), want %T", err, err, target)
	}
	return target
}
