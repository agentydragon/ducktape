package authserver

import (
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
)

// enrollments is how many enrollments the authorization requests created.
func (h *harness) enrollments(t *testing.T) int {
	t.Helper()
	var count int
	if err := h.db.QueryRowContext(t.Context(), `SELECT count(*) FROM connection_enrollment`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestAnAuthorizationRequestRecordsItsEnrollmentAndSendsTheBrowserToConsent(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	location := must(h.follow(h.authorizeURL(r, nil)))(t)
	handle, found := strings.CutPrefix(location, consentPrefix)
	if !found {
		t.Fatalf("redirected to %s, want consent", location)
	}
	// Consent previews the client, as the authorization request named it.
	preview := must(h.connections.PreviewConnectionEnrollment(as(t, testOperator), &pb.PreviewConnectionEnrollmentRequest{
		Handle: handle, BrowserBinding: testBrowser,
	}))(t)
	if preview.GetClientId() != r.clientID || preview.GetClientName() != "Test DCR client" ||
		preview.GetRedirectUri() != testRedirectURI {
		t.Errorf("preview %v", preview)
	}
	// An authorization request of the same client, redirect URI and code challenge cannot start another.
	retried := r
	retried.state = "test-retried-state"
	wantError(t, "a reused authorization request", h.answerAt(t, retried, h.authorizeURL(retried, nil)),
		"invalid_request", "This authorization interaction cannot be reused.")
}

func TestAnAuthorizationRequestTheServerCannotServeIsRefusedAtTheClientsRedirectURI(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	for _, refused := range []struct {
		what   string
		change func(url.Values)
		code   string
	}{
		{"prompt=none", func(q url.Values) { q.Set("prompt", "none") }, "interaction_required"},
		{"the fragment response mode", func(q url.Values) { q.Set("response_mode", "fragment") }, "invalid_request"},
		{"no code challenge", func(q url.Values) { q.Del("code_challenge"); q.Del("code_challenge_method") }, "invalid_request"},
		{"an S256 method with no code challenge", func(q url.Values) { q.Del("code_challenge") }, "invalid_request"},
		{"a plain code challenge", func(q url.Values) { q.Set("code_challenge_method", "plain") }, "invalid_request"},
		{"another resource", func(q url.Values) { q.Set("resource", "https://test-other-resource.example/mcp") }, "invalid_target"},
		{"a second resource", func(q url.Values) { q.Add("resource", "https://test-other-resource.example/mcp") }, "invalid_target"},
	} {
		r := newRequest(clientID)
		wantError(t, refused.what, h.answerAt(t, r, h.authorizeURL(r, refused.change)),
			refused.code, "")
	}
	if count := h.enrollments(t); count != 0 {
		t.Errorf("the refused requests created %d enrollments", count)
	}
}

func TestAnAuthorizationRequestNotForARegisteredRedirectIsRenderedNotRedirected(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	for _, refused := range []struct {
		what   string
		change func(url.Values)
	}{
		{"an unknown client", func(q url.Values) { q.Set("client_id", "test-unknown-client") }},
		{"an unregistered redirect URI", func(q url.Values) { q.Set("redirect_uri", "https://test-attacker.example/callback") }},
		{"a request object", func(q url.Values) { q.Set("request", "test-request-object") }},
	} {
		response, body := must2(h.load(h.authorizeURL(newRequest(clientID), refused.change)))(t)
		if response.StatusCode != http.StatusBadRequest || response.Header.Get("Location") != "" {
			t.Errorf("%s: %s %s, want a rendered refusal", refused.what, response.Status, body)
		}
	}
}

func TestAnOutageAtAuthorizationIsAnsweredAsTemporary(t *testing.T) {
	h := start(t)
	registered := h.client(t)
	published := h.documentClient(t, "/test-client.json", nil)
	h.db.Close()
	// A registered client cannot be read: the refusal is rendered.
	response, body := must2(h.load(h.authorizeURL(newRequest(registered), nil)))(t)
	if response.StatusCode != http.StatusServiceUnavailable || response.Header.Get("Retry-After") == "" {
		t.Errorf("looking up a registered client: %s %s", response.Status, body)
	}
	// A client with a metadata document is known: the client is told at its redirect URI.
	r := newRequest(published)
	wantError(t, "recording the enrollment", h.answerAt(t, r, h.authorizeURL(r, nil)),
		"temporarily_unavailable", unavailableDescription)
}

// upstreamURL is where the operator's allow of the request sends the browser.
func (h *harness) upstreamURL(t *testing.T, r request) string {
	t.Helper()
	return must(h.consent(t.Context(), h.authorizeURL(r, nil), testOperator, newConnection(callerAccount)))(t).GetRedirectUrl()
}

func TestTheCallbackSendsACodeOnlyForASignInOfThePinnedOperator(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	refusals := []struct {
		what              string
		before            func(t *testing.T, r request)
		code, description string
	}{
		{"a sign-in of another account", func(*testing.T, request) {
			h.upstream.change(func(u *fakeUpstream) { u.subject = testUpstreamStranger })
		}, "access_denied", notOperator},
		{"a sign-in whose access token the provider's keys do not verify", func(*testing.T, request) {
			h.upstream.change(func(u *fakeUpstream) { u.forging = true })
		}, "access_denied", upstreamUnverified},
		{"a sign-in whose access token is for another client", func(*testing.T, request) {
			h.upstream.change(func(u *fakeUpstream) { u.altering = func(c map[string]any) { c["aud"] = "test-other-upstream-client" } })
		}, "access_denied", upstreamUnverified},
		{"a sign-in whose access token another issuer issued", func(*testing.T, request) {
			h.upstream.change(func(u *fakeUpstream) {
				u.altering = func(c map[string]any) { c["iss"] = "https://test-other-issuer.example" }
			})
		}, "access_denied", upstreamUnverified},
		{"a sign-in whose access token has expired", func(*testing.T, request) {
			h.upstream.change(func(u *fakeUpstream) {
				u.altering = func(c map[string]any) { c["exp"] = time.Now().Add(-time.Minute).Unix() }
			})
		}, "access_denied", upstreamUnverified},
		{"an expired authorization", func(t *testing.T, r request) {
			h.exec(t, `UPDATE oauth_authorization SET expires_at = now() - interval '1 second'`)
		}, "access_denied", authorizationExpiry},
	}
	for _, refused := range refusals {
		r := newRequest(clientID)
		upstream := h.upstreamURL(t, r)
		refused.before(t, r)
		wantError(t, refused.what, h.answer(t, r, must(h.signIn(upstream))(t)), refused.code, refused.description)
		h.upstream.change(func(u *fakeUpstream) { u.subject, u.forging, u.altering = testUpstreamSubject, false, nil })
	}
	r := newRequest(clientID)
	h.code(t, r)
}

func TestTheCallbackAnswersAnOutageOfTheProviderAsTemporary(t *testing.T) {
	h := start(t)
	clientID := h.client(t)
	// The key set's outage comes first, while none of the provider's keys is cached.
	for _, path := range []string{"/jwks", "/token"} {
		r := newRequest(clientID)
		upstream := h.upstreamURL(t, r)
		h.upstream.change(func(u *fakeUpstream) { u.failing = path })
		wantError(t, "an outage of "+path, h.answer(t, r, must(h.signIn(upstream))(t)), "temporarily_unavailable",
			unavailableDescription)
		h.upstream.change(func(u *fakeUpstream) { u.failing = "" })
	}
}

func TestTheCallbackPassesOnTheProvidersRefusal(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	upstream := must(url.Parse(h.upstreamURL(t, r)))(t)
	callback := h.url + callbackPath + "?" + url.Values{
		"error": {"access_denied"}, "state": {upstream.Query().Get("state")},
	}.Encode()
	wantError(t, "a refused sign-in", h.answerAt(t, r, callback), "access_denied",
		upstreamRefused)
}

func TestTheCallbackServesEachUpstreamStateOnce(t *testing.T) {
	h := start(t)
	r := newRequest(h.client(t))
	callback := must(h.follow(h.upstreamURL(t, r)))(t)
	h.answerAt(t, r, callback)
	for what, rawURL := range map[string]string{
		"the callback again":       callback,
		"a state it did not issue": h.url + callbackPath + "?state=test-unknown-state&code=test-code",
	} {
		response, body := must2(h.load(rawURL))(t)
		if response.StatusCode != http.StatusBadRequest || response.Header.Get("Location") != "" {
			t.Errorf("%s: %s %s, want a rendered refusal", what, response.Status, body)
		}
	}
}
