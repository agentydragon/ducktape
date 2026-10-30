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
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/baggage"
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
	resolver     *auth.Resolver
	transport    *http.Transport
	roundTripper http.RoundTripper
	baseURL      *url.URL
	key          string
	models       map[string]modelConfig
	attribution  attributionConfig
	codex        *codexCatalog
	logBody      bool
	logger       *slog.Logger
	pool         chan struct{}

	centralSamplerAccounts []string
}

func newIngressHandler(c config, resolver *auth.Resolver, logger *slog.Logger) (*ingressHandler, error) {
	baseURL, err := url.Parse(c.LiteLLMURL)
	if err != nil {
		return nil, errors.New("invalid LiteLLM URL")
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
	models := make(map[string]modelConfig, len(c.Models))
	for _, item := range c.Models {
		models[item.Model] = item
	}
	var codex *codexCatalog
	if c.CodexModelCatalogFile != "" {
		if codex, err = loadCodexCatalog(c.CodexModelCatalogFile); err != nil {
			return nil, err
		}
	}
	return &ingressHandler{
		resolver:     resolver,
		transport:    transport,
		roundTripper: observability.HTTPTransport(transport),
		baseURL:      baseURL,
		key:          c.LiteLLMKey,
		models:       models,
		attribution:  c.Attribution,
		codex:        codex,
		logBody:      c.LogLLMRequests,
		logger:       logger,
		pool:         make(chan struct{}, 100),

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
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(writer, `{"status":"ok"}`)
		return
	}
	if !allowedMethod(request.Method) {
		writer.Header().Set("Allow", strings.Join(proxyMethods, ", "))
		http.Error(writer, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}
	principal, okay := h.authenticate(writer, request)
	if !okay {
		return
	}
	if request.URL.Path == "/agentplane/model-config" && request.Method == http.MethodGet {
		query := request.URL.Query()
		if !query.Has("model") {
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = io.WriteString(writer, `{"detail":[{"type":"missing","loc":["query","model"],"msg":"Field required","input":null}]}`)
			return
		}
		model := h.models[query.Get("model")]
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusOK)
		if model.Model == "" {
			_, _ = io.WriteString(writer, "null")
			return
		}
		_ = json.NewEncoder(writer).Encode(model)
		return
	}
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
	h.forward(writer, request, principal)
}

func allowedMethod(method string) bool {
	for _, allowed := range proxyMethods {
		if method == allowed {
			return true
		}
	}
	return false
}

func (h *ingressHandler) authenticate(writer http.ResponseWriter, request *http.Request) (auth.Principal, bool) {
	values := request.Header.Values("Authorization")
	if len(values) != 1 {
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
		return auth.Principal{}, false
	}
	match := bearerPattern.FindStringSubmatch(values[0])
	if match == nil {
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
		return auth.Principal{}, false
	}
	// Normalize the accepted HTTP bearer syntax before applying the shared
	// TokenReview policy.
	principal, err := h.resolver.ResolveAuthorization(request.Context(), []string{"Bearer " + match[1]})
	if errors.Is(err, auth.ErrInvalidBearer) {
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
		return auth.Principal{}, false
	}
	if err != nil {
		writeJSONError(writer, http.StatusInternalServerError, "workload authentication unavailable", "")
		return auth.Principal{}, false
	}
	return principal, true
}

func (h *ingressHandler) forward(writer http.ResponseWriter, incoming *http.Request, principal auth.Principal) {
	select {
	case h.pool <- struct{}{}:
		defer func() { <-h.pool }()
	case <-incoming.Context().Done():
		return
	case <-time.After(5 * time.Second):
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}

	sessionID := incoming.Header.Get("x-agentplane-session-id")
	if sessionID != "" {
		if !slices.Contains(h.centralSamplerAccounts, principal.Account.Namespace+"/"+principal.Account.Name) {
			writeJSONError(writer, http.StatusForbidden, "Session attribution requires an authorized central sampler", "")
			return
		}
		parsed, err := uuid.Parse(sessionID)
		if err != nil || parsed.String() != sessionID {
			writeJSONError(writer, http.StatusBadRequest, "Session attribution requires a canonical UUID", "")
			return
		}
	}
	verified, err := verifiedRequestHeaders(principal, h.key, h.attribution, sessionID)
	if err != nil {
		writeJSONError(writer, http.StatusInternalServerError, "Could not attach verified attribution", "")
		return
	}

	requestID := rand.Text()
	var requestBody *boundedCapture
	proxy := &httputil.ReverseProxy{
		Rewrite: func(out *httputil.ProxyRequest) {
			out.SetURL(h.baseURL)
			dropUntrustedRequestHeaders(out.Out.Header, h.attribution)
			for name, values := range verified {
				out.Out.Header[name] = values
			}
			if h.logBody {
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
		ErrorHandler: func(writer http.ResponseWriter, request *http.Request, _ error) {
			if request.Context().Err() == nil {
				writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
			}
		},
	}
	if h.logBody {
		defer func() {
			if requestBody == nil {
				return
			}
			h.logger.Info("llm request", "id", requestID, "namespace", principal.Account.Namespace,
				"pod", principal.PodName, "pod_uid", principal.PodUID, "method", incoming.Method,
				"path", incoming.URL.Path, "body", fmt.Sprintf("%q", requestBody.captured.Bytes()),
				"body_bytes", requestBody.total, "body_truncated", requestBody.total > requestBody.captured.Len())
		}()
	}
	proxy.ServeHTTP(writer, incoming)
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
		if credential || untrusted || hasIdentityHeaderPrefix(lower) ||
			lower == "host" || lower == "content-length" || lower == "baggage" {
			delete(headers, name)
		}
	}
	for _, header := range attribution.JSONHeaders {
		headers.Del(header.Name)
	}
}

// verifiedRequestHeaders are the headers LiteLLM trusts: the server-held key and attribution
// derived from the authenticated principal, plus the Session claim of an authorized central
// sampler (sessionID != "").
func verifiedRequestHeaders(
	principal auth.Principal,
	key string,
	attribution attributionConfig,
	sessionID string,
) (http.Header, error) {
	forwarded := make(http.Header)
	forwarded.Set("Authorization", "Bearer "+key)
	claims := map[verifiedClaim]string{
		verifiedNamespace:      principal.Account.Namespace,
		verifiedServiceAccount: principal.Account.Name,
		verifiedPodName:        principal.PodName,
		verifiedPodUID:         principal.PodUID,
	}
	for _, header := range attribution.JSONHeaders {
		members := make(map[string]string, len(header.Fields))
		for _, field := range header.Fields {
			members[field.Key] = claims[field.Source]
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
	// Baggage is derived only from the authenticated Kubernetes principal.
	outgoing := baggage.Baggage{}
	for _, field := range fields {
		member, err := baggage.NewMemberRaw(field.Key, claims[field.Source])
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
	for _, prefix := range identityHeaderPrefixes {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}

func writeJSONError(writer http.ResponseWriter, status int, detail, authenticate string) {
	writer.Header().Set("Content-Type", "application/json")
	if authenticate != "" {
		writer.Header().Set("WWW-Authenticate", authenticate)
	}
	writer.WriteHeader(status)
	_, _ = fmt.Fprintf(writer, `{"detail":%q}`, detail)
}
