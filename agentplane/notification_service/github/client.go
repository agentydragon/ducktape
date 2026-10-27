// Package github is the Notification service's GitHub App client: App and installation authentication over
// google/go-github and bradleyfalzon/ghinstallation, and the REST reads the service needs, each failing with a
// typed error the worker classifies.
//
// The client holds no webhook, database or rate-limit state. A rate limit goes straight to the caller so the
// service can persist the backoff instead of sleeping here.
package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/bradleyfalzon/ghinstallation/v2"
	"github.com/google/go-github/v88/github"
)

var repositoryName = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)

const maxRepositoryNameLength = 200

// Repository is a repository's stable identity.
type Repository struct {
	ID       int64
	FullName string
}

// Revision is a pull request's head commit and, unless GitHub no longer has the fork, the repository it lives in.
type Revision struct {
	SHA        string
	Repository *Repository
}

// PullRequestObservation is the current state GitHub reports for a pull request. Mergeable is nil while GitHub
// has not computed mergeability yet.
type PullRequestObservation struct {
	Head      Revision
	BaseRef   string
	BaseSHA   string
	Open      bool
	Mergeable *bool
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
	installations map[int64]*installationAuth
}

// installationAuth is an installation's signing transport, which caches and refreshes its token, and the client
// that uses it.
type installationAuth struct {
	transport *ghinstallation.Transport
	client    *github.Client
}

// New validates settings and the App private key. It makes no request.
func New(settings Settings) (*Client, error) {
	if err := settings.Validate(); err != nil {
		return nil, err
	}
	c := &Client{
		timeout:    settings.requestTimeout(),
		baseURL:    strings.TrimRight(settings.APIURL, "/"),
		appID:      settings.AppID,
		privateKey: []byte(settings.PrivateKey),
		transport: observability.HTTPTransport(apiHeaders{
			next:    http.DefaultTransport.(*http.Transport).Clone(),
			version: settings.APIVersion,
		}),
		now:           time.Now,
		installations: map[int64]*installationAuth{},
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
func (c *Client) installationClient(id int64) (*installationAuth, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if auth, ok := c.installations[id]; ok {
		return auth, nil
	}
	transport, err := ghinstallation.New(c.transport, c.appID, id, c.privateKey)
	if err != nil {
		return nil, err
	}
	transport.BaseURL = c.baseURL
	read := github.Ptr("read")
	transport.InstallationTokenOptions = &github.InstallationTokenOptions{
		Permissions: &github.InstallationPermissions{Metadata: read, Contents: read, PullRequests: read, Issues: read},
	}
	client, err := c.newClient(transport)
	if err != nil {
		return nil, err
	}
	auth := &installationAuth{transport: transport, client: client}
	c.installations[id] = auth
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
		return &invalidResponseError{reason: "installation token is empty"}
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
	authorize := func(ctx context.Context) (*github.Client, error) {
		if installationID <= 0 {
			return nil, errors.New("installation ID must be above 0")
		}
		auth, err := c.installationClient(installationID)
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
		return &invalidResponseError{reason: err.Error()}
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
		return &invalidResponseError{reason: err.Error()}
	}
	return err
}

// isDecodeError matches how ghinstallation fails to decode a token response, which it returns unwrapped.
func isDecodeError(err error) bool {
	var syntax *json.SyntaxError
	var typed *json.UnmarshalTypeError
	return errors.As(err, &syntax) || errors.As(err, &typed) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
}

func splitRepository(repository string) (owner, name string, err error) {
	if len(repository) > maxRepositoryNameLength || !repositoryName.MatchString(repository) {
		return "", "", errors.New("repository must be owner/name")
	}
	owner, name, _ = strings.Cut(repository, "/")
	return owner, name, nil
}

func repositoryFrom(repository *github.Repository) (Repository, error) {
	if repository.GetID() <= 0 || len(repository.GetFullName()) > maxRepositoryNameLength ||
		!repositoryName.MatchString(repository.GetFullName()) {
		return Repository{}, &invalidResponseError{reason: "repository identity is missing or malformed"}
	}
	return Repository{ID: repository.GetID(), FullName: repository.GetFullName()}, nil
}

// Installation returns the ID of the App's installation for a repository. It is a NotInstalledError if the App has
// none that can see it, and a SuspendedError if the installation is suspended.
func (c *Client) Installation(ctx context.Context, repository string) (int64, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return 0, err
	}
	installation, err := callAsApp(ctx, c, true, func(ctx context.Context, client *github.Client) (*github.Installation, *github.Response, error) {
		return client.Apps.GetRepositoryInstallation(ctx, owner, name)
	})
	switch {
	case errors.Is(err, errMissing):
		return 0, &NotInstalledError{AccessError: AccessError{StatusCode: http.StatusNotFound}}
	case err != nil:
		return 0, err
	case installation.GetID() <= 0:
		return 0, &invalidResponseError{reason: "installation id is missing"}
	case installation.SuspendedAt != nil:
		return 0, &SuspendedError{AccessError: AccessError{StatusCode: http.StatusOK}}
	}
	return installation.GetID(), nil
}

// Repository reads a repository by name as an installation.
func (c *Client) Repository(ctx context.Context, name string, installationID int64) (Repository, error) {
	owner, repo, err := splitRepository(name)
	if err != nil {
		return Repository{}, err
	}
	repository, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.Repository, *github.Response, error) {
		return client.Repositories.Get(ctx, owner, repo)
	})
	if err != nil {
		return Repository{}, err
	}
	return repositoryFrom(repository)
}

// RepositoryByID reads a repository by its numeric ID as an installation.
func (c *Client) RepositoryByID(ctx context.Context, id, installationID int64) (Repository, error) {
	repository, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.Repository, *github.Response, error) {
		return client.Repositories.GetByID(ctx, id)
	})
	if err != nil {
		return Repository{}, err
	}
	return repositoryFrom(repository)
}

// PullRequest reads a pull request as an installation.
func (c *Client) PullRequest(ctx context.Context, repository string, number int, installationID int64) (PullRequestObservation, error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return PullRequestObservation{}, err
	}
	pullRequest, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.PullRequest, *github.Response, error) {
		return client.PullRequests.Get(ctx, owner, name, number)
	})
	if err != nil {
		return PullRequestObservation{}, err
	}
	if pullRequest == nil || pullRequest.Number == nil {
		return PullRequestObservation{}, &invalidResponseError{reason: "pull request number is missing"}
	}
	if *pullRequest.Number != number {
		return PullRequestObservation{}, &SourceChangedError{Reason: "GitHub pull request identity changed"}
	}
	if pullRequest.State == nil {
		return PullRequestObservation{}, &invalidResponseError{reason: "pull request state is missing"}
	}
	var open bool
	switch *pullRequest.State {
	case "open":
		open = true
	case "closed":
	default:
		return PullRequestObservation{}, &invalidResponseError{reason: "pull request state is invalid"}
	}
	base := pullRequest.GetBase()
	if base.GetRef() == "" {
		return PullRequestObservation{}, &invalidResponseError{reason: "pull request base ref is missing"}
	}
	if base.SHA == nil || *base.SHA == "" {
		return PullRequestObservation{}, &invalidResponseError{reason: "pull request base sha is missing"}
	}
	head := pullRequest.GetHead()
	if head.GetSHA() == "" {
		return PullRequestObservation{}, &invalidResponseError{reason: "pull request head is missing"}
	}
	revision := Revision{SHA: head.GetSHA()}
	if head.Repo != nil {
		fork, err := repositoryFrom(head.Repo)
		if err != nil {
			return PullRequestObservation{}, err
		}
		revision.Repository = &fork
	}
	var mergeable *bool
	if pullRequest.Mergeable != nil {
		value := *pullRequest.Mergeable
		mergeable = &value
	}
	return PullRequestObservation{
		Head: revision, BaseRef: base.GetRef(), BaseSHA: *base.SHA, Open: open, Mergeable: mergeable,
	}, nil
}

// CheckIssue reads an issue as an installation and returns a SourceChangedError unless it is that issue and not a
// pull request.
func (c *Client) CheckIssue(ctx context.Context, repository string, number int, installationID int64) error {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return err
	}
	issue, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.Issue, *github.Response, error) {
		return client.Issues.Get(ctx, owner, name, number)
	})
	switch {
	case err != nil:
		return err
	case issue == nil || issue.Number == nil:
		return &invalidResponseError{reason: "issue number is missing"}
	case *issue.Number != number || issue.PullRequestLinks != nil:
		return &SourceChangedError{Reason: "GitHub issue identity changed or refers to a pull request"}
	}
	return nil
}

// Branch reads a branch's head commit as an installation. found is false if the branch does not exist.
func (c *Client) Branch(ctx context.Context, repository, branch string, installationID int64) (sha string, found bool, err error) {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return "", false, err
	}
	// A segment of only dots is not allowed in a branch name, and the request URL would resolve it onto another
	// endpoint.
	if slices.ContainsFunc(strings.Split(branch, "/"), func(segment string) bool { return segment == "." || segment == ".." }) {
		return "", false, nil
	}
	ref, err := callAsInstallation(ctx, c, installationID, true, func(ctx context.Context, client *github.Client) (*github.Reference, *github.Response, error) {
		return client.Git.GetRef(ctx, owner, name, "heads/"+branch)
	})
	switch {
	case errors.Is(err, errMissing):
		return "", false, nil
	case err != nil:
		return "", false, err
	case ref.GetObject().GetSHA() == "":
		return "", false, &invalidResponseError{reason: "branch commit is missing"}
	}
	return ref.GetObject().GetSHA(), true, nil
}

// CheckCommit reads a commit as an installation and returns a SourceChangedError unless it is that commit.
func (c *Client) CheckCommit(ctx context.Context, repository, sha string, installationID int64) error {
	owner, name, err := splitRepository(repository)
	if err != nil {
		return err
	}
	commit, err := callAsInstallation(ctx, c, installationID, false, func(ctx context.Context, client *github.Client) (*github.RepositoryCommit, *github.Response, error) {
		return client.Repositories.GetCommit(ctx, owner, name, sha, nil)
	})
	switch {
	case err != nil:
		return err
	case commit.GetSHA() == "":
		return &invalidResponseError{reason: "commit sha is missing"}
	case commit.GetSHA() != sha:
		return &SourceChangedError{Reason: "GitHub commit identity changed"}
	}
	return nil
}
