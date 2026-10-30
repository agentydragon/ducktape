// Package bridge adapts Agentplane's egress decision to iron-proxy's external TransformService.
// It is an experimental integration seam, not a production proxy replacement: projected-token
// review, DNS pinning/private-address checks, and tunnel-handle cleanup are not implemented here.
package bridge

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/textproto"
	"net/url"
	"strconv"
	"strings"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	transformv1 "github.com/agentydragon/ducktape/agentplane/egress/x/iron_proxy/bridge/proto"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

const (
	// TransformName identifies this bridge's CONNECT trace when iron-proxy passes it to inner requests.
	TransformName   = "agentplane-egress"
	tunnelHandleKey = "tunnel_handle"

	proxyAuthorizationHeader = "Proxy-Authorization"
	egressReasonHeader       = "X-Agentplane-Egress"
)

// Options are the Agentplane authorities the adapter delegates to. Ready can add a component-local
// readiness gate; Policy.Health.Available remains mandatory regardless of that callback.
type Options struct {
	Identity *auth.Resolver
	Policy   *policy.Index
	Ready    func() bool
}

// Bridge implements iron-proxy's unary request/response transform service.
//
// Tunnel state deliberately stays in-process. Only an opaque random handle is returned in the
// CONNECT transform annotation; this prototype has no expiration or cleanup hook for those entries.
type Bridge struct {
	transformv1.UnimplementedTransformServiceServer
	identity *auth.Resolver
	policy   *policy.Index
	ready    func() bool

	mu      sync.Mutex
	tunnels map[string]tunnelState
}

type tunnelState struct {
	token     string
	principal auth.Principal
	target    string
}

func (tunnelState) String() string   { return "tunnelState{redacted}" }
func (tunnelState) GoString() string { return "tunnelState{redacted}" }

// New returns a bridge that uses the supplied live resolver and policy index. It does not copy or
// reinterpret Agentplane's matcher.
func New(options Options) (*Bridge, error) {
	if options.Identity == nil || options.Policy == nil {
		return nil, errors.New("identity resolver and policy index are required")
	}
	return &Bridge{
		identity: options.Identity,
		policy:   options.Policy,
		ready:    options.Ready,
		tunnels:  make(map[string]tunnelState),
	}, nil
}

// TransformRequest authenticates each request, invokes Agentplane's existing policy evaluator, and
// returns only the existing presentation rewrites. The CONNECT request creates a server-side
// identity entry and passes its opaque handle through iron-proxy's per-transform tunnel annotation.
func (b *Bridge) TransformRequest(ctx context.Context, request *transformv1.TransformRequestRequest) (*transformv1.TransformRequestResponse, error) {
	if request == nil || request.GetRequest() == nil {
		return reject(http.StatusBadRequest, decision.NoRule), nil
	}
	if !b.available() {
		return reject(http.StatusServiceUnavailable, decision.Unavailable), nil
	}

	input := request.GetRequest()
	headers, err := headersFromProto(input.GetHeaders())
	if err != nil {
		return reject(http.StatusBadRequest, decision.NoRule), nil
	}
	connect := input.GetMethod() == http.MethodConnect
	_, suppliedProxyAuthorization := headers[textproto.CanonicalMIMEHeaderKey(proxyAuthorizationHeader)]
	policyRequest, target, err := requestForPolicy(request.GetContext(), input, headers)
	if err != nil {
		return reject(http.StatusBadRequest, decision.NoRule), nil
	}

	var previous *tunnelState
	var handle string
	if !connect && request.GetContext().GetTunnel() != nil {
		var ok bool
		handle, ok = tunnelHandle(request.GetContext().GetTunnel())
		if !ok {
			return reject(http.StatusForbidden, decision.TokenMissing), nil
		}
		state, ok := b.tunnel(handle)
		if !ok || canonicalTarget(request.GetContext().GetTunnel().GetTarget()) != state.target {
			return reject(http.StatusForbidden, decision.TokenMissing), nil
		}
		previous = &state
	}

	state, reason, statusCode := b.authenticate(ctx, headers, previous)
	if reason != "" {
		if handle != "" {
			// Match the HTTP proxy's replacement behavior: a rejected supplied token or a revoked
			// inherited token cannot leave the old tunnel identity available for a later request.
			b.deleteTunnel(handle)
		}
		return reject(statusCode, reason), nil
	}
	privateHeadersRemoved := stripPrivateHeaders(headers)
	if handle != "" && suppliedProxyAuthorization {
		b.storeTunnel(handle, state)
	}
	if !b.available() {
		return reject(http.StatusServiceUnavailable, decision.Unavailable), nil
	}

	caller := policyapi.ServiceAccountRef{
		Namespace: state.principal.Account.Namespace,
		Name:      state.principal.Account.Name,
	}
	workload := &policy.WorkloadContext{
		Bearer: state.token,
		Caller: caller,
		PodUID: state.principal.PodUID,
		// Projected audience tokens are intentionally unsupported by this bridge prototype.
	}
	verdict := b.policy.Evaluate(caller, policyRequest, workload)
	allowed, ok := verdict.(policy.Allowed)
	if !ok {
		return reject(policyStatus(verdict), deniedReason(verdict)), nil
	}
	if !b.available() {
		return reject(http.StatusServiceUnavailable, decision.Unavailable), nil
	}

	response := &transformv1.TransformRequestResponse{
		Action: transformv1.TransformAction_TRANSFORM_ACTION_CONTINUE,
	}
	changed := privateHeadersRemoved
	for _, rewrite := range allowed.Rewrites {
		rewrite.Apply(headers)
		changed = true
	}
	var body []byte
	if allowed.BodyRewrite != nil {
		body = allowed.BodyRewrite.Content
		changed = true
	}
	if changed {
		response.ModifiedRequest = copyRequest(input, headers, body, allowed.BodyRewrite != nil)
	}

	if connect {
		handle, err := randomHandle()
		if err != nil {
			return reject(http.StatusServiceUnavailable, decision.Unavailable), nil
		}
		state.target = target
		b.storeTunnel(handle, state)
		response.Annotations = map[string]string{tunnelHandleKey: handle}
	}
	return response, nil
}

// TransformResponse is an explicit pass-through; this adapter changes only admitted requests.
func (b *Bridge) TransformResponse(_ context.Context, _ *transformv1.TransformResponseRequest) (*transformv1.TransformResponseResponse, error) {
	return &transformv1.TransformResponseResponse{
		Action: transformv1.TransformAction_TRANSFORM_ACTION_CONTINUE,
	}, nil
}

func (b *Bridge) available() bool {
	if b.ready != nil && !b.ready() {
		return false
	}
	return b.policy.Health.Available()
}

func (b *Bridge) authenticate(ctx context.Context, headers http.Header, previous *tunnelState) (tunnelState, decision.Reason, int) {
	values, supplied := headers[textproto.CanonicalMIMEHeaderKey(proxyAuthorizationHeader)]
	token := ""
	if supplied {
		var ok bool
		token, ok = identity.BearerToken(values)
		if !ok {
			return tunnelState{}, decision.TokenRejected, http.StatusForbidden
		}
	} else if previous != nil {
		token = previous.token
	} else {
		return tunnelState{}, decision.TokenMissing, http.StatusForbidden
	}

	principal, err := b.identity.ResolveToken(ctx, token)
	if err != nil {
		if errors.Is(err, auth.ErrInvalidBearer) {
			return tunnelState{}, decision.TokenRejected, http.StatusForbidden
		}
		return tunnelState{}, decision.Unavailable, http.StatusBadGateway
	}
	if previous != nil && principal != previous.principal {
		return tunnelState{}, decision.PodMismatch, http.StatusForbidden
	}

	state := tunnelState{token: token, principal: principal}
	if previous != nil {
		state.target = previous.target
	}
	return state, "", 0
}

func requestForPolicy(context *transformv1.TransformContext, request *transformv1.HttpRequest, headers http.Header) (policy.Request, string, error) {
	if request.GetMethod() == "" {
		return policy.Request{}, "", errors.New("method is required")
	}
	parsedURL, err := url.Parse(request.GetUrl())
	if err != nil {
		return policy.Request{}, "", err
	}
	scheme := parsedURL.Scheme
	if scheme == "" && context.GetSni() != "" {
		scheme = "https"
	}
	if scheme == "" {
		scheme = "http"
	}
	defaultPort := "80"
	if scheme == "https" {
		defaultPort = "443"
	}
	if scheme != "http" && scheme != "https" && request.GetMethod() != http.MethodConnect {
		return policy.Request{}, "", errors.New("unsupported request scheme")
	}

	authority := request.GetHost()
	if request.GetMethod() == http.MethodConnect {
		if authority == "" {
			authority = parsedURL.Host
		}
		defaultPort = "443"
	} else if authority == "" {
		authority = parsedURL.Host
	}
	if authority == "" {
		return policy.Request{}, "", errors.New("request authority is required")
	}
	host, canonicalTarget, err := parseAuthority(authority, defaultPort)
	if err != nil {
		return policy.Request{}, "", err
	}

	result := policy.Request{
		Method:  request.GetMethod(),
		Host:    host,
		Headers: headers,
		Body:    request.GetBody(),
	}
	if request.GetMethod() != http.MethodConnect {
		result.Path = parsedURL.RequestURI()
	}
	return result, canonicalTarget, nil
}

func parseAuthority(authority, defaultPort string) (host, canonical string, err error) {
	var port string
	switch {
	case strings.HasPrefix(authority, "[") && strings.HasSuffix(authority, "]"):
		host = strings.TrimSuffix(strings.TrimPrefix(authority, "["), "]")
		if net.ParseIP(host) == nil {
			return "", "", fmt.Errorf("invalid bracketed host")
		}
		port = defaultPort
	case strings.Count(authority, ":") == 0:
		host, port = authority, defaultPort
	default:
		host, port, err = net.SplitHostPort(authority)
		if err != nil {
			return "", "", err
		}
	}
	if host == "" {
		return "", "", errors.New("host is required")
	}
	portNumber, err := strconv.ParseUint(port, 10, 16)
	if err != nil || portNumber == 0 {
		return "", "", errors.New("invalid port")
	}
	return host, net.JoinHostPort(strings.ToLower(host), strconv.FormatUint(portNumber, 10)), nil
}

func canonicalTarget(target string) string {
	_, canonical, err := parseAuthority(target, "443")
	if err != nil {
		return ""
	}
	return canonical
}

func headersFromProto(input map[string]*transformv1.HeaderValues) (http.Header, error) {
	headers := make(http.Header, len(input))
	for key, values := range input {
		canonical := textproto.CanonicalMIMEHeaderKey(key)
		if canonical == "" {
			return nil, fmt.Errorf("invalid header name")
		}
		if _, exists := headers[canonical]; exists {
			return nil, fmt.Errorf("duplicate header name after canonicalization")
		}
		if values != nil {
			headers[canonical] = append([]string(nil), values.GetValues()...)
		}
	}
	return headers, nil
}

func stripPrivateHeaders(headers http.Header) bool {
	changed := false
	for _, name := range []string{proxyAuthorizationHeader, identity.ProjectedTokenHeader} {
		canonical := textproto.CanonicalMIMEHeaderKey(name)
		if _, ok := headers[canonical]; ok {
			// iron-proxy applies a modified header map only when it is non-empty. Keep a
			// zero-value entry so clearing the final private header is distinguishable from
			// returning no header modification at all.
			headers[canonical] = nil
			changed = true
		}
	}
	return changed
}

func copyRequest(input *transformv1.HttpRequest, headers http.Header, body []byte, replaceBody bool) *transformv1.HttpRequest {
	modified := &transformv1.HttpRequest{
		Method:     input.GetMethod(),
		Url:        input.GetUrl(),
		Headers:    headersToProto(headers),
		Host:       input.GetHost(),
		RemoteAddr: input.GetRemoteAddr(),
	}
	if replaceBody {
		modified.Body = append([]byte(nil), body...)
	} else if input.GetBody() != nil {
		modified.Body = append([]byte(nil), input.GetBody()...)
	}
	return modified
}

func headersToProto(headers http.Header) map[string]*transformv1.HeaderValues {
	if len(headers) == 0 {
		return nil
	}
	result := make(map[string]*transformv1.HeaderValues, len(headers))
	for name, values := range headers {
		result[name] = &transformv1.HeaderValues{Values: append([]string(nil), values...)}
	}
	return result
}

func tunnelHandle(tunnel *transformv1.TunnelInfo) (string, bool) {
	var handle string
	found := false
	for _, trace := range tunnel.GetRequestTransforms() {
		if trace.GetName() != TransformName {
			continue
		}
		value := trace.GetAnnotations().GetFields()[tunnelHandleKey]
		if value == nil || value.GetStringValue() == "" || (found && handle != value.GetStringValue()) {
			return "", false
		}
		handle, found = value.GetStringValue(), true
	}
	return handle, found
}

func (b *Bridge) tunnel(handle string) (tunnelState, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	state, ok := b.tunnels[handle]
	return state, ok
}

func (b *Bridge) storeTunnel(handle string, state tunnelState) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.tunnels[handle] = state
}

func (b *Bridge) deleteTunnel(handle string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.tunnels, handle)
}

func randomHandle() (string, error) {
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw[:]), nil
}

func reject(statusCode int, reason decision.Reason) *transformv1.TransformRequestResponse {
	body := []byte(http.StatusText(statusCode) + "\n")
	return &transformv1.TransformRequestResponse{
		Action: transformv1.TransformAction_TRANSFORM_ACTION_REJECT,
		Response: &transformv1.HttpResponse{
			StatusCode: int32(statusCode),
			Headers: map[string]*transformv1.HeaderValues{
				"Content-Type":     {Values: []string{"text/plain; charset=utf-8"}},
				egressReasonHeader: {Values: []string{"denied; reason=" + string(reason)}},
			},
			Body: body,
		},
	}
}

func deniedReason(verdict policy.Decision) decision.Reason {
	if denied, ok := verdict.(policy.Denied); ok {
		return denied.Reason
	}
	return decision.Unavailable
}

func policyStatus(verdict policy.Decision) int {
	switch deniedReason(verdict) {
	case decision.Unavailable, decision.HostUnresolved:
		return http.StatusBadGateway
	default:
		return http.StatusForbidden
	}
}

var _ transformv1.TransformServiceServer = (*Bridge)(nil)
