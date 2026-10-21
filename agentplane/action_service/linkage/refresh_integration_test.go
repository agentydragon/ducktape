package linkage_test

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/testing/oauthprovider"
	"k8s.io/apimachinery/pkg/util/wait"
)

// due grants tokens that are due for refresh as soon as they are granted: they expire within a minute.
var due = oauthprovider.Options{ExpiresIn: 30, RefreshTokens: true}

// eventually waits for the condition, and fails the test once awaitBound passes without it.
func eventually(t *testing.T, what string, condition func() bool) {
	t.Helper()
	err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, awaitBound, true,
		func(context.Context) (bool, error) { return condition(), nil })
	if err != nil {
		t.Fatalf("gave up waiting for %s: %v", what, err)
	}
}

func TestATokenDueForRefreshIsRefreshedWhenRead(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, due)
	authority := f.authority(t, provider.Server(testServer))
	link(t, authority, testServer)
	f.changed(t, "the link")
	token, err := authority.Token(t.Context(), testServer)
	if err != nil {
		t.Fatal(err)
	}
	if token.AccessToken != "test-refreshed-token-2" || token.RefreshFailure != nil {
		t.Errorf("the token is %+v", token)
	}
	f.changed(t, "the refresh")
	refreshes := provider.Requests("refresh_token")
	if len(refreshes) != 1 || refreshes[0].Get("refresh_token") != "test-refresh-token-1" ||
		refreshes[0].Get("client_id") != oauthprovider.ClientID {
		t.Errorf("the refreshes sent %v", refreshes)
	}
	if revision := f.tokenRevision(t, testServer); revision != 2 {
		t.Errorf("the refreshed token's revision is %d", revision)
	}
	// The refreshed token is due again, and refreshed again from the refresh token the provider rotated it to.
	if _, err := authority.Token(t.Context(), testServer); err != nil {
		t.Fatal(err)
	}
	if refreshes := provider.Requests("refresh_token"); len(refreshes) != 2 || refreshes[1].Get("refresh_token") != "test-refresh-token-2" {
		t.Errorf("the refreshes sent %v", refreshes)
	}
}

func TestATokenIsRefreshedOnlyWhenDue(t *testing.T) {
	for name, options := range map[string]oauthprovider.Options{
		"an hour from its expiry": {ExpiresIn: 3600, RefreshTokens: true},
		"without an expiry":       {RefreshTokens: true},
		"without a refresh token": {ExpiresIn: 30},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			provider := oauthprovider.New(t, options)
			authority := f.authority(t, provider.Server(testServer))
			link(t, authority, testServer)
			if token, err := authority.Token(t.Context(), testServer); err != nil || token.AccessToken != "test-access-token-1" {
				t.Errorf("the token is %+v, %v", token, err)
			}
			if refreshes := provider.Requests("refresh_token"); len(refreshes) != 0 {
				t.Errorf("the token was refreshed: %v", refreshes)
			}
		})
	}
}

func TestAFailedRefreshIsRecordedAsWhatTheProviderAnswered(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   any
		want   operator_action_servicepb.McpLinkageStatus
		action operator_action_servicepb.McpRefreshAction
		error  string
	}{{
		name: "refused", status: http.StatusBadRequest,
		body: map[string]string{"error": "invalid_grant", "error_description": "Token is not active"},
		want: operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_DEGRADED, action: operator_action_servicepb.McpRefreshAction_MCP_REFRESH_ACTION_RECONNECT,
		error: "the OAuth provider refused the token request: invalid_grant: Token is not active",
	}, {
		name: "unavailable", status: http.StatusServiceUnavailable,
		// The token holds until its expiry, so the linkage stays linked while the refresh is retried.
		want: operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED, action: operator_action_servicepb.McpRefreshAction_MCP_REFRESH_ACTION_RETRYING,
		error: "the token endpoint answered HTTP 503 Service Unavailable",
	}} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			provider := oauthprovider.New(t, due)
			authority := f.authority(t, provider.Server(testServer))
			link(t, authority, testServer)
			f.changed(t, "the link")
			provider.Refuse(tc.status, tc.body)
			failedAt := time.Now()
			token, err := authority.Token(t.Context(), testServer)
			if err != nil {
				t.Fatal(err)
			}
			f.changed(t, "the failed refresh")
			if token.AccessToken != "test-access-token-1" || token.RefreshFailure == nil {
				t.Errorf("the token is %+v", token)
			}
			linked := linkageOf(t, authority, testServer)
			failure := linked.GetRefreshFailure()
			if linked.GetStatus() != tc.want || failure.GetAction() != tc.action || failure.GetError() != tc.error ||
				failure.GetAttempts() != 1 {
				t.Errorf("the linkage is %v", linked)
			}
			// A refusal waits for the operator; anything else is tried again 30 seconds on.
			switch retryAt := failure.GetRetryAt(); tc.action {
			case operator_action_servicepb.McpRefreshAction_MCP_REFRESH_ACTION_RECONNECT:
				if retryAt != nil {
					t.Errorf("a refused refresh is retried at %v", retryAt.AsTime())
				}
			default:
				if delay := retryAt.AsTime().Sub(failedAt); delay < 29*time.Second || delay > 31*time.Second {
					t.Errorf("the refresh is retried %v after it failed", delay)
				}
			}
			if _, err := authority.Token(t.Context(), testServer); err != nil {
				t.Fatal(err)
			}
			if refreshes := provider.Requests("refresh_token"); len(refreshes) != 1 {
				t.Errorf("%d refreshes before the next was due", len(refreshes))
			}
		})
	}
}

func TestOneReplicaAtATimeRefreshesAToken(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, due)
	server := provider.Server(testServer)
	first, second := f.authority(t, server), f.authority(t, server)
	link(t, first, testServer)
	release := provider.Hold()
	defer release()
	refreshed := make(chan *linkage.Token, 1)
	go func() {
		token, err := first.Token(context.Background(), testServer)
		if err != nil {
			t.Error(err)
		}
		refreshed <- token
	}()
	eventually(t, "the first replica's refresh", func() bool { return len(provider.Requests("refresh_token")) == 1 })
	// The first replica's claim holds the token: the second serves it as it stands.
	token, err := second.Token(t.Context(), testServer)
	if err != nil || token.AccessToken != "test-access-token-1" {
		t.Errorf("the second replica's token is %+v, %v", token, err)
	}
	release()
	if token := <-refreshed; token.AccessToken != "test-refreshed-token-2" {
		t.Errorf("the first replica's token is %+v", token)
	}
	if refreshes := provider.Requests("refresh_token"); len(refreshes) != 1 {
		t.Errorf("%d refreshes", len(refreshes))
	}
}

func TestRunRefreshesTheTokensDueUnasked(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{ExpiresIn: 3600, RefreshTokens: true})
	authority := f.authority(t, provider.Server(testServer))
	link(t, authority, testServer)
	f.changed(t, "the link")
	if _, err := f.db.ExecContext(t.Context(), "UPDATE mcp_oauth_token_state SET expires_at = now() + interval '30 seconds'"); err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(t.Context())
	ran := make(chan struct{})
	go func() {
		defer close(ran)
		authority.Run(ctx)
	}()
	f.changed(t, "the sweep's refresh")
	stop()
	<-ran
	if refreshes := provider.Requests("refresh_token"); len(refreshes) != 1 {
		t.Errorf("%d refreshes", len(refreshes))
	}
	if token, err := authority.Token(t.Context(), testServer); err != nil || token.AccessToken != "test-refreshed-token-2" {
		t.Errorf("the token is %+v, %v", token, err)
	}
}
