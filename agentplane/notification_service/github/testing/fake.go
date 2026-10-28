package testing

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
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
)

const (
	AppID             = 42
	SHA               = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	BaseSHA           = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	OtherBaseSHA      = "dddddddddddddddddddddddddddddddddddddddd"
	MergeCommitSHA    = "cccccccccccccccccccccccccccccccccccccccc"
	InstallationToken = "fixture-installation-token"
	TokenPath         = "/app/installations/11/access_tokens"
)

var testKey = sync.OnceValue(func() *rsa.PrivateKey {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		panic(err)
	}
	return key
})

func KeyPEM() string {
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

// FakeGitHub serves the endpoints the client reads, and fails the test on any request that does not carry the
// headers and credentials GitHub requires: a valid App JWT on the installation and token endpoints, the
// installation token on everything else, and the exact read permissions for the selected profile when minting.
type FakeGitHub struct {
	t          *testing.T
	APIVersion string
	// Prefix is the path the API is served under, as for GitHub Enterprise.
	Prefix string
	// Stall holds the response to a path until the client gives up.
	Stall string

	// Handler, when set, answers the requests that pass the checks and have no override, ahead of the default
	// endpoints. It returns false for a request it leaves to them.
	Handler func(w http.ResponseWriter, r *http.Request, path string) bool

	mu       sync.Mutex
	requests []string
	// uris are the request targets as the client sent them, with escapes intact.
	uris      []string
	responses map[string]fakeResponse
}

func NewFake(t *testing.T) *FakeGitHub {
	t.Helper()
	return &FakeGitHub{t: t, APIVersion: "2022-11-28", responses: map[string]fakeResponse{}}
}

// Respond overrides what path answers, for every method.
func (f *FakeGitHub) Respond(path string, status int, header map[string]string, body string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.responses[path] = fakeResponse{status: status, header: header, body: body}
}

func (f *FakeGitHub) Restore(path string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.responses, path)
}

func (f *FakeGitHub) Count(path string) int {
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

func (f *FakeGitHub) RequestURIs() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.uris)
}

// Forget drops the requests recorded so far.
func (f *FakeGitHub) Forget() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests, f.uris = nil, nil
}

func (f *FakeGitHub) Total() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.requests)
}

func (f *FakeGitHub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path, ok := strings.CutPrefix(r.URL.Path, f.Prefix)
	if !ok {
		f.t.Errorf("request %q is outside the API Prefix %q", r.URL.Path, f.Prefix)
	}
	if got := r.Header.Get("Accept"); got != "application/vnd.github+json" {
		f.t.Errorf("%s: Accept = %q", path, got)
	}
	if got := r.Header.Get("X-GitHub-Api-Version"); got != f.APIVersion {
		f.t.Errorf("%s: X-GitHub-Api-Version = %q, want %q", path, got, f.APIVersion)
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
	if path == f.Stall {
		<-r.Context().Done()
		return
	}
	if strings.HasSuffix(path, "/installation") || strings.HasSuffix(path, "/access_tokens") {
		f.requireAppJWT(path, r)
	} else if got := r.Header.Get("Authorization"); got != "token "+InstallationToken {
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
	if f.Handler != nil && f.Handler(w, r, path) {
		return
	}
	f.serve(w, r, path, payload)
}

func (f *FakeGitHub) serve(w http.ResponseWriter, r *http.Request, path string, payload []byte) {
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
		corePermissions := map[string]string{"metadata": "read", "contents": "read", "pull_requests": "read", "issues": "read"}
		checkRunsPermissions := map[string]string{"checks": "read"}
		commitStatusesPermissions := map[string]string{"statuses": "read"}
		if !samePermissions(body.Permissions, corePermissions) && !samePermissions(body.Permissions, checkRunsPermissions) &&
			!samePermissions(body.Permissions, commitStatusesPermissions) {
			f.t.Errorf("token permissions = %v, want the core, Checks-only, or Commit-statuses-only read set", body.Permissions)
		}
		reply(http.StatusCreated, fmt.Sprintf(`{"token": %q, "expires_at": %q}`,
			InstallationToken, time.Now().Add(time.Hour).UTC().Format(time.RFC3339)))
	case path == "/repos/owner/repo" || path == "/repositories/100":
		reply(http.StatusOK, `{"id": 100, "full_name": "owner/repo"}`)
	case strings.HasSuffix(path, "/reviews"):
		reply(http.StatusOK, `[]`)
	case path == "/repos/owner/repo/pulls/7":
		reply(http.StatusOK, fmt.Sprintf(`{"number": 7, "state": "open", "merged": false, "base": {"ref": "main", "sha": %q}, "mergeable": true, "merge_commit_sha": %q, "head": {"sha": %q, "repo": {"id": 200, "full_name": "fork/repo"}}}`, BaseSHA, MergeCommitSHA, SHA))
	case path == "/repos/owner/repo/issues/8":
		reply(http.StatusOK, `{"number": 8}`)
	case path == "/repos/owner/repo/git/ref/heads/devel", path == "/repos/owner/repo/git/ref/heads/feature/x":
		reply(http.StatusOK, fmt.Sprintf(`{"object": {"sha": %q}}`, SHA))
	case path == "/repos/owner/repo/git/ref/heads/main":
		reply(http.StatusOK, fmt.Sprintf(`{"object": {"sha": %q}}`, BaseSHA))
	case strings.HasPrefix(path, "/repos/owner/repo/git/ref/heads/"):
		reply(http.StatusNotFound, `{"message": "Not Found"}`)
	case path == "/repos/owner/repo/git/commits/"+MergeCommitSHA:
		reply(http.StatusOK, fmt.Sprintf(`{"sha": %q, "parents": [{"sha": %q}, {"sha": %q}]}`, MergeCommitSHA, BaseSHA, SHA))
	case strings.HasSuffix(path, "/check-runs"):
		reply(http.StatusOK, `{"total_count": 0, "check_runs": []}`)
	case strings.HasSuffix(path, "/status"):
		reply(http.StatusOK, fmt.Sprintf(`{"state": "pending", "sha": %q, "total_count": 0, "statuses": []}`, SHA))
	case strings.HasPrefix(path, "/repos/owner/repo/commits/"):
		reply(http.StatusOK, fmt.Sprintf(`{"sha": %q}`, last))
	default:
		f.t.Errorf("unexpected request %s %s", r.Method, path)
		reply(http.StatusInternalServerError, "")
	}
}

func samePermissions(got, want map[string]string) bool {
	if len(got) != len(want) {
		return false
	}
	for name, level := range want {
		if got[name] != level {
			return false
		}
	}
	return true
}

// requireAppJWT checks the RS256 signature and the claims GitHub requires of an App JWT.
func (f *FakeGitHub) requireAppJWT(path string, r *http.Request) {
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
	if header.Alg != "RS256" || claims.Iss != fmt.Sprint(AppID) || claims.Iat > now || claims.Exp <= now {
		f.t.Errorf("%s: JWT header %+v claims %+v", path, header, claims)
	}
}

func Settings(apiURL string) settings.Settings {
	settings := settings.DefaultSettings()
	settings.APIURL = apiURL
	settings.AppID = AppID
	settings.PrivateKey = KeyPEM()
	settings.WebhookSecret = "test-webhook-secret!"
	return settings
}

func MergeablePullRequestBody(baseRef, baseSHA, headSHA, mergeable, mergeCommitSHA string) string {
	mergeCommit := ""
	if mergeCommitSHA != "" {
		mergeCommit = fmt.Sprintf(`,"merge_commit_sha":%q`, mergeCommitSHA)
	}
	return fmt.Sprintf(`{"number":7,"state":"open","merged":false,"base":{"ref":%q,"sha":%q},"mergeable":%s%s,"head":{"sha":%q,"repo":{"id":200,"full_name":"fork/repo"}}}`,
		baseRef, baseSHA, mergeable, mergeCommit, headSHA)
}

func MergeCommitBody(sha string, parents ...string) string {
	encodedParents := make([]string, len(parents))
	for i, parent := range parents {
		encodedParents[i] = fmt.Sprintf(`{"sha":%q}`, parent)
	}
	return fmt.Sprintf(`{"sha":%q,"parents":[%s]}`, sha, strings.Join(encodedParents, ","))
}

// AsError requires err to be, or wrap, an E, and returns it.
func AsError[E error](t *testing.T, err error) E {
	t.Helper()
	var target E
	if !errors.As(err, &target) {
		t.Fatalf("error = %v (%T), want %T", err, err, target)
	}
	return target
}
