package client

import (
	"context"
	"fmt"
	"net/http"
	"reflect"
	"testing"

	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/testing" // Shared fake API server; avoids colliding with Go testing.
)

func replyPage(w http.ResponseWriter, r *http.Request, path, query string, nextPage int, body string) {
	if nextPage != 0 {
		w.Header().Set("Link", fmt.Sprintf(`<http://%s%s?%s&page=%d>; rel="next"`, r.Host, path, query, nextPage))
	}
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, body)
}

func TestPaginatedReadsReturnTypedFailureFromLaterPage(t *testing.T) {
	for name, endpoint := range map[string]struct {
		path, query, firstPage string
		read                   func(context.Context, *Client) (any, error)
	}{
		"check runs": {
			path:      "/repos/owner/repo/commits/" + fixture.SHA + "/check-runs",
			query:     "filter=latest&per_page=100",
			firstPage: `{"total_count":1,"check_runs":[]}`,
			read: func(ctx context.Context, c *Client) (any, error) {
				return c.CheckRuns(ctx, "owner/repo", fixture.SHA, installationID)
			},
		},
		"commit statuses": {
			path:      "/repos/owner/repo/commits/" + fixture.SHA + "/status",
			query:     "per_page=100",
			firstPage: fmt.Sprintf(`{"state":"pending","sha":%q,"total_count":1,"statuses":[]}`, fixture.SHA),
			read: func(ctx context.Context, c *Client) (any, error) {
				return c.CommitStatuses(ctx, "owner/repo", fixture.SHA, installationID)
			},
		},
		"pull request reviews": {
			path:      "/repos/owner/repo/pulls/7/reviews",
			query:     "per_page=100",
			firstPage: `[]`,
			read: func(ctx context.Context, c *Client) (any, error) {
				return c.PullRequestReviews(ctx, "owner/repo", 7, installationID)
			},
		},
	} {
		t.Run(name, func(t *testing.T) {
			fake := fixture.NewFake(t)
			fake.Handler = func(w http.ResponseWriter, r *http.Request, path string) bool {
				if path != endpoint.path {
					return false
				}
				if r.URL.Query().Get("page") == "1" {
					replyPage(w, r, path, endpoint.query, 2, endpoint.firstPage)
				} else {
					w.WriteHeader(http.StatusServiceUnavailable)
					fmt.Fprint(w, `{"message":"unavailable"}`)
				}
				return true
			}

			result, err := endpoint.read(t.Context(), newTestClient(t, fake))
			if failure := fixture.AsError[*StatusError](t, err); failure.StatusCode != http.StatusServiceUnavailable {
				t.Errorf("failure %+v", failure)
			}
			if !reflect.ValueOf(result).IsNil() {
				t.Errorf("result on error = %#v, want nil", result)
			}
		})
	}
}
