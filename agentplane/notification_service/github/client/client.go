// Package client is the Notification service's GitHub App client: App and installation authentication over
// google/go-github and bradleyfalzon/ghinstallation, and the REST reads the service needs, each failing with a
// typed error the worker classifies.
//
// The client holds no webhook, database or rate-limit state. A rate limit goes straight to the caller so the
// service can persist the backoff instead of sleeping here.
package client

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/bradleyfalzon/ghinstallation/v2"
	"github.com/google/go-github/v88/github"
)

// Revision is a pull request's head commit and, unless GitHub no longer has the fork, the repository it lives in.
type Revision struct {
	SHA        string
	Repository *Repository
}

// PullRequestObservation is the current state GitHub reports for a pull request.
type PullRequestObservation struct {
	Head         Revision
	BaseRef      string
	BaseSHA      string
	State        model.GitHubPullRequestState
	Mergeability model.GitHubMergeableState
	MergeCommit  *model.GitHubMergeCommit
}

// Client reads GitHub as the App, or as one of its installations. It is safe for concurrent use.
type Client struct {
	timeout    time.Duration
	baseURL    string
	appID      int64
	privateKey []byte
	// transport is below every signing layer, so each request carries the same headers, including the
	// installation-token request ghinstallation sends itself.
	transport http.RoundTripper
	app       *github.Client
	now       func() time.Time

	mu sync.Mutex
	// installations are discarded together when any request is rejected as unauthenticated.
	installations map[installationKey]*installationAuth
}

// installationPermissionProfile names the permissions needed by one class of reads. Check runs and commit
// statuses are separate so a missing permission for one cannot prevent reads that need the other.
type installationPermissionProfile uint8

const (
	coreReads installationPermissionProfile = iota
	checkRunsRead
	commitStatusesRead
)

// installationKey keeps each permission profile's token separate. An installation may not have granted Checks
// or Commit statuses, and requesting those permissions must not make its existing subscriptions fail.
type installationKey struct {
	id      int64
	profile installationPermissionProfile
}

// installationAuth is an installation's signing transport, which caches and refreshes its token, and the client
// that uses it.
type installationAuth struct {
	transport *ghinstallation.Transport
	client    *github.Client
}

// New validates settings and the App private key. It makes no request.
func New(settings settings.Settings) (*Client, error) {
	if err := settings.Validate(); err != nil {
		return nil, err
	}
	c := &Client{
		timeout:    settings.RequestTimeout(),
		baseURL:    strings.TrimRight(settings.APIURL, "/"),
		appID:      settings.AppID,
		privateKey: []byte(settings.PrivateKey),
		transport: observability.HTTPTransport(apiHeaders{
			next:    http.DefaultTransport.(*http.Transport).Clone(),
			version: settings.APIVersion,
		}),
		now:           time.Now,
		installations: map[installationKey]*installationAuth{},
	}
	appTransport, err := ghinstallation.NewAppsTransport(c.transport, c.appID, c.privateKey)
	if err != nil {
		return nil, fmt.Errorf("github.private_key: %w", err)
	}
	appTransport.BaseURL = c.baseURL
	if c.app, err = c.newClient(appTransport); err != nil {
		return nil, err
	}
	return c, nil
}

// newClient never follows a redirect: a credential must not leave for another origin, so a redirect surfaces as the
// response it is. It disables go-github's local rate-limit memory, which would answer for GitHub without asking.
func (c *Client) newClient(transport http.RoundTripper) (*github.Client, error) {
	return github.NewClient(
		github.WithHTTPClient(&http.Client{
			Transport:     transport,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}),
		github.WithURLs(&c.baseURL, nil),
		github.WithDisableRateLimitCheck(),
	)
}

// apiHeaders sets the headers GitHub's REST API expects.
type apiHeaders struct {
	next    http.RoundTripper
	version string
}

func (h apiHeaders) RoundTrip(request *http.Request) (*http.Response, error) {
	request = request.Clone(request.Context())
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("X-GitHub-Api-Version", h.version)
	return h.next.RoundTrip(request)
}

// installationClient returns the auth cached for an installation; nothing is minted until its first request. Each
// installation signs through its own AppsTransport, because ghinstallation writes to it while refreshing a token.
func (c *Client) installationClient(id int64, profile installationPermissionProfile) (*installationAuth, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	key := installationKey{id: id, profile: profile}
	if auth, ok := c.installations[key]; ok {
		return auth, nil
	}
	transport, err := ghinstallation.New(c.transport, c.appID, id, c.privateKey)
	if err != nil {
		return nil, err
	}
	transport.BaseURL = c.baseURL
	read := github.Ptr("read")
	var permissions *github.InstallationPermissions
	switch profile {
	case coreReads:
		permissions = &github.InstallationPermissions{Metadata: read, Contents: read, PullRequests: read, Issues: read}
	case checkRunsRead:
		permissions = &github.InstallationPermissions{Checks: read}
	case commitStatusesRead:
		permissions = &github.InstallationPermissions{Statuses: read}
	default:
		return nil, errors.New("unknown installation permission profile")
	}
	transport.InstallationTokenOptions = &github.InstallationTokenOptions{
		Permissions: permissions,
	}
	client, err := c.newClient(transport)
	if err != nil {
		return nil, err
	}
	auth := &installationAuth{transport: transport, client: client}
	c.installations[key] = auth
	return auth, nil
}

func (c *Client) forgetInstallations() {
	c.mu.Lock()
	defer c.mu.Unlock()
	clear(c.installations)
}

// mint makes the installation's token exist before the request needs it, so that its failures arrive as
// themselves, and rejects a token response without a token.
func (a *installationAuth) mint(ctx context.Context) error {
	token, err := a.transport.Token(ctx)
	if err == nil && token == "" {
		return &InvalidResponseError{Reason: "installation token is empty"}
	}
	return err
}

type request[T any] func(context.Context, *github.Client) (T, *github.Response, error)

// callAsApp runs req as the App itself, signed with a JWT.
func callAsApp[T any](ctx context.Context, c *Client, missingOK bool, req request[T]) (T, error) {
	return call(ctx, c, missingOK, func(context.Context) (*github.Client, error) { return c.app, nil }, req)
}

// callAsInstallation runs req as an installation, signed with the installation token it first mints or reuses.
func callAsInstallation[T any](ctx context.Context, c *Client, installationID int64, missingOK bool, req request[T]) (T, error) {
	return callAsInstallationWithProfile(ctx, c, installationID, coreReads, missingOK, req)
}

// callAsCheckRunsInstallation uses the token profile scoped to the Checks repository permission.
func callAsCheckRunsInstallation[T any](ctx context.Context, c *Client, installationID int64, req request[T]) (T, error) {
	return callAsInstallationWithProfile(ctx, c, installationID, checkRunsRead, false, req)
}

// callAsCommitStatusesInstallation uses the token profile scoped to the Commit statuses repository permission.
func callAsCommitStatusesInstallation[T any](ctx context.Context, c *Client, installationID int64, req request[T]) (T, error) {
	return callAsInstallationWithProfile(ctx, c, installationID, commitStatusesRead, false, req)
}

func callAsInstallationWithProfile[T any](ctx context.Context, c *Client, installationID int64, profile installationPermissionProfile, missingOK bool, req request[T]) (T, error) {
	authorize := func(ctx context.Context) (*github.Client, error) {
		if installationID <= 0 {
			return nil, errors.New("installation ID must be above 0")
		}
		auth, err := c.installationClient(installationID, profile)
		if err != nil {
			return nil, err
		}
		return auth.client, auth.mint(ctx)
	}
	return call(ctx, c, missingOK, authorize, req)
}

// call runs req under the request timeout, which also covers the installation-token request. missingOK turns a 404
// from req itself, not from authorize, into errMissing.
func call[T any](ctx context.Context, c *Client, missingOK bool, authorize func(context.Context) (*github.Client, error), req request[T]) (T, error) {
	var zero T
	callCtx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	client, err := authorize(callCtx)
	if err != nil {
		return zero, c.failure(ctx, err, nil, false)
	}
	value, response, err := req(callCtx, client)
	if err != nil {
		return zero, c.failure(ctx, err, response, missingOK)
	}
	return value, nil
}

// failure turns a failed GitHub call into the typed error callers classify. ctx is the caller's, so a caller that
// went away gets its own error back, not a timeout it did not ask for.
func (c *Client) failure(ctx context.Context, err error, response *github.Response, missingOK bool) error {
	var raw *http.Response
	var minted *ghinstallation.HTTPError
	switch {
	case response != nil:
		raw = response.Response
	case errors.As(err, &minted):
		raw = minted.Response
	}
	if raw != nil {
		// ghinstallation leaves the body of a rejected token request for its caller to close.
		defer raw.Body.Close()
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if raw == nil {
		return failureWithoutResponse(err)
	}
	status := raw.StatusCode
	switch {
	case rateLimited(raw):
		return &RateLimitedError{StatusCode: status, RetrySeconds: retrySeconds(raw.Header, c.now())}
	case status == http.StatusNotFound && missingOK:
		return errMissing
	case status == http.StatusUnauthorized:
		c.forgetInstallations()
		return &AccessError{StatusCode: status}
	case status == http.StatusForbidden || status == http.StatusNotFound:
		return &AccessError{StatusCode: status}
	case status >= 200 && status < 300:
		// The request succeeded and its body did not decode.
		return &InvalidResponseError{Reason: err.Error()}
	}
	return &StatusError{StatusCode: status}
}

func failureWithoutResponse(err error) error {
	var urlError *url.Error
	var minted *ghinstallation.HTTPError
	switch {
	case errors.As(err, &urlError), errors.As(err, &minted), errors.Is(err, context.DeadlineExceeded):
		return &TransportError{Err: err}
	case isDecodeError(err):
		return &InvalidResponseError{Reason: err.Error()}
	}
	return err
}

// isDecodeError matches how ghinstallation fails to decode a token response, which it returns unwrapped.
func isDecodeError(err error) bool {
	var syntax *json.SyntaxError
	var typed *json.UnmarshalTypeError
	return errors.As(err, &syntax) || errors.As(err, &typed) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
}
