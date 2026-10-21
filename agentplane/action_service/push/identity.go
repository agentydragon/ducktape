// Package push notifies an operator's browsers over Web Push while Action requests wait for a Decision, and keeps the
// subscriptions the browsers register.
//
// Delivery reconciles from the tables: each subscription's ledger (`action_push_delivery`) records what its browser was
// last told of each request, and a pass compares it with the requests awaiting a Decision, which it reads of the
// lifecycle through Lifecycle alone. webpush-go signs the VAPID header (RFC 8292) and encrypts the payload (RFC 8291,
// `aes128gcm`); the HTTP send runs with no transaction open.
package push

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"net/url"
	"strings"
)

// Settings are the Python service's `web_push` keys (push_settings.py). A deployment supplies the private key, a
// Secret, as AGENTPLANE_ACTIONS_WEB_PUSH__PRIVATE_KEY_PEM.
type Settings struct {
	// PrivateKeyPEM is the VAPID key: a P-256 private key, PKCS #8 or SEC 1.
	PrivateKeyPEM string `mapstructure:"private_key_pem"`
	// Subject is the VAPID contact, a `mailto:` or `https:` URI.
	Subject string `mapstructure:"subject"`
	// PublicBaseURL is the integration app's address, which a notification links into.
	PublicBaseURL string `mapstructure:"public_base_url"`
	// AllowedPushHosts are the reviewed push services' hostnames; no subscription names another.
	AllowedPushHosts []string `mapstructure:"allowed_push_hosts"`
}

// Configured is whether any setting is set, which turns Web Push on.
func (s Settings) Configured() bool {
	return s.PrivateKeyPEM != "" || s.Subject != "" || s.PublicBaseURL != "" || len(s.AllowedPushHosts) > 0
}

// Validate refuses settings Web Push cannot run on. Its errors name the key, never the value.
func (s Settings) Validate() error {
	_, err := NewIdentity(s)
	return err
}

// ErrUnsupportedEndpoint is a push endpoint that is not an HTTPS URL of a configured push service.
var ErrUnsupportedEndpoint = errors.New("push endpoint is not an HTTPS URL of a configured push service")

// Identity is the service's VAPID identity and the push services it sends to.
type Identity struct {
	// privateKey and publicKey are the VAPID key pair, as webpush-go reads them: unpadded base64url of the private
	// scalar and of the uncompressed public point.
	privateKey, publicKey string
	// subscriber is Subject as webpush-go reads it, which prefixes `mailto:` to any value not an `https:` URI.
	subscriber string
	hosts      map[string]struct{}
}

func NewIdentity(settings Settings) (*Identity, error) {
	for _, required := range []struct{ key, value string }{
		{"private_key_pem", settings.PrivateKeyPEM},
		{"subject", settings.Subject},
		{"public_base_url", settings.PublicBaseURL},
	} {
		if required.value == "" {
			return nil, errors.New("web_push." + required.key + " is required")
		}
	}
	if len(settings.AllowedPushHosts) == 0 {
		return nil, errors.New("web_push.allowed_push_hosts must name at least one host")
	}
	key, err := parsePrivateKey(settings.PrivateKeyPEM)
	if err != nil {
		return nil, err
	}
	exchange, err := key.ECDH()
	if err != nil {
		return nil, errors.New("web_push.private_key_pem is not a P-256 private key")
	}
	hosts := make(map[string]struct{}, len(settings.AllowedPushHosts))
	for _, host := range settings.AllowedPushHosts {
		hosts[host] = struct{}{}
	}
	return &Identity{
		privateKey: base64.RawURLEncoding.EncodeToString(exchange.Bytes()),
		publicKey:  base64.RawURLEncoding.EncodeToString(exchange.PublicKey().Bytes()),
		subscriber: strings.TrimPrefix(settings.Subject, "mailto:"),
		hosts:      hosts,
	}, nil
}

// parsePrivateKey reads a PEM P-256 private key in either form Python's `Vapid02.from_pem` reads.
func parsePrivateKey(document string) (*ecdsa.PrivateKey, error) {
	block, _ := pem.Decode([]byte(document))
	if block == nil {
		return nil, errors.New("web_push.private_key_pem is not PEM")
	}
	var key *ecdsa.PrivateKey
	if parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		key, _ = parsed.(*ecdsa.PrivateKey)
	} else if key, err = x509.ParseECPrivateKey(block.Bytes); err != nil {
		return nil, errors.New("web_push.private_key_pem is not a PKCS #8 or SEC 1 private key")
	}
	if key == nil || key.Curve != elliptic.P256() {
		return nil, errors.New("web_push.private_key_pem is not a P-256 private key")
	}
	return key, nil
}

// ApplicationServerKey is the key a browser subscribes with: unpadded base64url of the uncompressed public point.
func (i *Identity) ApplicationServerKey() string { return i.publicKey }

// CheckEndpoint refuses an endpoint the service will not send to: one that is not HTTPS on the default port, names an
// unreviewed host, or carries credentials or a fragment.
func (i *Identity) CheckEndpoint(endpoint string) error {
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Fragment != "" {
		return ErrUnsupportedEndpoint
	}
	if port := parsed.Port(); port != "" && port != "443" {
		return ErrUnsupportedEndpoint
	}
	if _, reviewed := i.hosts[strings.ToLower(parsed.Hostname())]; !reviewed {
		return ErrUnsupportedEndpoint
	}
	return nil
}
