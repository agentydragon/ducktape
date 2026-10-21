package authserver

import (
	"errors"
	"net/http"
	"net/url"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"golang.org/x/oauth2"

	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/mcpserver"
)

func TestACodeExchangeActivatesTheGrantItIssuesTokensFor(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	// A client may leave out resource.
	tokens := issued(t, "the exchange", h.exchange(t, r, h.code(t, r), func(f url.Values) { f.Del("resource") }))
	if tokens.RefreshToken == "" || tokens.ExpiresIn > int64(accessTokenLifetime/time.Second) ||
		tokens.ExpiresIn < int64((accessTokenLifetime-time.Minute)/time.Second) {
		t.Errorf("issued %+v", tokens)
	}
	h.wantAdmitted(t, "the access token", tokens.AccessToken, callerAccount)
	grants := h.only(t).GetGrants()
	if len(grants) != 1 || grants[0].GetClientId() != r.clientID || grants[0].GetIssuer() != h.url ||
		grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_ACTIVE {
		t.Errorf("grants %v", grants)
	}
}

func TestAnAccessTokenLapsesWithTheOperatorsUpstreamSession(t *testing.T) {
	h := start(t)
	h.upstream.change(func(u *fakeUpstream) { u.lifetime = 10 * time.Minute })
	clientID := h.client(t)
	tokens := h.tokens(t, clientID)
	if tokens.ExpiresIn > 600 {
		t.Errorf("an access token for %d seconds of a 600-second upstream session", tokens.ExpiresIn)
	}
	h.exec(t, `UPDATE oauth_token SET expires_at = now() - interval '1 second' WHERE token_hash = $1`,
		digest(tokens.AccessToken))
	h.wantInvalid(t, "an expired access token", tokens.AccessToken)

	r := newRequest(clientID)
	code := h.code(t, r)
	h.exec(t, `UPDATE oauth_authorization SET upstream_expires_at = now() - interval '1 second' WHERE exchanged_at IS NULL`)
	wantRefused(t, "an exchange after the upstream session lapsed", h.exchange(t, r, code, nil), http.StatusBadRequest,
		"invalid_grant", upstreamLapsed)
}

func TestACodeIsRedeemedOnlyByItsClientAtItsRedirectURIWithItsVerifier(t *testing.T) {
	h := start(t)
	clientID, other := h.client(t), h.client(t)
	refusals := []struct {
		what              string
		change            func(url.Values)
		code, description string
	}{
		{"another client", func(f url.Values) { f.Set("client_id", other) }, "invalid_grant", codeInvalid},
		{"another redirect URI", func(f url.Values) { f.Set("redirect_uri", "http://127.0.0.1:49152/test-callback") }, "invalid_grant", redirectDiffer},
		{"another verifier", func(f url.Values) { f.Set("code_verifier", oauth2.GenerateVerifier()) }, "invalid_grant", ""},
		{"another resource", func(f url.Values) { f.Set("resource", "https://test-other-resource.example/mcp") }, "invalid_target", ""},
		{"an unknown code", func(f url.Values) { f.Set("code", "test-unknown-code") }, "invalid_grant", codeInvalid},
		{"a client secret", func(f url.Values) { f.Set("client_secret", "test-client-secret") }, "invalid_client", ""},
	}
	for _, refused := range refusals {
		r := newRequest(clientID)
		code := h.code(t, r)
		wantRefused(t, refused.what, h.exchange(t, r, code, refused.change), http.StatusBadRequest, refused.code,
			refused.description)
		// The refusal left the code to its client.
		if answer := h.exchange(t, r, code, nil); answer.status != http.StatusOK {
			t.Errorf("%s, then the exchange: %+v", refused.what, answer)
		}
	}
	if listed := h.listConnections(t); len(listed) != len(refusals) {
		t.Errorf("the refused exchanges created Connections: %v", listed)
	}
}

func TestAnExpiredCodeIsRefused(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	code := h.code(t, r)
	h.exec(t, `UPDATE oauth_authorization SET signed_in_at = $1`, time.Now().Add(-codeLifetime-time.Second))
	wantRefused(t, "an expired code", h.exchange(t, r, code, nil), http.StatusBadRequest, "invalid_grant", codeInvalid)
}

func TestAReplayedCodeRevokesTheGrantItsFirstExchangeActivated(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	code := h.code(t, r)
	first := issued(t, "the first exchange", h.exchange(t, r, code, nil))
	wantRefused(t, "the replay", h.exchange(t, r, code, nil), http.StatusBadRequest, "invalid_grant", codeReplayed)
	h.wantInvalid(t, "the first exchange's access token", first.AccessToken)
	wantRefused(t, "a refresh of the first exchange's refresh token", h.refresh(t, r.clientID, first.RefreshToken),
		http.StatusBadRequest, "invalid_grant", "")
	if c := h.only(t); c.GetGrants()[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED || c.GetBoundCaller() == nil {
		t.Errorf("after the replay %v", c)
	}
}

func TestConcurrentExchangesOfACodeActivateOneGrant(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	form := h.exchangeForm(r, h.code(t, r))
	answers := make([]tokenAnswer, 2)
	failures := make([]error, len(answers))
	var wg sync.WaitGroup
	for i := range answers {
		wg.Go(func() { answers[i], failures[i] = h.post(tokenPath, form) })
	}
	wg.Wait()
	slices.SortFunc(answers, func(a, b tokenAnswer) int { return a.status - b.status })
	if errors.Join(failures...) != nil || answers[0].status != http.StatusOK || answers[1].status != http.StatusBadRequest {
		t.Fatalf("exchanges answered %+v, %v", answers, failures)
	}
	// The later exchange was a replay, which revoked the grant of the earlier one.
	h.wantInvalid(t, "the access token of the exchange answered", answers[0].AccessToken)
	if c := h.only(t); c.GetGrants()[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED {
		t.Errorf("after the replay %v", c)
	}
}

// upstreamURLOf is the upstream sign-in the request's enrollment would send the browser to once allowed.
func (h *harness) upstreamURLOf(t *testing.T, r request) string {
	t.Helper()
	var upstream string
	if err := h.db.QueryRowContext(t.Context(), `SELECT upstream_url FROM connection_enrollment WHERE code_challenge = $1`,
		oauth2.S256ChallengeFromVerifier(r.verifier)).Scan(&upstream); err != nil {
		t.Fatal(err)
	}
	return upstream
}

func TestTheExchangeRefusesAConsentThatAllowsNoGrant(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	for _, refused := range []struct {
		what string
		// authorize runs the browser through consent and the upstream sign-in, and returns the code.
		authorize func(r request) string
	}{
		// Knowing the upstream URL, or signing in upstream, is no consent.
		{"an undecided consent", func(r request) string {
			must(h.follow(h.authorizeURL(r, nil)))(t)
			return h.answer(t, r, must(h.signIn(h.upstreamURLOf(t, r)))(t)).Get("code")
		}},
		{"a denied consent", func(r request) string {
			if denied := must(h.consent(t.Context(), h.authorizeURL(r, nil), testOperator, nil))(t); denied.RedirectUrl != nil {
				t.Errorf("a deny sent the browser to %s", denied.GetRedirectUrl())
			}
			return h.answer(t, r, must(h.signIn(h.upstreamURLOf(t, r)))(t)).Get("code")
		}},
		{"a consent of an operator other than the approver", func(r request) string {
			allowed := must(h.consent(t.Context(), h.authorizeURL(r, nil), testOtherOperator, newConnection(callerAccount)))(t)
			return h.answer(t, r, must(h.signIn(allowed.GetRedirectUrl()))(t)).Get("code")
		}},
		{"a consent for an account no longer labeled", func(r request) string {
			code := h.code(t, r)
			h.callers.label(otherAccount)
			return code
		}},
	} {
		r := newRequest(clientID)
		wantRefused(t, refused.what, h.exchange(t, r, refused.authorize(r), nil), http.StatusBadRequest, "invalid_grant", "")
		h.callers.label(callerAccount, otherAccount)
	}
	if listed := h.listConnections(t); len(listed) != 0 {
		t.Errorf("the refused exchanges created %v", listed)
	}
}

func TestAReconnectRevokesTheOldClientsTokensAndKeepsTheConnection(t *testing.T) {
	h := start(t)
	oldClient := h.client(t)
	old := h.tokens(t, oldClient)
	c := h.only(t)
	r := newRequest(h.client(t))
	answer := h.answer(t, r, must(h.authorize(t.Context(), h.authorizeURL(r, nil), reconnection(otherAccount, c)))(t))
	h.wantAdmitted(t, "the old access token before the exchange", old.AccessToken, callerAccount)
	fresh := issued(t, "the reconnect", h.exchange(t, r, answer.Get("code"), nil))
	h.wantAdmitted(t, "the new access token", fresh.AccessToken, otherAccount)
	h.wantInvalid(t, "the old access token", old.AccessToken)
	wantRefused(t, "a refresh of the old refresh token", h.refresh(t, oldClient, old.RefreshToken),
		http.StatusBadRequest, "invalid_grant", "")
	got := h.only(t)
	grants := got.GetGrants()
	if got.GetId() != c.GetId() || len(grants) != 2 || grants[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED ||
		grants[1].GetStatus() != pb.GrantStatus_GRANT_STATUS_ACTIVE || grants[1].GetRevision() != 2 ||
		grants[1].GetClientId() != r.clientID {
		t.Errorf("reconnected %v", got)
	}
}

func TestAReconnectOfAConnectionChangedSinceConsentIsRefusedAndRevokesNothing(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	current := h.tokens(t, clientID)
	c := h.only(t)
	r := newRequest(clientID)
	answer := h.answer(t, r, must(h.authorize(t.Context(), h.authorizeURL(r, nil), reconnection(callerAccount, c)))(t))
	if _, err := h.connections.UpdateConnection(as(t, testOperator), &pb.UpdateConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: c.GetVersion(), DisplayName: "test-renamed",
	}); err != nil {
		t.Fatal(err)
	}
	wantRefused(t, "the stale reconnect", h.exchange(t, r, answer.Get("code"), nil), http.StatusBadRequest, "invalid_grant", "")
	h.wantAdmitted(t, "the current access token", current.AccessToken, callerAccount)
}

func TestARefreshRotatesTheRefreshTokenAndASpentOneRevokesTheGrant(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	first := h.tokens(t, clientID)
	second := issued(t, "the refresh", h.refresh(t, clientID, first.RefreshToken))
	if second.RefreshToken == "" || second.RefreshToken == first.RefreshToken {
		t.Errorf("refreshed %+v", second)
	}
	h.wantAdmitted(t, "the refreshed access token", second.AccessToken, callerAccount)
	wantRefused(t, "the spent refresh token", h.refresh(t, clientID, first.RefreshToken), http.StatusBadRequest,
		"invalid_grant", refreshReused)
	h.wantInvalid(t, "the refreshed access token after the reuse", second.AccessToken)
	wantRefused(t, "the refreshed refresh token after the reuse", h.refresh(t, clientID, second.RefreshToken),
		http.StatusBadRequest, "invalid_grant", "")
}

func TestARefreshRenewsTheOperatorsUpstreamSessionOrIsRefused(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	tokens := h.tokens(t, clientID)
	refuse := func(what, refreshToken string, change func(*fakeUpstream), status int, code, description string) {
		t.Helper()
		h.upstream.change(change)
		answer := h.refresh(t, clientID, refreshToken)
		wantRefused(t, what, answer, status, code, description)
		if status == http.StatusServiceUnavailable && answer.header.Get("Retry-After") == "" {
			t.Errorf("%s: no Retry-After", what)
		}
		h.upstream.change(func(u *fakeUpstream) { u.refusing, u.failing, u.subject = false, "", testUpstreamSubject })
	}
	refuse("the provider refusing", tokens.RefreshToken, func(u *fakeUpstream) { u.refusing = true },
		http.StatusBadRequest, "invalid_grant", upstreamUnverified)
	refuse("an outage of the provider", tokens.RefreshToken, func(u *fakeUpstream) { u.failing = "/token" },
		http.StatusServiceUnavailable, "server_error", unavailableDescription)
	// Neither spent the refresh token.
	renewed := issued(t, "the refresh after both", h.refresh(t, clientID, tokens.RefreshToken))
	refuse("a renewal as another account", renewed.RefreshToken, func(u *fakeUpstream) { u.subject = testUpstreamStranger },
		http.StatusBadRequest, "invalid_grant", notOperator)
}

func TestARefreshTokenIsRedeemedOnlyByItsClientWhileItLasts(t *testing.T) {
	h := start(t)
	clientID, other := h.client(t), h.client(t)
	for _, refused := range []struct {
		what              string
		refresh           func(tokens tokenAnswer) tokenAnswer
		code, description string
	}{
		{"another client's refresh", func(tokens tokenAnswer) tokenAnswer {
			return h.refresh(t, other, tokens.RefreshToken)
		}, "invalid_grant", grantInvalid},
		{"an access token as one", func(tokens tokenAnswer) tokenAnswer {
			return h.refresh(t, clientID, tokens.AccessToken)
		}, "invalid_grant", grantInvalid},
		{"an unknown refresh token", func(tokenAnswer) tokenAnswer {
			return h.refresh(t, clientID, "test-unknown-token")
		}, "invalid_grant", grantInvalid},
		{"an expired refresh token", func(tokens tokenAnswer) tokenAnswer {
			h.exec(t, `UPDATE oauth_token SET expires_at = now() - interval '1 second' WHERE token_hash = $1`,
				digest(tokens.RefreshToken))
			return h.refresh(t, clientID, tokens.RefreshToken)
		}, "invalid_grant", grantInvalid},
		{"a refresh token of a sign-in nothing renews", func(tokens tokenAnswer) tokenAnswer {
			h.exec(t, `
				UPDATE oauth_authorization SET upstream_refresh_token = NULL
				WHERE id = (SELECT authorization_id FROM oauth_token WHERE token_hash = $1)`, digest(tokens.RefreshToken))
			return h.refresh(t, clientID, tokens.RefreshToken)
		}, "invalid_grant", grantInvalid},
		{"another resource", func(tokens tokenAnswer) tokenAnswer {
			return must(h.post(tokenPath, url.Values{
				"grant_type": {"refresh_token"}, "client_id": {clientID}, "refresh_token": {tokens.RefreshToken},
				"resource": {"https://test-other-resource.example/mcp"},
			}))(t)
		}, "invalid_target", ""},
	} {
		tokens := h.tokens(t, clientID)
		wantRefused(t, refused.what, refused.refresh(tokens), http.StatusBadRequest, refused.code, refused.description)
		h.wantAdmitted(t, refused.what+", then the access token", tokens.AccessToken, callerAccount)
	}
}

func TestARefreshTokenIsIssuedOnlyWhenTheClientAndTheProviderCanRefresh(t *testing.T) {
	h := start(t)
	if tokens := h.tokens(t, h.registered(t, "authorization_code")); tokens.RefreshToken != "" {
		t.Errorf("a client that may not refresh got a refresh token")
	}
	h.upstream.change(func(u *fakeUpstream) { u.withholding = true })
	if tokens := h.tokens(t, h.client(t)); tokens.RefreshToken != "" {
		t.Errorf("a refresh token of a sign-in the provider gave none for")
	}
}

func TestARevocationEndsTheGrantOfATokenOnlyItsClientHolds(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	other := h.client(t)
	revoked := func(what string, answer tokenAnswer) {
		t.Helper()
		if answer.status != http.StatusOK || answer.Error != "" {
			t.Errorf("%s: %+v", what, answer)
		}
	}
	for _, kind := range []string{"access", "refresh"} {
		tokens := h.tokens(t, clientID)
		token := map[string]string{"access": tokens.AccessToken, "refresh": tokens.RefreshToken}[kind]
		revoked("another client revoking the "+kind+" token", h.revoke(t, other, token))
		h.wantAdmitted(t, "after another client revoked the "+kind+" token", tokens.AccessToken, callerAccount)
		revoked("revoking the "+kind+" token", h.revoke(t, clientID, token))
		h.wantInvalid(t, "after revoking the "+kind+" token", tokens.AccessToken)
		wantRefused(t, "a refresh after revoking the "+kind+" token", h.refresh(t, clientID, tokens.RefreshToken),
			http.StatusBadRequest, "invalid_grant", "")
		revoked("revoking the "+kind+" token again", h.revoke(t, clientID, token))
	}
	revoked("revoking an unknown token", h.revoke(t, clientID, "test-unknown-token"))
	for _, c := range h.listConnections(t) {
		if c.GetGrants()[0].GetStatus() != pb.GrantStatus_GRANT_STATUS_REVOKED || c.GetBoundCaller() == nil {
			t.Errorf("after the revocations %v", c)
		}
	}
}

func TestABearerIsAdmittedAsTheAccountItsLiveGrantIsBoundTo(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	tokens := h.tokens(t, clientID)
	h.wantAdmitted(t, "the access token", tokens.AccessToken, callerAccount)
	h.wantInvalid(t, "an unknown token", "test-unknown-token")
	h.wantInvalid(t, "the refresh token", tokens.RefreshToken)
	h.callers.label(otherAccount)
	h.wantInvalid(t, "the access token of an account no longer labeled", tokens.AccessToken)
	h.callers.label(callerAccount, otherAccount)
	c := h.only(t)
	if _, err := h.connections.UnbindConnection(as(t, testOperator), &pb.UnbindConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: c.GetVersion(),
	}); err != nil {
		t.Fatal(err)
	}
	h.wantInvalid(t, "the access token of an unbound Connection", tokens.AccessToken)

	live := h.tokens(t, clientID)
	h.db.Close()
	if _, err := h.server.VerifyBearer(t.Context(), live.AccessToken, nil); !errors.Is(err, mcpserver.ErrAuthorityUnavailable) ||
		errors.Is(err, auth.ErrInvalidToken) {
		t.Errorf("verifying a bearer without the database: %v", err)
	}
}

func TestAnOutageAtTheTokenEndpointsIsAnsweredAsTemporary(t *testing.T) {
	h := start(t)
	// A client with a metadata document is known without the database.
	r := newRequest(h.documentClient(t, "/test-client.json", nil))
	code := h.code(t, r)
	h.db.Close()
	for what, answer := range map[string]tokenAnswer{
		"an exchange":  h.exchange(t, r, code, nil),
		"a revocation": h.revoke(t, r.clientID, "test-token"),
	} {
		wantRefused(t, what, answer, http.StatusServiceUnavailable, "server_error", unavailableDescription)
		if answer.header.Get("Retry-After") == "" {
			t.Errorf("%s: no Retry-After", what)
		}
	}
}
