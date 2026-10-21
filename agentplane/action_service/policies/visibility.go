package policies

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"golang.org/x/sync/singleflight"
	"k8s.io/apimachinery/pkg/util/cache"
)

const (
	// GitHubAPIBaseURL is the GitHub REST API's public base URL.
	GitHubAPIBaseURL = "https://api.github.com"
	// VisibilityTTL is how long a confirmed visibility is reused. The unauthenticated GitHub REST API allows 60
	// requests an hour in all; a day bounds how long a repository turning private keeps reading as public.
	VisibilityTTL            = 24 * time.Hour
	visibilityRequestTimeout = 10 * time.Second
)

// ErrVisibilityUnavailable is a lookup that neither confirmed nor refuted a repository's visibility; its callers fail
// closed.
var ErrVisibilityUnavailable = errors.New("GitHub repository visibility unavailable")

// Visibility confirms a GitHub repository public by an unauthenticated `GET /repos/{owner}/{repo}`.
//
// A policy admitting any public repository cannot infer public from the absence of a restriction: the GitHub MCP
// connections behind it authenticate as the operator, who sees private repositories. An unauthenticated lookup 404s
// on every repository the anonymous caller cannot see, private or nonexistent alike, so it has no credential that could
// observe a private one.
//
// A replica reuses each answer GitHub gave for the TTL and shares one lookup between concurrent callers. A lookup that
// fails is not kept, so an outage does not wedge a repository as unknown for the TTL.
type Visibility struct {
	client  *http.Client
	baseURL string
	ttl     time.Duration
	answers *cache.Expiring
	lookups singleflight.Group
}

// NewVisibility looks repositories up at apiBaseURL and reuses each answer for ttl.
func NewVisibility(apiBaseURL string, ttl time.Duration) (*Visibility, error) {
	if _, err := url.ParseRequestURI(apiBaseURL); err != nil {
		return nil, fmt.Errorf("GitHub API base URL: %w", err)
	}
	return &Visibility{
		client: &http.Client{
			Timeout: visibilityRequestTimeout,
			// A redirect is neither of the two answers that confirm anything.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		baseURL: strings.TrimSuffix(apiBaseURL, "/"),
		ttl:     ttl,
		answers: cache.NewExpiring(),
	}, nil
}

// IsPublic reports whether GitHub confirms owner/repository public: true for a 200 whose `private` is false, false
// for a 404, and ErrVisibilityUnavailable for anything else. Names are compared case-insensitively. A caller that gives
// up leaves the lookup running for the others.
func (v *Visibility) IsPublic(ctx context.Context, owner, repository string) (bool, error) {
	// Each name is one path segment, so a `/` or `..` in it cannot reach another repository's path.
	path := "/repos/" + url.PathEscape(fold(owner)) + "/" + url.PathEscape(fold(repository))
	if public, ok := v.answers.Get(path); ok {
		return public.(bool), nil
	}
	lookup := v.lookups.DoChan(path, func() (any, error) {
		public, err := v.fetch(context.WithoutCancel(ctx), path)
		if err == nil {
			v.answers.Set(path, public, v.ttl)
		}
		return public, err
	})
	select {
	case <-ctx.Done():
		return false, fmt.Errorf("%w: %w", ErrVisibilityUnavailable, ctx.Err())
	case result := <-lookup:
		if result.Err != nil {
			return false, result.Err
		}
		return result.Val.(bool), nil
	}
}

func (v *Visibility) fetch(ctx context.Context, path string) (bool, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, v.baseURL+path, nil)
	if err != nil {
		return false, fmt.Errorf("%w: %w", ErrVisibilityUnavailable, err)
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	response, err := v.client.Do(request)
	if err != nil {
		return false, fmt.Errorf("%w: %w", ErrVisibilityUnavailable, err)
	}
	defer response.Body.Close()
	switch response.StatusCode {
	case http.StatusNotFound:
		// Unauthenticated, GitHub 404s a private repository exactly as a nonexistent one: neither is public.
		return false, nil
	case http.StatusOK:
	default:
		return false, fmt.Errorf("%w: GitHub answered %s for %s", ErrVisibilityUnavailable, response.Status, path)
	}
	// An unauthenticated 200 should never be private, but this check exists not to trust inference.
	var body struct {
		Private *bool `json:"private"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		return false, fmt.Errorf("%w: GitHub's answer for %s: %w", ErrVisibilityUnavailable, path, err)
	}
	if body.Private == nil || *body.Private {
		return false, fmt.Errorf("%w: GitHub's answer for %s does not say private=false", ErrVisibilityUnavailable, path)
	}
	return true, nil
}
