package proxy

import (
	"errors"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
)

// connectPassthrough authenticates the outer hop, then carries opaque service
// TLS. Only deployment-configured exact authorities enter here. The destination
// verifies the workload SVID and owns all authorization inside the connection.
func (p *Proxy) connectPassthrough(w http.ResponseWriter, request *http.Request, client *connection, host string, port int) {
	hop, supplied := request.Header[proxyAuthorization]
	request.Header.Del(proxyAuthorization)
	request.Header.Del(identity.ProjectedTokenHeader)
	refuse := func(reason decision.Reason, caller *authenticated) {
		p.record(request, client, host, port, outcome{decision: policy.Denied{Reason: reason}, caller: caller})
		deny(w, reason)
	}
	caller, err := p.authenticate(request.Context(), client, hop, supplied, nil)
	if err != nil {
		refuse(decision.TokenRejected, nil)
		return
	}
	if !p.options.Policy.Health.Available() {
		refuse(decision.Unavailable, caller)
		return
	}
	pin, err := p.options.Upstream.Pin(request.Context(), host, port, true)
	switch {
	case errors.Is(err, upstream.ErrAddressForbidden):
		refuse(decision.AddressForbidden, caller)
		return
	case errors.Is(err, upstream.ErrHostUnresolved):
		refuse(decision.HostUnresolved, caller)
		return
	case err != nil:
		refuse(decision.Unavailable, caller)
		return
	}
	upstream, err := (&net.Dialer{Timeout: 10 * time.Second}).DialContext(request.Context(), "tcp", net.JoinHostPort(pin.Address.String(), strconv.Itoa(port)))
	if err != nil {
		refuse(decision.Unavailable, caller)
		return
	}
	defer upstream.Close()
	if !p.options.Policy.Health.Available() {
		refuse(decision.Unavailable, caller)
		return
	}
	securedCaller, err := p.authenticate(request.Context(), client, nil, false, nil)
	if err != nil || securedCaller.principal.Account != caller.principal.Account ||
		securedCaller.principal.PodUID != caller.principal.PodUID {
		refuse(decision.TokenRejected, caller)
		return
	}
	incoming, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		refuse(decision.Unavailable, caller)
		http.Error(w, "CONNECT requires HTTP/1.1", http.StatusHTTPVersionNotSupported)
		return
	}
	defer incoming.Close()
	if !p.tunnels.add(incoming) {
		return
	}
	defer p.tunnels.remove(incoming)
	p.record(request, client, host, port, outcome{
		caller: caller, pin: pin, decision: policy.Allowed{ClusterInternal: true},
	})
	if _, err := io.WriteString(incoming, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		return
	}
	finished := make(chan error, 2)
	go func() { _, err := io.Copy(upstream, buffered); finished <- err }()
	go func() { _, err := io.Copy(incoming, upstream); finished <- err }()
	<-finished
	_ = incoming.Close()
	_ = upstream.Close()
	<-finished
}

func validatePassthroughTargets(targets []string) error {
	for _, target := range targets {
		host, port, err := net.SplitHostPort(target)
		portNumber, portErr := strconv.Atoi(port)
		if err != nil || host == "" || portErr != nil || portNumber < 1 || portNumber > 65535 || strings.ContainsAny(host, "* /\\\t\r\n") {
			return errors.New("TLS passthrough targets must be exact host:port authorities")
		}
	}
	return nil
}
