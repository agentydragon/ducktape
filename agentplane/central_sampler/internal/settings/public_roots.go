package settings

import (
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"os"
	"sync"
)

// External provider transports are shared across samples, but replace their root pool when the
// projected bundle changes. Closing idle connections leaves in-flight provider streams untouched.
var publicRootTransports = struct {
	sync.Mutex
	entries map[publicRootKey]publicRootEntry
}{entries: make(map[publicRootKey]publicRootEntry)}

type publicRootKey struct {
	base *http.Transport
	path string
}

type publicRootEntry struct {
	digest    [sha256.Size]byte
	transport *http.Transport
}

func clientWithPublicRoots(client *http.Client) (*http.Client, error) {
	path := os.Getenv("SSL_CERT_FILE")
	if path == "" {
		return client, nil
	}
	base := client.Transport
	if base == nil {
		base = http.DefaultTransport
	}
	transport, ok := base.(*http.Transport)
	if !ok {
		return nil, fmt.Errorf("public roots require a configurable HTTP transport")
	}
	pem, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read public roots: %w", err)
	}
	digest := sha256.Sum256(pem)
	key := publicRootKey{base: transport, path: path}
	publicRootTransports.Lock()
	defer publicRootTransports.Unlock()
	entry, found := publicRootTransports.entries[key]
	if !found || entry.digest != digest {
		roots := x509.NewCertPool()
		if !roots.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("public roots contain no certificates")
		}
		replacement := transport.Clone()
		if replacement.TLSClientConfig == nil {
			replacement.TLSClientConfig = &tls.Config{}
		} else {
			replacement.TLSClientConfig = replacement.TLSClientConfig.Clone()
		}
		replacement.TLSClientConfig.RootCAs = roots
		if found {
			entry.transport.CloseIdleConnections()
		}
		entry = publicRootEntry{digest: digest, transport: replacement}
		publicRootTransports.entries[key] = entry
	}
	copy := *client
	copy.Transport = entry.transport
	return &copy, nil
}
