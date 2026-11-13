package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/baggage"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var (
	bearerPattern     = regexp.MustCompile(`(?i)^Bearer +([A-Za-z0-9._~+/=-]+)$`)
	credentialHeaders = map[string]struct{}{
		"authorization": {}, "x-api-key": {}, "x-litellm-api-key": {},
	}
	untrustedMetadataHeaders = map[string]struct{}{
		"x-litellm-agent-id": {}, "x-litellm-customer-id": {}, "x-litellm-spend-logs-metadata": {},
	}
	identityHeaderPrefixes = []string{"x-agentplane-", "x-sandbox-", "x-pod-", "x-agent-", "x-thread-"}
	proxyMethods           = []string{http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodOptions, http.MethodHead}
)

type ingressHandler struct {
	// admission admits every inference request: a workload subject directly, or a configured
	// central sampler acting for an Identity.
	admission       auth.Rule
	transport       *http.Transport
	roundTripper    http.RoundTripper
	baseURL         *url.URL
	key             string
	attribution     attributionConfig
	codex           *codexCatalog
	logBody         bool
	logger          *slog.Logger
	pool            chan struct{}
	poolWaitTimeout time.Duration

	centralSamplerAccounts []string
}

// newIngressHandler serves inference for the subjects resolver authenticates. identities, required
// by central_sampler_identity_callers, reads the live Identities the central samplers may act for.
func newIngressHandler(c config, resolver *auth.Resolver, identities auth.IdentityUIDReader, logger *slog.Logger) (*ingressHandler, error) {
	baseURL, err := url.Parse(c.LiteLLMURL)
	if err != nil {
		return nil, errors.New("invalid LiteLLM URL")
	}
	admission := auth.AnySubject(resolver)
	if c.CentralSamplerIdentityCallers {
		delegators := make([]auth.Delegator, 0, len(c.CentralSamplerAccounts))
		for _, name := range c.CentralSamplerAccounts {
			var account auth.Account
			if err := account.UnmarshalText([]byte(name)); err != nil {
				return nil, fmt.Errorf("central_sampler_accounts: %w", err)
			}
			delegators = append(delegators, auth.Delegator{Account: account, IdentityCallers: true})
		}
		if admission, err = admission.AllowDelegationFrom(delegators, nil, identities); err != nil {
			return nil, fmt.Errorf("central_sampler_identity_callers: %w", err)
		}
	}
	transport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return nil, errors.New("default HTTP transport is not configurable")
	}
	transport = transport.Clone()
	transport.DisableCompression = true
	transport.TLSHandshakeTimeout = 5 * time.Second
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 20
	transport.MaxConnsPerHost = 100
	dialer := &net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		conn, err := dialer.DialContext(ctx, network, address)
		if err != nil {
			return nil, err
		}
		return writeTimeoutConn{Conn: conn, timeout: 60 * time.Second}, nil
	}
	var codex *codexCatalog
	if c.CodexModelCatalogFile != "" {
		if codex, err = loadCodexCatalog(c.CodexModelCatalogFile); err != nil {
			return nil, err
		}
	}
	return &ingressHandler{
		admission:       admission,
		transport:       transport,
		roundTripper:    observability.HTTPTransport(transport),
		baseURL:         baseURL,
		key:             c.LiteLLMKey,
		attribution:     c.Attribution,
		codex:           codex,
		logBody:         c.LogLLMRequests,
		logger:          logger,
		pool:            make(chan struct{}, 100),
		poolWaitTimeout: 5 * time.Second,

		centralSamplerAccounts: c.CentralSamplerAccounts,
	}, nil
}

// writeTimeoutConn bounds a blocked outbound request write without imposing a
// timeout on the response stream read from the same full-duplex connection.
type writeTimeoutConn struct {
	net.Conn
	timeout time.Duration
}

func (c writeTimeoutConn) Write(data []byte) (int, error) {
	if err := c.Conn.SetWriteDeadline(time.Now().Add(c.timeout)); err != nil {
		return 0, err
	}
	return c.Conn.Write(data)
}

func (h *ingressHandler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.URL.Path == "/healthz" && request.Method == http.MethodGet {
		writeHealth(writer)
		return
	}
	if !allowedMethod(request.Method) {
		writer.Header().Set("Allow", strings.Join(proxyMethods, ", "))
		http.Error(writer, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}
	caller, okay := h.authenticate(writer, request)
	if !okay {
		return
	}
	h.serveAuthenticated(writer, request, caller)
}

func writeHealth(writer http.ResponseWriter) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(writer, `{"status":"ok"}`)
}

func (h *ingressHandler) serveAuthenticated(writer http.ResponseWriter, request *http.Request, caller verifiedCaller) {
	if request.URL.Path == codexCatalogPath && request.Method == http.MethodGet {
		if h.codex == nil {
			writeJSONError(writer, http.StatusNotFound, "Codex model catalog is not configured", "")
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		writer.Header().Set("ETag", h.codex.etag)
		_, _ = writer.Write(h.codex.body)
		return
	}
	h.forward(writer, request, caller)
}

func allowedMethod(method string) bool {
	return slices.Contains(proxyMethods, method)
}

// verifiedCaller is who an admitted request is attributed to.
type verifiedCaller struct {
	claims   map[verifiedClaim]string
	logAttrs []any
	// account is a direct ServiceAccount caller, which alone may claim a Session by header.
	account *auth.Principal
	// sessionID is the central Session a delegating sampler named in its envelope; its Identity is
	// the subject, so no header claim applies.
	sessionID string
}

func (h *ingressHandler) authenticate(writer http.ResponseWriter, request *http.Request) (verifiedCaller, bool) {
	// Preserve the HTTP API's case-insensitive Bearer spelling. Credential
	// presence still selects one configured mode; malformed or combined
	// credentials never enable a second authentication attempt.
	values := request.Header.Values("Authorization")
	if len(values) == 1 {
		if match := bearerPattern.FindStringSubmatch(values[0]); match != nil {
			request = request.Clone(request.Context())
			request.Header.Set("Authorization", "Bearer "+match[1])
		}
	}
	admitted, err := h.admission.AdmitRequest(request, 0)
	var caller verifiedCaller
	if err == nil {
		caller, err = admittedCaller(admitted)
	}
	switch {
	case err == nil:
		return caller, true
	case errors.Is(err, auth.ErrInvalidBearer) || status.Code(err) == codes.Unauthenticated:
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
	case status.Code(err) == codes.PermissionDenied:
		writeJSONError(writer, http.StatusForbidden, "delegated caller refused", "")
	default:
		writeJSONError(writer, http.StatusInternalServerError, "workload authentication unavailable", "")
	}
	return verifiedCaller{}, false
}

// admittedCaller attributes the admitted call: a direct subject by its own claims; a trusted
// central sampler acting for an Identity by that Identity, with the sampler's Pod as the Pod that
// called, and the Session its envelope names.
func admittedCaller(ctx context.Context) (verifiedCaller, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return verifiedCaller{}, err
	}
	switch admission := admission.(type) {
	case auth.DirectAdmission:
		principal, err := auth.Caller(ctx)
		if err != nil {
			return verifiedCaller{}, err
		}
		return verifiedCaller{claims: subjectClaims(principal), logAttrs: callerLogAttrs(principal), account: &principal}, nil
	case auth.IdentityAdmission:
		return verifiedCaller{claims: subjectClaims(admission.Caller), logAttrs: callerLogAttrs(admission.Caller)}, nil
	case auth.DelegatedIdentityAdmission:
		session := admission.Provenance.GetSession()
		if session == nil {
			return verifiedCaller{}, status.Error(codes.PermissionDenied, "a delegated inference call names the Session it serves")
		}
		actor := admission.Actor
		return verifiedCaller{
			claims: map[verifiedClaim]string{
				verifiedIdentity: admission.EffectiveCaller.Name, verifiedIdentityUID: admission.EffectiveCaller.UID,
				verifiedPodName: actor.PodName, verifiedPodUID: actor.PodUID,
			},
			logAttrs: append([]any{
				"subject_kind", "identity", "subject_name", admission.EffectiveCaller.Name,
				"actor", actor.Account.Namespace + "/" + actor.Account.Name,
			}, podLogAttrs(actor.PodName, actor.PodUID)...),
			sessionID: session.GetSessionId(),
		}, nil
	default:
		return verifiedCaller{}, status.Error(codes.PermissionDenied, "the admitted caller kind is not served")
	}
}

func (h *ingressHandler) forward(writer http.ResponseWriter, incoming *http.Request, caller verifiedCaller) {
	requestID := rand.Text()
	select {
	case h.pool <- struct{}{}:
		defer func() { <-h.pool }()
	case <-incoming.Context().Done():
		return
	case <-time.After(h.poolWaitTimeout):
		attrs := requestLogAttrs(requestID, incoming, caller)
		attrs = append(attrs, "timeout", h.poolWaitTimeout.String())
		h.logger.Warn("llm forwarding slot acquisition timed out", attrs...)
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}

	sessionID := incoming.Header.Get("x-agentplane-session-id")
	switch {
	case caller.sessionID != "" && sessionID != "":
		writeJSONError(writer, http.StatusForbidden, "a delegated call names its Session in the envelope, not a header", "")
		return
	case caller.sessionID != "":
		sessionID = caller.sessionID
	case sessionID != "":
		// Central samplers are ServiceAccounts; an Identity caller never attributes a Session.
		if caller.account == nil || !slices.Contains(h.centralSamplerAccounts, caller.account.Account.Namespace+"/"+caller.account.Account.Name) {
			writeJSONError(writer, http.StatusForbidden, "Session attribution requires an authorized central sampler", "")
			return
		}
		parsed, err := uuid.Parse(sessionID)
		if err != nil || parsed.String() != sessionID {
			writeJSONError(writer, http.StatusBadRequest, "Session attribution requires a canonical UUID", "")
			return
		}
	}
	verified, err := verifiedRequestHeaders(caller.claims, h.key, h.attribution, sessionID)
	if err != nil {
		writeJSONError(writer, http.StatusInternalServerError, "Could not attach verified attribution", "")
		return
	}

	// The contract is plain HTTP request/response streaming: without an Upgrade value,
	// ReverseProxy forwards no protocol switch to LiteLLM.
	incoming.Header.Del("Upgrade")

	var requestBody *boundedCapture
	proxy := &httputil.ReverseProxy{
		Rewrite: func(out *httputil.ProxyRequest) {
			out.SetURL(h.baseURL)
			dropUntrustedRequestHeaders(out.Out.Header, h.attribution)
			maps.Copy(out.Out.Header, verified)
			// A bodyless request has a nil or NoBody Body. Wrapping nil makes the transport's body
			// probe dereference it on its own goroutine, outside the handler's recovery, and the
			// whole process exits.
			if h.logBody && out.Out.Body != nil && out.Out.Body != http.NoBody {
				requestBody = &boundedCapture{ReadCloser: out.Out.Body, limit: maxLoggedRequestBody}
				out.Out.Body = requestBody
			}
		},
		Transport:     h.roundTripper,
		FlushInterval: -1,
		ModifyResponse: func(response *http.Response) error {
			for name := range credentialHeaders {
				response.Header.Del(name)
			}
			if h.codex != nil && incoming.URL.Path == "/v1/responses" {
				response.Header.Set("X-Models-Etag", h.codex.etag)
			}
			if h.logBody {
				h.logger.Info("llm response start", "id", requestID, "status", response.StatusCode,
					"content_type", response.Header.Get("Content-Type"))
				response.Body = &loggedResponseBody{ReadCloser: response.Body, logger: h.logger, id: requestID, status: response.StatusCode}
			}
			return nil
		},
		ErrorLog: slog.NewLogLogger(h.logger.Handler(), slog.LevelWarn),
		ErrorHandler: func(writer http.ResponseWriter, request *http.Request, err error) {
			if request.Context().Err() == nil {
				attrs := requestLogAttrs(requestID, incoming, caller)
				var urlErr *url.Error
				if errors.As(err, &urlErr) {
					err = urlErr.Err
				}
				attrs = append(attrs, "error", err)
				h.logger.Warn("llm backend forwarding failed", attrs...)
				writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
			}
		},
	}
	if h.logBody {
		defer func() {
			if requestBody == nil {
				return
			}
			attrs := append([]any{"id", requestID}, caller.logAttrs...)
			h.logger.Info("llm request", append(attrs, "method", incoming.Method,
				"path", incoming.URL.Path, "body", fmt.Sprintf("%q", requestBody.captured.Bytes()),
				"body_bytes", requestBody.total, "body_truncated", requestBody.total > requestBody.captured.Len())...)
		}()
	}
	proxy.ServeHTTP(writer, incoming)
}

func requestLogAttrs(requestID string, incoming *http.Request, caller verifiedCaller) []any {
	return append([]any{"id", requestID, "method", incoming.Method, "path", incoming.URL.Path}, caller.logAttrs...)
}

// callerLogAttrs names the verified caller by subject kind and name (a ServiceAccount also by
// namespace), and its Pod when known, so an Identity caller never logs as an empty account.
func callerLogAttrs(subject auth.Subject) []any {
	switch subject := subject.(type) {
	case auth.Principal:
		attrs := []any{"subject_kind", "service_account", "subject_name", subject.Account.Name}
		if subject.Account.Namespace != "" {
			attrs = append(attrs, "namespace", subject.Account.Namespace)
		}
		return append(attrs, podLogAttrs(subject.PodName, subject.PodUID)...)
	case auth.IdentityPrincipal:
		return append([]any{"subject_kind", "identity", "subject_name", subject.Identity.Name}, podLogAttrs(subject.PodName, subject.PodUID)...)
	default:
		panic(fmt.Sprintf("unknown subject %T", subject))
	}
}

func podLogAttrs(podName, podUID string) []any {
	if podName == "" {
		return nil
	}
	return []any{"pod", podName, "pod_uid", podUID}
}

// maxLoggedRequestBody bounds the request body prefix kept for `log_llm_requests`; the body itself
// is streamed to LiteLLM, never buffered.
const maxLoggedRequestBody = 1 << 20

// boundedCapture passes a body through unchanged while keeping its first `limit` bytes.
type boundedCapture struct {
	io.ReadCloser
	limit    int
	captured bytes.Buffer
	total    int
}

func (c *boundedCapture) Read(p []byte) (int, error) {
	n, err := c.ReadCloser.Read(p)
	c.captured.Write(p[:min(n, max(c.limit-c.captured.Len(), 0))])
	c.total += n
	return n, err
}

// loggedResponseBody logs each raw response chunk and whether the stream completed.
type loggedResponseBody struct {
	io.ReadCloser
	logger   *slog.Logger
	id       string
	status   int
	chunk    int
	complete bool
}

func (b *loggedResponseBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if n > 0 {
		b.logger.Info("llm response chunk", "id", b.id, "status", b.status, "chunk", b.chunk, "body", fmt.Sprintf("%q", p[:n]))
		b.chunk++
	}
	if err == io.EOF {
		b.complete = true
	}
	return n, err
}

func (b *loggedResponseBody) Close() error {
	b.logger.Info("llm response end", "id", b.id, "complete", b.complete)
	return b.ReadCloser.Close()
}

// dropUntrustedRequestHeaders removes what the caller must not choose. ReverseProxy has already
// removed hop-by-hop headers and `Connection`-named ones.
func dropUntrustedRequestHeaders(headers http.Header, attribution attributionConfig) {
	for name := range headers {
		lower := strings.ToLower(name)
		_, credential := credentialHeaders[lower]
		_, untrusted := untrustedMetadataHeaders[lower]
		if credential || untrusted || hasIdentityHeaderPrefix(lower) || lower == auth.DelegationMetadataKey ||
			lower == "host" || lower == "content-length" || lower == "baggage" {
			delete(headers, name)
		}
	}
	for _, header := range attribution.JSONHeaders {
		headers.Del(header.Name)
	}
}

// subjectClaims are the verified claims a caller has. A ServiceAccount caller has no identity
// claims and an Identity caller no namespace or service_account claim, so neither is ever
// attributed with the other's empty fields.
func subjectClaims(subject auth.Subject) map[verifiedClaim]string {
	switch subject := subject.(type) {
	case auth.Principal:
		return map[verifiedClaim]string{
			verifiedNamespace:      subject.Account.Namespace,
			verifiedServiceAccount: subject.Account.Name,
			verifiedPodName:        subject.PodName,
			verifiedPodUID:         subject.PodUID,
		}
	case auth.IdentityPrincipal:
		return map[verifiedClaim]string{
			verifiedIdentity:    subject.Identity.Name,
			verifiedIdentityUID: subject.Identity.UID,
			verifiedPodName:     subject.PodName,
			verifiedPodUID:      subject.PodUID,
		}
	default:
		panic(fmt.Sprintf("unknown subject %T", subject))
	}
}

// verifiedRequestHeaders are the headers LiteLLM trusts: the server-held key and attribution
// derived from the admitted caller's verified claims, plus the central Session (sessionID != "")
// an authorized sampler claimed or delegated for. A field whose claim the caller lacks is omitted.
func verifiedRequestHeaders(
	claims map[verifiedClaim]string,
	key string,
	attribution attributionConfig,
	sessionID string,
) (http.Header, error) {
	forwarded := make(http.Header)
	forwarded.Set("Authorization", "Bearer "+key)
	for _, header := range attribution.JSONHeaders {
		members := make(map[string]string, len(header.Fields))
		for _, field := range header.Fields {
			if value, ok := claims[field.Source]; ok {
				members[field.Key] = value
			}
		}
		metadata, err := json.Marshal(members)
		if err != nil {
			return nil, err
		}
		forwarded.Set(header.Name, string(metadata))
	}
	if len(attribution.Baggage) > 0 {
		value, err := agentplaneIdentityBaggage(attribution.Baggage, claims)
		if err != nil {
			return nil, err
		}
		forwarded.Set("baggage", value)
	}
	if sessionID == "" {
		return forwarded, nil
	}
	// This claim is supplied by an explicitly trusted sampler, not inferred from
	// its shared Pod. Existing workload attribution stays bound to TokenReview.
	members := map[string]string{}
	if value := forwarded.Get("x-litellm-spend-logs-metadata"); value != "" {
		if err := json.Unmarshal([]byte(value), &members); err != nil {
			return nil, err
		}
	}
	members["agentplane.session_id"] = sessionID
	encoded, err := json.Marshal(members)
	if err != nil {
		return nil, err
	}
	forwarded.Set("x-litellm-spend-logs-metadata", string(encoded))
	// LiteLLM creates its pre-call span before spend-log metadata promotion.
	// Extend only the freshly verified baggage; caller baggage was stripped.
	verifiedBaggage, err := baggage.Parse(forwarded.Get("baggage"))
	if err != nil {
		return nil, err
	}
	member, err := baggage.NewMemberRaw("litellm.metadata.spend_logs_metadata.agentplane.session_id", sessionID)
	if err != nil {
		return nil, err
	}
	verifiedBaggage, err = verifiedBaggage.SetMember(member)
	if err != nil {
		return nil, err
	}
	forwarded.Set("baggage", verifiedBaggage.String())
	return forwarded, nil
}

func agentplaneIdentityBaggage(fields []attributionField, claims map[verifiedClaim]string) (string, error) {
	// Baggage is derived only from the authenticated subject.
	outgoing := baggage.Baggage{}
	for _, field := range fields {
		value, ok := claims[field.Source]
		if !ok {
			continue
		}
		member, err := baggage.NewMemberRaw(field.Key, value)
		if err != nil {
			return "", err
		}
		outgoing, err = outgoing.SetMember(member)
		if err != nil {
			return "", err
		}
	}
	return outgoing.String(), nil
}

func hasIdentityHeaderPrefix(name string) bool {
	return slices.ContainsFunc(identityHeaderPrefixes, func(prefix string) bool {
		return strings.HasPrefix(name, prefix)
	})
}

func writeJSONError(writer http.ResponseWriter, status int, detail, authenticate string) {
	writer.Header().Set("Content-Type", "application/json")
	if authenticate != "" {
		writer.Header().Set("WWW-Authenticate", authenticate)
	}
	writer.WriteHeader(status)
	_, _ = fmt.Fprintf(writer, `{"detail":%q}`, detail)
}
