// Package goproxyspike evaluates GoProxy's central egress transport hooks. It is not a
// replacement service: the caller supplies bounded-fresh shared policy snapshots and
// whole-value header credentials; unsupported policy shapes fail closed.
package goproxyspike

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"sync"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"github.com/elazarl/goproxy"
	"golang.org/x/net/http2"
	"google.golang.org/grpc/metadata"
)

// HeaderCredential intentionally supports only wholeValue targets in this spike.
// It is fixture configuration, not a second EgressCredential CR definition.
type HeaderCredential struct {
	Header string
	Value  string
}

type Snapshot struct {
	Sets        []policyapi.PolicySet
	Bindings    []policyapi.PolicyBinding
	Credentials map[string]HeaderCredential
	FreshUntil  time.Time
}

// Options requires explicit trust roots, interception identity, policy and auth.
// Resolve is a dependency seam: production callers would use net.DefaultResolver.LookupNetIP.
type Options struct {
	CA           tls.Certificate
	Roots        *x509.CertPool
	Auth         *auth.Resolver
	ReadSnapshot func() (Snapshot, error)
	Resolve      func(context.Context, string, string) ([]netip.Addr, error)
	Exempt       []netip.Prefix
}

type tunnel struct {
	mu        sync.Mutex
	bearer    string
	principal auth.Principal
}

func New(options Options) (*goproxy.ProxyHttpServer, error) {
	if len(options.CA.Certificate) == 0 || options.CA.PrivateKey == nil || options.Roots == nil || options.Auth == nil || options.ReadSnapshot == nil || options.Resolve == nil {
		return nil, errors.New("CA, upstream roots, authentication, snapshot source and resolver are required")
	}
	proxy := goproxy.NewProxyHttpServer()
	proxy.AllowHTTP2 = true
	// Never retain GoProxy's insecure default transport or environment proxy dialer.
	proxy.Tr = &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: options.Roots, MinVersion: tls.VersionTLS12}, Proxy: nil,
		DialContext: func(context.Context, string, string) (net.Conn, error) { return nil, errors.New("unadmitted dial") },
	}
	proxy.ConnectDial = func(string, string) (net.Conn, error) { return nil, errors.New("raw CONNECT disabled") }
	proxy.Logger = log.New(io.Discard, "", 0) // upstream errors can contain private URLs
	mitm := &goproxy.ConnectAction{Action: goproxy.ConnectMitm, TLSConfig: goproxy.TLSConfigFromCA(&options.CA)}
	proxy.OnRequest().HandleConnectFunc(func(host string, ctx *goproxy.ProxyCtx) (*goproxy.ConnectAction, string) {
		identity := &tunnel{}
		ctx.UserData = identity
		principal, err := identify(ctx.Req, identity, options.Auth)
		if err != nil {
			ctx.Resp = denied(ctx.Req, "token-rejected")
			return goproxy.RejectConnect, host
		}
		req := ctx.Req.Clone(ctx.Req.Context())
		req.URL.Scheme = "https"
		req.URL.Host = host
		if _, _, err := admit(req, principal, options.ReadSnapshot, true); err != nil {
			ctx.Resp = denied(ctx.Req, "no-rule")
			return goproxy.RejectConnect, host
		}
		// ConnectMitm acknowledges without contacting the upstream. Inner admission
		// installs the only usable RoundTripper, so a denied path cannot leak a dial.
		return mitm, host
	})
	proxy.OnRequest().DoFunc(func(req *http.Request, ctx *goproxy.ProxyCtx) (*http.Request, *http.Response) {
		identity, ok := ctx.UserData.(*tunnel)
		if !ok {
			identity = &tunnel{}
		}
		principal, err := identify(req, identity, options.Auth)
		// Always consume both hop headers, including on denial.
		req.Header.Del("Proxy-Authorization")
		projected := req.Header.Values("X-Agentplane-Workload-Token")
		req.Header.Del("X-Agentplane-Workload-Token")
		if err != nil {
			return req, denied(req, "token-rejected")
		}
		// Projected token substitution is deliberately outside this spike's subset.
		if len(projected) != 0 {
			return req, denied(req, "credential-unavailable")
		}
		rule, snapshot, err := admit(req, principal, options.ReadSnapshot, false)
		if err != nil {
			return req, denied(req, "no-rule")
		}
		if err := substitute(req, rule, snapshot.Credentials); err != nil {
			return req, denied(req, "placeholder-unresolved")
		}
		address, err := pin(req.Context(), req.URL.Hostname(), rule.ClusterInternal, options)
		if err != nil {
			return req, denied(req, "address-forbidden")
		}
		// Recheck after asynchronous DNS and before constructing a dial-capable transport.
		current, latest, err := admit(req, principal, options.ReadSnapshot, false)
		if err != nil || !sameRule(rule, current) || !sameCredentials(snapshot.Credentials, latest.Credentials) {
			return req, denied(req, "unavailable")
		}
		port := req.URL.Port()
		if port == "" {
			port = "80"
			if req.URL.Scheme == "https" {
				port = "443"
			}
		}
		authority := req.URL.Host
		dial := func(ctx context.Context, network, target string) (net.Conn, error) {
			if target != net.JoinHostPort(req.URL.Hostname(), port) {
				return nil, errors.New("unadmitted dial target")
			}
			var d net.Dialer
			return d.DialContext(ctx, network, net.JoinHostPort(address.String(), port))
		}
		transport := &http.Transport{
			Proxy: nil, DialContext: dial, DisableKeepAlives: true, DisableCompression: true,
			ForceAttemptHTTP2: req.Header.Get("Upgrade") == "", TLSClientConfig: &tls.Config{RootCAs: options.Roots, MinVersion: tls.VersionTLS12},
		}
		var roundTrip http.RoundTripper = transport
		// A normal http.Transport speaks HTTP/1 to plaintext upstreams. h2c needs
		// its own transport and an explicitly non-TLS, pinned dial hook.
		if req.URL.Scheme == "http" && req.ProtoMajor == 2 {
			roundTrip = &http2.Transport{AllowHTTP: true, DialTLSContext: func(ctx context.Context, network, target string, _ *tls.Config) (net.Conn, error) {
				return dial(ctx, network, target)
			}}
		}
		ctx.RoundTripper = goproxy.RoundTripperFunc(func(out *http.Request, _ *goproxy.ProxyCtx) (*http.Response, error) {
			if out.URL.Host != authority {
				return nil, errors.New("authority changed after admission")
			}
			response, err := roundTrip.RoundTrip(out)
			if err == nil {
				if h2, ok := roundTrip.(*http2.Transport); ok {
					response.Body = &closingBody{ReadCloser: response.Body, closeIdle: h2.CloseIdleConnections}
				}
			}
			return response, err
		})
		return req, nil
	})
	return proxy, nil
}

type closingBody struct {
	io.ReadCloser
	closeIdle func()
}

func (b *closingBody) Close() error { err := b.ReadCloser.Close(); b.closeIdle(); return err }

func identify(req *http.Request, state *tunnel, resolver *auth.Resolver) (auth.Principal, error) {
	state.mu.Lock()
	defer state.mu.Unlock()
	values, supplied := req.Header["Proxy-Authorization"]
	bearer := state.bearer
	if supplied {
		// A replacement always clears earlier context, including malformed values.
		state.bearer = ""
		state.principal = auth.Principal{}
		if len(values) != 1 {
			return auth.Principal{}, errors.New("ambiguous hop bearer")
		}
		bearer = values[0]
	}
	principal, err := resolver.Resolve(metadata.NewIncomingContext(req.Context(), metadata.Pairs("authorization", bearer)))
	if err != nil {
		state.bearer = ""
		state.principal = auth.Principal{}
		return auth.Principal{}, err
	}
	if !supplied && state.principal != (auth.Principal{}) && state.principal != principal {
		state.bearer = ""
		state.principal = auth.Principal{}
		return auth.Principal{}, errors.New("authenticated context changed")
	}
	state.bearer, state.principal = bearer, principal
	return principal, nil
}

func admit(req *http.Request, principal auth.Principal, read func() (Snapshot, error), connect bool) (policyapi.Rule, Snapshot, error) {
	snapshot, err := read()
	if err != nil || !time.Now().Before(snapshot.FreshUntil) {
		return policyapi.Rule{}, snapshot, errors.New("snapshot unavailable")
	}
	for _, binding := range snapshot.Bindings {
		if binding.Spec.Subject.Namespace != principal.Account.Namespace || binding.Spec.Subject.Name != principal.Account.Name ||
			(binding.Spec.ExpiresAt != nil && !time.Now().Before(binding.Spec.ExpiresAt.Time)) {
			continue
		}
		for _, name := range binding.Spec.PolicySets {
			for _, set := range snapshot.Sets {
				if set.Name != name || set.Namespace != binding.Namespace {
					continue
				}
				for _, rule := range set.Spec.Rules {
					// Unsupported globs fail closed, rather than silently broadening a rule.
					if rule.Type != "egress" || !slices.Contains(rule.Hosts, req.URL.Hostname()) {
						continue
					}
					if !connect && ((len(rule.Methods) != 0 && !slices.Contains(rule.Methods, req.Method)) || (len(rule.Paths) != 0 && !slices.Contains(rule.Paths, req.URL.Path))) {
						continue
					}
					if !connect && !credentialMatches(req, rule, snapshot.Credentials) {
						continue
					}
					return rule, snapshot, nil
				}
			}
		}
	}
	return policyapi.Rule{}, snapshot, errors.New("no matching rule")
}

func credentialMatches(req *http.Request, rule policyapi.Rule, credentials map[string]HeaderCredential) bool {
	wanted := ""
	for name, credential := range credentials {
		if slices.Contains(req.Header.Values(credential.Header), "agentplane-credential-"+name) {
			if wanted != "" && wanted != name {
				return false
			}
			wanted = name
		}
	}
	return wanted == "" || (rule.CredentialRef != nil && rule.CredentialRef.Name == wanted)
}

func substitute(req *http.Request, rule policyapi.Rule, credentials map[string]HeaderCredential) error {
	for name, credential := range credentials {
		values := req.Header.Values(credential.Header)
		for i, value := range values {
			if value != "agentplane-credential-"+name {
				continue
			}
			if rule.CredentialRef == nil || rule.CredentialRef.Name != name || credential.Value == "" {
				return errors.New("credential unavailable")
			}
			values[i] = credential.Value
		}
	}
	return nil
}

func sameRule(a, b policyapi.Rule) bool {
	return a.Type == b.Type && a.ClusterInternal == b.ClusterInternal && slices.Equal(a.Hosts, b.Hosts) && slices.Equal(a.Methods, b.Methods) && slices.Equal(a.Paths, b.Paths) &&
		((a.CredentialRef == nil && b.CredentialRef == nil) || (a.CredentialRef != nil && b.CredentialRef != nil && a.CredentialRef.Name == b.CredentialRef.Name))
}

func sameCredentials(a, b map[string]HeaderCredential) bool {
	if len(a) != len(b) {
		return false
	}
	for key, value := range a {
		if b[key] != value {
			return false
		}
	}
	return true
}

func pin(ctx context.Context, host string, internal bool, options Options) (netip.Addr, error) {
	addresses, err := options.Resolve(ctx, "ip", host)
	if err != nil || len(addresses) == 0 {
		return netip.Addr{}, errors.New("unresolved")
	}
	for _, address := range addresses {
		address = address.Unmap()
		exempt := slices.ContainsFunc(options.Exempt, func(prefix netip.Prefix) bool { return prefix.Contains(address) })
		// Bound the spike to IPv4 until the complete IPv6 special-purpose range
		// contract has coverage. Reserved/documentation/benchmark ranges never pass
		// merely because netip calls them global unicast.
		forbidden := !address.Is4() || !address.IsGlobalUnicast() || address.IsLoopback() || address.IsLinkLocalUnicast() || (!internal && address.IsPrivate())
		for _, reserved := range []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"} {
			forbidden = forbidden || netip.MustParsePrefix(reserved).Contains(address)
		}
		if forbidden && !exempt {
			return netip.Addr{}, errors.New("forbidden address")
		}
	}
	return addresses[0].Unmap(), nil
}

func denied(req *http.Request, reason string) *http.Response {
	response := goproxy.NewResponse(req, "text/plain", http.StatusForbidden, "")
	response.Header.Set("x-agentplane-egress", "denied; reason="+reason)
	return response
}
