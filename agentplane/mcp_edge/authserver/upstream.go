package authserver

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
)

// upstreamTimeout bounds each request to the upstream provider.
const upstreamTimeout = 10 * time.Second

var (
	// errUpstreamDown is an outage of the upstream provider, a failed request or a 5xx answer, which a retry may get
	// past.
	errUpstreamDown = errors.New("the upstream OIDC provider is unavailable")
	// errUnverified is an upstream answer that proves no sign-in.
	errUnverified = errors.New("the upstream sign-in could not be verified")
	// errNotOperator is a verified sign-in of an account other than the pinned operator's.
	errNotOperator = errors.New("the upstream account that signed in is not this service's operator")
)

// upstream is the OIDC provider the pinned operator signs in at.
type upstream struct {
	oauth  oauth2.Config
	client *http.Client
	// keys is the provider's key set, which verifier checks signatures with.
	keys     *oidc.RemoteKeySet
	verifier *oidc.IDTokenVerifier
	subject  string
}

// session is a verified sign-in of the pinned operator: when it lapses, and what renews it, if anything does.
type session struct {
	expiresAt    time.Time
	refreshToken *string
}

func newUpstream(ctx context.Context, config Config, redirectURL string) (*upstream, error) {
	client := &http.Client{Transport: outageTransport{next: http.DefaultTransport}, Timeout: upstreamTimeout}
	// The key set fetches with this context for as long as the server runs, so it keeps no deadline of New's.
	ctx = oidc.ClientContext(context.WithoutCancel(ctx), client)
	provider, err := oidc.NewProvider(ctx, config.UpstreamIssuer)
	if err != nil {
		return nil, err
	}
	var discovered struct {
		JWKSURL string `json:"jwks_uri"`
	}
	if err := provider.Claims(&discovered); err != nil {
		return nil, err
	}
	keys := oidc.NewRemoteKeySet(ctx, discovered.JWKSURL)
	return &upstream{
		oauth: oauth2.Config{
			ClientID: config.UpstreamClientID, ClientSecret: config.UpstreamClientSecret, Endpoint: provider.Endpoint(),
			RedirectURL: redirectURL,
			// offline_access, so a refresh can verify the operator again.
			Scopes: []string{oidc.ScopeOpenID, "email", "profile", oidc.ScopeOfflineAccess},
		},
		client: client,
		keys:   keys,
		verifier: oidc.NewVerifier(config.UpstreamIssuer, keys, &oidc.Config{
			ClientID: config.UpstreamClientID, SupportedSigningAlgs: []string{oidc.RS256},
		}),
		subject: config.UpstreamSubject,
	}, nil
}

// authorizeURL is where the browser signs in upstream, with the upstream leg's own state and PKCE verifier.
func (u *upstream) authorizeURL(state, verifier string) string {
	return u.oauth.AuthCodeURL(state, oauth2.S256ChallengeOption(verifier))
}

// signIn redeems the code of an upstream sign-in, which must be the pinned operator's.
func (u *upstream) signIn(ctx context.Context, code, verifier string) (session, error) {
	token, err := u.oauth.Exchange(oidc.ClientContext(ctx, u.client), code, oauth2.VerifierOption(verifier))
	if err != nil {
		return session{}, upstreamFailure(err)
	}
	return u.verify(ctx, token)
}

// renew refreshes the pinned operator's upstream session and verifies it again.
func (u *upstream) renew(ctx context.Context, refreshToken string) (session, error) {
	token, err := u.oauth.TokenSource(oidc.ClientContext(ctx, u.client), &oauth2.Token{RefreshToken: refreshToken}).Token()
	if err != nil {
		return session{}, upstreamFailure(err)
	}
	return u.verify(ctx, token)
}

// verify reads the operator from the upstream access token, as the Python service did: the provider (Authentik)
// issues it as a JWT for this client, and renews it on every refresh, which the id token need not be.
func (u *upstream) verify(ctx context.Context, token *oauth2.Token) (session, error) {
	// go-oidc reports a key set's failure only as text; checking the signature first keeps an outage of the
	// provider's keys apart from a token they do not verify.
	if _, err := u.keys.VerifySignature(ctx, token.AccessToken); err != nil {
		return session{}, upstreamFailure(err)
	}
	claims, err := u.verifier.Verify(ctx, token.AccessToken)
	switch {
	case err != nil:
		return session{}, fmt.Errorf("%w: %w", errUnverified, err)
	case claims.Subject != u.subject:
		return session{}, errNotOperator
	}
	signedIn := session{expiresAt: claims.Expiry}
	if token.RefreshToken != "" {
		signedIn.refreshToken = &token.RefreshToken
	}
	return signedIn, nil
}

// upstreamFailure keeps an outage an outage; anything else the provider answered refuses the sign-in.
func upstreamFailure(err error) error {
	if errors.Is(err, errUpstreamDown) {
		return err
	}
	return fmt.Errorf("%w: %w", errUnverified, err)
}

// outageTransport reports a failed request or a 5xx answer of the provider as an error wrapping errUpstreamDown,
// which x/oauth2 and go-oidc's key set keep in the errors they return.
type outageTransport struct{ next http.RoundTripper }

func (t outageTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	response, err := t.next.RoundTrip(request)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", errUpstreamDown, err)
	}
	if response.StatusCode >= http.StatusInternalServerError {
		response.Body.Close()
		return nil, fmt.Errorf("%w: %s %s", errUpstreamDown, request.URL.Redacted(), response.Status)
	}
	return response, nil
}
