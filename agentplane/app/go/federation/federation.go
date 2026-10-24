// Package federation exchanges a verified operator login token for a request-scoped
// Action Service token. It deliberately has no router or global token-cache dependency.
package federation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/coreos/go-oidc/v3/oidc"
)

const (
	modeDirect          = "direct"
	modeExchange        = "exchange"
	profileAuthentik    = "authentik"
	profileDex          = "dex"
	clientAssertionType = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
	requestTimeout      = 10 * time.Second
)

// Config is the app-owned federation configuration. Issuer, audience and JWKS values are
// independently pinned for the login and target token; direct mode still checks both profiles.
type Config struct {
	Mode              string
	ServiceURL        string
	LoginJWKSURI      string
	LoginTokenProfile string
	Target            ProviderConfig
	Scope             string
	TokenEndpoint     string
	HTTPClient        *http.Client
}

// ProviderConfig pins one token issuer and audience.
type ProviderConfig struct {
	Issuer       string
	Audience     string
	JWKSURI      string
	TokenProfile string
}

// SessionLoginRow is implemented by *auth.SessionRow. WithLockedLogin must serialize callback
// execution with refresh-token rotation and logout for that browser session.
type SessionLoginRow interface {
	Login(context.Context) (*auth.OperatorSession, error)
	WithLockedLogin(context.Context, func(*auth.OperatorSession) (*auth.OperatorSession, error)) error
}

var _ SessionLoginRow = (*auth.SessionRow)(nil)

// FederationError is a fixed public error code and HTTP status. It never contains provider text
// or token material.
type FederationError struct {
	Code   string
	Status int
}

func (e *FederationError) Error() string { return e.Code }

// UpstreamError describes an identity-provider or Action Service request failure using a
// credential-free URL and the error shape only.
type UpstreamError struct {
	Method         string
	URL            string
	UpstreamStatus int
	ErrorType      string
}

func (e *UpstreamError) Error() string {
	if e.UpstreamStatus != 0 {
		return fmt.Sprintf("upstream %s %s returned status %d", e.Method, e.URL, e.UpstreamStatus)
	}
	return fmt.Sprintf("upstream %s %s failed: %s", e.Method, e.URL, e.ErrorType)
}

// Client verifies the current operator login and mints one target token per exchange call.
type Client struct {
	cfg          Config
	loginIssuer  string
	clientID     string
	clientSecret string
	renewBefore  time.Duration
	tokenURL     string
	tokenURLMu   sync.Mutex
	httpClient   *http.Client
	login        *tokenVerifier
	target       *tokenVerifier
	now          func() time.Time
}

// New validates federation settings and prepares lazy verifiers. Login-provider discovery is
// deferred until a refresh token is needed, as the Python Authlib client does.
func New(cfg Config, loginOIDC auth.OIDCConfig) (*Client, error) {
	if err := validateConfig(cfg, loginOIDC); err != nil {
		return nil, err
	}
	var httpClient *http.Client
	if cfg.HTTPClient == nil {
		httpClient = &http.Client{}
	} else {
		clientCopy := *cfg.HTTPClient
		httpClient = &clientCopy
	}
	httpClient.Timeout = requestTimeout
	httpClient.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	login, err := newTokenVerifier(ProviderConfig{
		Issuer: loginOIDC.Issuer, Audience: loginOIDC.ClientID, JWKSURI: cfg.LoginJWKSURI,
		TokenProfile: cfg.LoginTokenProfile,
	}, httpClient)
	if err != nil {
		return nil, fmt.Errorf("invalid login token verifier settings: %w", err)
	}
	target, err := newTokenVerifier(cfg.Target, httpClient)
	if err != nil {
		return nil, fmt.Errorf("invalid target token verifier settings: %w", err)
	}
	return &Client{
		cfg: cfg, loginIssuer: loginOIDC.Issuer, clientID: loginOIDC.ClientID,
		clientSecret: loginOIDC.ClientSecret,
		renewBefore:  time.Duration(loginOIDC.TokenRenewBeforeSeconds) * time.Second,
		httpClient:   httpClient, login: login, target: target, now: time.Now,
	}, nil
}

// Exchange returns a fresh Action Service bearer bound to the operator session in row.
func (c *Client) Exchange(ctx context.Context, row SessionLoginRow) (string, error) {
	if row == nil {
		return "", federationError("operator_reauthentication_required", http.StatusUnauthorized)
	}
	session, err := c.currentLogin(ctx, row)
	if err != nil {
		return "", err
	}
	if session.Issuer != c.loginIssuer || session.Tokens == nil {
		return "", federationError("operator_reauthentication_required", http.StatusForbidden)
	}
	if !session.Tokens.ExpiresAt.After(c.now()) {
		return "", federationError("operator_reauthentication_required", http.StatusUnauthorized)
	}
	loginToken := session.Tokens.AccessToken
	loginPrincipal, err := c.login.verify(ctx, loginToken, c.now())
	if err != nil {
		return "", err
	}
	if loginPrincipal.Issuer != session.Issuer || loginPrincipal.Subject != session.Subject {
		return "", federationError("operator_federation_identity_mismatch", http.StatusForbidden)
	}

	accessToken := loginToken
	if c.cfg.Mode == modeExchange {
		accessToken, err = c.exchangeToken(ctx, loginToken)
		if err != nil {
			return "", err
		}
	}
	targetPrincipal, err := c.target.verify(ctx, accessToken, c.now())
	if err != nil {
		return "", err
	}
	if targetPrincipal.Subject != loginPrincipal.Subject {
		return "", federationError("operator_federation_identity_mismatch", http.StatusForbidden)
	}
	return accessToken, nil
}

func (c *Client) currentLogin(ctx context.Context, row SessionLoginRow) (*auth.OperatorSession, error) {
	session, err := row.Login(ctx)
	if err != nil {
		return nil, fmt.Errorf("read operator login: %w", err)
	}
	if session == nil {
		return nil, federationError("operator_reauthentication_required", http.StatusUnauthorized)
	}
	if session.Issuer != c.loginIssuer || session.Tokens == nil || session.Tokens.RefreshToken == "" {
		return session, nil
	}
	if session.Tokens.ExpiresAt.Sub(c.now()) > c.renewBefore {
		return session, nil
	}
	var held *auth.OperatorSession
	var refreshErr error
	err = row.WithLockedLogin(ctx, func(current *auth.OperatorSession) (*auth.OperatorSession, error) {
		if current == nil {
			held = nil
			return nil, nil
		}
		held = current
		if current.Issuer != c.loginIssuer || current.Tokens == nil || current.Tokens.RefreshToken == "" ||
			current.Tokens.ExpiresAt.Sub(c.now()) > c.renewBefore {
			return current, nil
		}
		updated, refused, err := c.refresh(ctx, current.Tokens.RefreshToken)
		if err != nil {
			return current, err
		}
		if refused {
			refreshErr = federationError("operator_reauthentication_required", http.StatusUnauthorized)
			held = nil
			return nil, nil
		}
		held = &auth.OperatorSession{
			Issuer: current.Issuer, Subject: current.Subject, Username: current.Username, Tokens: updated,
		}
		return held, nil
	})
	if err != nil {
		return nil, err
	}
	if refreshErr != nil {
		return nil, refreshErr
	}
	if held == nil {
		return nil, federationError("operator_reauthentication_required", http.StatusUnauthorized)
	}
	return held, nil
}

func (c *Client) refresh(ctx context.Context, refreshToken string) (*auth.LoginTokens, bool, error) {
	tokenURL, err := c.loginTokenURL(ctx)
	if err != nil {
		return nil, false, err
	}
	form := url.Values{"grant_type": {"refresh_token"}, "refresh_token": {refreshToken}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, false, federationError("operator_federation_exchange_failed", http.StatusBadGateway)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.SetBasicAuth(c.clientID, c.clientSecret)
	response, err := c.httpClient.Do(req)
	if err != nil {
		return nil, false, upstreamError(req, 0, errorType(err))
	}
	defer response.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if readErr != nil {
		return nil, false, upstreamError(req, 0, errorType(readErr))
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var oauthErr struct {
			Error string `json:"error"`
		}
		if (response.StatusCode == http.StatusBadRequest || response.StatusCode == http.StatusUnauthorized) &&
			json.Unmarshal(body, &oauthErr) == nil && oauthErr.Error != "" {
			return nil, true, nil
		}
		return nil, false, upstreamError(req, response.StatusCode, "HTTPStatusError")
	}
	token, err := parseTokenResponse(body, c.now())
	if err != nil || !token.ExpiresAt.After(c.now()) {
		return nil, false, federationError("operator_federation_exchange_failed", http.StatusBadGateway)
	}
	if token.RefreshToken == "" {
		token.RefreshToken = refreshToken
	}
	return token, false, nil
}

func (c *Client) loginTokenURL(ctx context.Context) (string, error) {
	c.tokenURLMu.Lock()
	defer c.tokenURLMu.Unlock()
	if c.tokenURL != "" {
		return c.tokenURL, nil
	}
	discoveryCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	provider, err := oidc.NewProvider(oidc.ClientContext(discoveryCtx, c.httpClient), c.loginIssuer)
	if err != nil {
		return "", federationError("operator_federation_exchange_failed", http.StatusBadGateway)
	}
	tokenURL := provider.Endpoint().TokenURL
	if !validTokenEndpoint(tokenURL) {
		return "", federationError("operator_federation_exchange_failed", http.StatusBadGateway)
	}
	c.tokenURL = tokenURL
	return tokenURL, nil
}

func (c *Client) exchangeToken(ctx context.Context, loginToken string) (string, error) {
	form := url.Values{
		"client_id":             {c.cfg.Target.Audience},
		"grant_type":            {"client_credentials"},
		"client_assertion_type": {clientAssertionType},
		"client_assertion":      {loginToken},
		"scope":                 {c.cfg.Scope},
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.TokenEndpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", federationError("operator_federation_exchange_failed", http.StatusBadGateway)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	response, err := c.httpClient.Do(req)
	if err != nil {
		return "", upstreamError(req, 0, errorType(err))
	}
	defer response.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if readErr != nil {
		return "", upstreamError(req, 0, errorType(readErr))
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return "", upstreamError(req, response.StatusCode, "HTTPStatusError")
	}
	var token tokenResponse
	if err := json.Unmarshal(body, &token); err != nil || strings.TrimSpace(token.AccessToken) == "" {
		return "", federationError("operator_federation_exchange_failed", http.StatusBadGateway)
	}
	if !strings.EqualFold(token.TokenType, "Bearer") {
		return "", tokenInvalid()
	}
	return token.AccessToken, nil
}

type tokenResponse struct {
	AccessToken  string          `json:"access_token"`
	TokenType    string          `json:"token_type"`
	ExpiresIn    json.Number     `json:"expires_in"`
	ExpiresAt    json.RawMessage `json:"expires_at"`
	RefreshToken string          `json:"refresh_token"`
}

func parseTokenResponse(body []byte, now time.Time) (*auth.LoginTokens, error) {
	var token tokenResponse
	if err := json.Unmarshal(body, &token); err != nil || strings.TrimSpace(token.AccessToken) == "" {
		return nil, errors.New("invalid token response")
	}
	expiresAt, err := tokenExpiry(token, now)
	if err != nil {
		return nil, err
	}
	return &auth.LoginTokens{
		AccessToken: token.AccessToken, ExpiresAt: expiresAt,
		RefreshToken: token.RefreshToken,
	}, nil
}

func tokenExpiry(token tokenResponse, now time.Time) (time.Time, error) {
	if len(token.ExpiresAt) > 0 && string(token.ExpiresAt) != "null" {
		if token.ExpiresAt[0] == '"' {
			var text string
			if json.Unmarshal(token.ExpiresAt, &text) != nil {
				return time.Time{}, errors.New("invalid token expiry")
			}
			parsed, err := time.Parse(time.RFC3339Nano, text)
			if err != nil {
				return time.Time{}, errors.New("invalid token expiry")
			}
			return parsed, nil
		}
		var number json.Number
		if json.Unmarshal(token.ExpiresAt, &number) != nil {
			return time.Time{}, errors.New("invalid token expiry")
		}
		seconds, err := number.Float64()
		maxSeconds := float64(math.MaxInt64 / int64(time.Second))
		minSeconds := float64(math.MinInt64 / int64(time.Second))
		if err != nil || math.IsNaN(seconds) || math.IsInf(seconds, 0) || seconds >= maxSeconds || seconds <= minSeconds {
			return time.Time{}, errors.New("invalid token expiry")
		}
		wholeSeconds := math.Floor(seconds)
		nanoseconds := int64((seconds - wholeSeconds) * float64(time.Second))
		return time.Unix(int64(wholeSeconds), nanoseconds), nil
	}
	expiresIn, err := token.ExpiresIn.Int64()
	maxSeconds := int64(math.MaxInt64 / int64(time.Second))
	if err != nil || expiresIn <= 0 || expiresIn > maxSeconds {
		return time.Time{}, errors.New("invalid token lifetime")
	}
	return now.Add(time.Duration(expiresIn) * time.Second), nil
}

func validateConfig(cfg Config, login auth.OIDCConfig) error {
	if cfg.Mode != modeDirect && cfg.Mode != modeExchange {
		return errors.New("action_federation.mode must be direct or exchange")
	}
	if !validServiceURL(cfg.ServiceURL) {
		return errors.New("action_federation.service_url must be an HTTP(S) endpoint URL without credentials, query, or fragment")
	}
	if !validIssuerOrJWKSURL(login.Issuer) || strings.TrimSpace(login.ClientID) == "" || login.ClientSecret == "" {
		return errors.New("login OIDC issuer, client ID, and client secret are required")
	}
	if !validIssuerOrJWKSURL(cfg.LoginJWKSURI) {
		return errors.New("action_federation.login_jwks_uri must be an HTTPS URL, or HTTP on loopback")
	}
	if cfg.LoginTokenProfile != profileAuthentik && cfg.LoginTokenProfile != profileDex {
		return errors.New("action_federation.login_token_profile must be authentik or dex")
	}
	if err := validateProvider(cfg.Target); err != nil {
		return fmt.Errorf("action_federation.target: %w", err)
	}
	if strings.TrimSpace(cfg.Scope) == "" {
		return errors.New("action_federation.scope must not be blank")
	}
	if cfg.Mode == modeExchange && !validTokenEndpoint(cfg.TokenEndpoint) {
		return errors.New("action_federation.token_endpoint must be an HTTPS URL, or HTTP on loopback, without credentials, query, or fragment")
	}
	if cfg.Mode == modeDirect && cfg.TokenEndpoint != "" {
		return errors.New("action_federation.token_endpoint must be empty in direct mode")
	}
	maxDurationSeconds := int64(^uint64(0)>>1) / int64(time.Second)
	if login.TokenRenewBeforeSeconds < 0 || int64(login.TokenRenewBeforeSeconds) > maxDurationSeconds {
		return errors.New("OIDC token renewal threshold is outside the supported duration range")
	}
	return nil
}

func validateProvider(provider ProviderConfig) error {
	if !validIssuerOrJWKSURL(provider.Issuer) {
		return errors.New("issuer must be HTTPS, or HTTP on loopback")
	}
	if strings.TrimSpace(provider.Audience) == "" {
		return errors.New("audience must not be blank")
	}
	if !validIssuerOrJWKSURL(provider.JWKSURI) {
		return errors.New("jwks_uri must be HTTPS, or HTTP on loopback")
	}
	if provider.TokenProfile != profileAuthentik && provider.TokenProfile != profileDex {
		return errors.New("token_profile must be authentik or dex")
	}
	return nil
}

func validServiceURL(raw string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && (parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.Host != "" &&
		parsed.User == nil && parsed.RawQuery == "" && parsed.Fragment == "" && !strings.Contains(raw, "#")
}

func validTokenEndpoint(raw string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && (parsed.Scheme == "https" || parsed.Scheme == "http" && loopback(parsed.Hostname())) &&
		parsed.Host != "" && parsed.User == nil && parsed.RawQuery == "" && parsed.Fragment == "" && !strings.Contains(raw, "#")
}

func validIssuerOrJWKSURL(raw string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && (parsed.Scheme == "https" || parsed.Scheme == "http" && loopback(parsed.Hostname())) &&
		parsed.Host != "" && parsed.User == nil && parsed.Fragment == "" && !strings.Contains(raw, "#")
}

func loopback(host string) bool {
	if strings.EqualFold(host, "localhost") || strings.HasSuffix(strings.ToLower(host), ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func federationError(code string, status int) *FederationError {
	return &FederationError{Code: code, Status: status}
}

func upstreamError(req *http.Request, status int, errorType string) *UpstreamError {
	parsed := *req.URL
	parsed.User = nil
	parsed.RawQuery = ""
	parsed.ForceQuery = false
	parsed.Fragment = ""
	parsed.RawFragment = ""
	return &UpstreamError{Method: req.Method, URL: parsed.String(), UpstreamStatus: status, ErrorType: errorType}
}

func errorType(err error) string {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "TimeoutError"
	}
	return fmt.Sprintf("%T", err)
}
