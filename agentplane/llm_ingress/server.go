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
	"net/url"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/llm_ingress/configpb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/baggage"
)

var (
	bearerPattern = regexp.MustCompile(`(?i)^Bearer +([A-Za-z0-9._~+/=-]+)$`)
	hopHeaders    = map[string]struct{}{
		"connection": {}, "keep-alive": {}, "proxy-authenticate": {}, "proxy-authorization": {},
		"te": {}, "trailer": {}, "transfer-encoding": {}, "upgrade": {},
	}
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
	resolver    *auth.Resolver
	backend     *http.Client
	baseURL     *url.URL
	key         string
	models      map[string]*configpb.ModelConfig
	attribution *configpb.AttributionConfig
	codex       *codexCatalog
	logBody     bool
	logger      *slog.Logger
	pool        chan struct{}

	centralSamplerAccounts []string
}

func newIngressHandler(c *configpb.Config, resolver *auth.Resolver, logger *slog.Logger) (*ingressHandler, error) {
	baseURL, err := url.Parse(c.LitellmUrl)
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
	backend := &http.Client{
		Transport: observability.HTTPTransport(transport),
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	models := make(map[string]*configpb.ModelConfig, len(c.Models))
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
		resolver:    resolver,
		backend:     backend,
		baseURL:     baseURL,
		key:         c.LitellmKey,
		models:      models,
		attribution: c.Attribution,
		codex:       codex,
		logBody:     c.GetLogLlmRequests(),
		logger:      logger,
		pool:        make(chan struct{}, 100),

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
		if model == nil {
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

	body, err := io.ReadAll(incoming.Body)
	if err != nil {
		if incoming.Context().Err() == nil {
			writeJSONError(writer, http.StatusBadRequest, "request body unavailable", "")
		}
		return
	}
	requestID := rand.Text()
	if h.logBody {
		h.logger.Info("llm request", "id", requestID, "namespace", principal.Account.Namespace,
			"pod", principal.PodName, "pod_uid", principal.PodUID, "method", incoming.Method,
			"path", incoming.URL.Path, "body", fmt.Sprintf("%q", body))
	}
	forwardURL := *h.baseURL
	basePath := strings.TrimSuffix(h.baseURL.EscapedPath(), "/")
	requestPath := strings.TrimLeft(incoming.URL.EscapedPath(), "/")
	rawPath := basePath + "/" + requestPath
	path, err := url.PathUnescape(rawPath)
	if err != nil {
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}
	forwardURL.Path = path
	forwardURL.RawPath = ""
	if path != rawPath {
		forwardURL.RawPath = rawPath
	}
	forwardURL.RawQuery = incoming.URL.RawQuery
	forwardURL.ForceQuery = incoming.URL.ForceQuery
	outgoing, err := http.NewRequestWithContext(incoming.Context(), incoming.Method, forwardURL.String(), bytes.NewReader(body))
	if err != nil {
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}
	outgoing.Header, err = forwardedRequestHeaders(incoming.Header, principal, h.key, h.attribution)
	if err != nil {
		writeJSONError(writer, http.StatusInternalServerError, "Could not attach verified attribution", "")
		return
	}
	if sessionID != "" {
		// This claim is supplied by an explicitly trusted sampler, not inferred from
		// its shared Pod. Existing workload attribution stays bound to TokenReview.
		members := map[string]string{}
		if value := outgoing.Header.Get("x-litellm-spend-logs-metadata"); value != "" {
			if err := json.Unmarshal([]byte(value), &members); err != nil {
				writeJSONError(writer, http.StatusInternalServerError, "Invalid attribution metadata", "")
				return
			}
		}
		members["agentplane.session_id"] = sessionID
		encoded, _ := json.Marshal(members)
		outgoing.Header.Set("x-litellm-spend-logs-metadata", string(encoded))
		// LiteLLM creates its pre-call span before spend-log metadata promotion.
		// Extend only the freshly verified baggage; caller baggage was stripped.
		verifiedBaggage, err := baggage.Parse(outgoing.Header.Get("baggage"))
		if err != nil {
			writeJSONError(writer, http.StatusInternalServerError, "Invalid attribution baggage", "")
			return
		}
		member, err := baggage.NewMemberRaw("litellm.metadata.spend_logs_metadata.agentplane.session_id", sessionID)
		if err != nil {
			writeJSONError(writer, http.StatusInternalServerError, "Invalid Session attribution", "")
			return
		}
		verifiedBaggage, err = verifiedBaggage.SetMember(member)
		if err != nil {
			writeJSONError(writer, http.StatusInternalServerError, "Could not attach Session attribution", "")
			return
		}
		outgoing.Header.Set("baggage", verifiedBaggage.String())
	}

	response, err := h.backend.Do(outgoing)
	if err != nil {
		if incoming.Context().Err() == nil {
			writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		}
		return
	}
	defer response.Body.Close()
	if h.logBody {
		h.logger.Info("llm response start", "id", requestID, "status", response.StatusCode,
			"content_type", response.Header.Get("Content-Type"))
	}
	copyResponseHeaders(writer.Header(), response.Header)
	if h.codex != nil && incoming.URL.Path == "/v1/responses" {
		writer.Header().Set("X-Models-Etag", h.codex.etag)
	}
	writer.WriteHeader(response.StatusCode)
	if err := http.NewResponseController(writer).Flush(); err != nil && !errors.Is(err, http.ErrNotSupported) {
		return
	}
	complete := false
	defer func() {
		if h.logBody {
			h.logger.Info("llm response end", "id", requestID, "complete", complete)
		}
	}()
	buffer := make([]byte, 32*1024)
	chunk := 0
	for {
		count, readErr := response.Body.Read(buffer)
		if count > 0 {
			part := buffer[:count]
			if h.logBody {
				h.logger.Info("llm response chunk", "id", requestID, "status", response.StatusCode,
					"chunk", chunk, "body", fmt.Sprintf("%q", part))
			}
			chunk++
			if _, err := writer.Write(part); err != nil {
				return
			}
			if err := http.NewResponseController(writer).Flush(); err != nil && !errors.Is(err, http.ErrNotSupported) {
				return
			}
		}
		if readErr == io.EOF {
			complete = true
			return
		}
		if readErr != nil {
			panic(http.ErrAbortHandler)
		}
	}
}

func forwardedRequestHeaders(
	headers http.Header,
	principal auth.Principal,
	key string,
	attribution *configpb.AttributionConfig,
) (http.Header, error) {
	connectionTokens := connectionHeaderTokens(headers)
	configuredHeaders := make(map[string]struct{}, len(attribution.JsonHeaders))
	for _, header := range attribution.JsonHeaders {
		configuredHeaders[strings.ToLower(header.Name)] = struct{}{}
	}
	forwarded := make(http.Header)
	for name, values := range headers {
		lower := strings.ToLower(name)
		if _, found := hopHeaders[lower]; found {
			continue
		}
		if _, found := connectionTokens[lower]; found {
			continue
		}
		if _, found := credentialHeaders[lower]; found {
			continue
		}
		if _, found := configuredHeaders[lower]; found {
			continue
		}
		if _, found := untrustedMetadataHeaders[lower]; found || lower == "host" || lower == "content-length" || lower == "baggage" {
			continue
		}
		if hasIdentityHeaderPrefix(lower) {
			continue
		}
		forwarded[name] = append([]string(nil), values...)
	}
	forwarded.Set("Authorization", "Bearer "+key)
	claims := map[configpb.VerifiedClaim]string{
		configpb.VerifiedClaim_namespace:       principal.Account.Namespace,
		configpb.VerifiedClaim_service_account: principal.Account.Name,
		configpb.VerifiedClaim_pod_name:        principal.PodName,
		configpb.VerifiedClaim_pod_uid:         principal.PodUID,
	}
	for _, header := range attribution.JsonHeaders {
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
	return forwarded, nil
}

func agentplaneIdentityBaggage(fields []*configpb.AttributionField, claims map[configpb.VerifiedClaim]string) (string, error) {
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

func copyResponseHeaders(destination, source http.Header) {
	connectionTokens := connectionHeaderTokens(source)
	for name, values := range source {
		lower := strings.ToLower(name)
		if _, found := hopHeaders[lower]; found {
			continue
		}
		if _, found := connectionTokens[lower]; found {
			continue
		}
		if _, found := credentialHeaders[lower]; found || lower == "content-length" {
			continue
		}
		for _, value := range values {
			destination.Add(name, value)
		}
	}
}

func connectionHeaderTokens(headers http.Header) map[string]struct{} {
	tokens := make(map[string]struct{})
	for _, value := range headers.Values("Connection") {
		for _, token := range strings.Split(value, ",") {
			if token = strings.TrimSpace(token); token != "" {
				tokens[strings.ToLower(token)] = struct{}{}
			}
		}
	}
	return tokens
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
