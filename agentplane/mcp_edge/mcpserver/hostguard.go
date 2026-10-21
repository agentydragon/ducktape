package mcpserver

import (
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// guardHostAndOrigin is FastMCP's host and origin guard in its "auto" mode, against DNS rebinding of a server bound
// to loopback. On a loopback listener, a Host that names neither loopback nor the listener's own address is
// misdirected. Where the listener or the Host is loopback, a browser Origin must be loopback too or the Host's own
// origin.
func guardHostAndOrigin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		server := listenerHost(r)
		serverLoopback := server != "" && isLoopbackHost(server)
		if serverLoopback && !hostAllowed(r.Host, server) {
			http.Error(w, "Misdirected Request", http.StatusMisdirectedRequest)
			return
		}
		origin := r.Header.Get("Origin")
		if origin != "" && (serverLoopback || isLoopbackHost(r.Host)) && !originAllowed(origin, r) {
			http.Error(w, "Forbidden Origin", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func listenerHost(r *http.Request) string {
	address, ok := r.Context().Value(http.LocalAddrContextKey).(net.Addr)
	if !ok {
		return ""
	}
	host, _, err := net.SplitHostPort(address.String())
	if err != nil {
		return ""
	}
	return host
}

// normalizeHost is a Host header's host: lower case, without brackets or a port.
func normalizeHost(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if strings.HasPrefix(host, "[") {
		if end := strings.Index(host, "]"); end != -1 {
			return host[1:end]
		}
		return host
	}
	if strings.Count(host, ":") == 1 {
		host, _, _ = strings.Cut(host, ":")
	}
	return host
}

func isLoopbackHost(host string) bool {
	host = normalizeHost(host)
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func hostAllowed(host, server string) bool {
	allowed := []string{"127.0.0.1", "localhost", "::1"}
	if ip := net.ParseIP(server); ip == nil || !ip.IsUnspecified() {
		allowed = append(allowed, server)
	}
	host = normalizeHost(host)
	for _, candidate := range allowed {
		if host == normalizeHost(candidate) {
			return true
		}
	}
	return false
}

func originAllowed(origin string, r *http.Request) bool {
	if parsed, err := url.Parse(strings.TrimSpace(origin)); err == nil && isLoopbackHost(parsed.Hostname()) &&
		isLoopbackHost(r.Host) {
		return true
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	return normalizeOrigin(origin) == normalizeOrigin(scheme+"://"+r.Host)
}

// normalizeOrigin is scheme://host:port with the scheme's default port spelled out, or the origin in lower case
// where it is not of that form.
func normalizeOrigin(origin string) string {
	origin = strings.TrimRight(strings.TrimSpace(origin), "/")
	parsed, err := url.Parse(origin)
	if err != nil || parsed.Scheme == "" || parsed.Hostname() == "" || parsed.Path != "" || parsed.RawQuery != "" ||
		parsed.Fragment != "" {
		return strings.ToLower(origin)
	}
	scheme := strings.ToLower(parsed.Scheme)
	host := normalizeHost(parsed.Hostname())
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	port := parsed.Port()
	if port == "" {
		switch scheme {
		case "http":
			port = "80"
		case "https":
			port = "443"
		default:
			return scheme + "://" + host
		}
	} else if number, err := strconv.Atoi(port); err == nil && number <= 65535 {
		port = strconv.Itoa(number)
	} else {
		return strings.ToLower(origin)
	}
	return scheme + "://" + host + ":" + port
}
