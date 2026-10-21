package push

import (
	"crypto/ecdh"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"testing"
)

// testSubject is the VAPID contact the tests' settings name.
const testSubject = "mailto:test-vapid@agentplane.test"

// vapidKey is a VAPID key pair: the PKCS #8 PEM a deployment configures, and the key a browser subscribes with.
func vapidKey(t *testing.T) (pemDocument, applicationServerKey string) {
	t.Helper()
	private, err := ecdh.P256().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})),
		base64.RawURLEncoding.EncodeToString(private.PublicKey().Bytes())
}

func testSettings(t *testing.T) (Settings, string) {
	t.Helper()
	key, applicationServerKey := vapidKey(t)
	return Settings{
		PrivateKeyPEM: key, Subject: testSubject, PublicBaseURL: "https://app.test/", AllowedPushHosts: []string{"push.test"},
	}, applicationServerKey
}

func TestTheIdentityReadsAP256KeyInEitherPEMForm(t *testing.T) {
	settings, applicationServerKey := testSettings(t)
	block, _ := pem.Decode([]byte(settings.PrivateKeyPEM))
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	sec1, err := x509.MarshalECPrivateKey(parsed.(*ecdsa.PrivateKey))
	if err != nil {
		t.Fatal(err)
	}
	for form, document := range map[string]string{
		"PKCS #8": settings.PrivateKeyPEM,
		"SEC 1":   string(pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: sec1})),
	} {
		settings.PrivateKeyPEM = document
		identity, err := NewIdentity(settings)
		if err != nil {
			t.Fatalf("%s: %v", form, err)
		}
		if got := identity.ApplicationServerKey(); got != applicationServerKey {
			t.Errorf("%s: application server key %q, want %q", form, got, applicationServerKey)
		}
	}
}

func pemOf(t *testing.T, key any) string {
	t.Helper()
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
}

func TestSettingsWebPushCannotRunOnAreRefused(t *testing.T) {
	p384, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	rsaKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*Settings){
		"no key":        func(s *Settings) { s.PrivateKeyPEM = "" },
		"no subject":    func(s *Settings) { s.Subject = "" },
		"no base URL":   func(s *Settings) { s.PublicBaseURL = "" },
		"no push host":  func(s *Settings) { s.AllowedPushHosts = nil },
		"a key not PEM": func(s *Settings) { s.PrivateKeyPEM = "test-not-a-key" },
		"a P-384 key":   func(s *Settings) { s.PrivateKeyPEM = pemOf(t, p384) },
		"an RSA key":    func(s *Settings) { s.PrivateKeyPEM = pemOf(t, rsaKey) },
		"a key of bytes": func(s *Settings) {
			s.PrivateKeyPEM = string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: []byte("test")}))
		},
	} {
		t.Run(name, func(t *testing.T) {
			settings, _ := testSettings(t)
			change(&settings)
			if err := settings.Validate(); err == nil {
				t.Error("accepted")
			}
		})
	}
}

func TestTheIdentitySendsOnlyToReviewedPushServices(t *testing.T) {
	settings, _ := testSettings(t)
	identity, err := NewIdentity(settings)
	if err != nil {
		t.Fatal(err)
	}
	for _, endpoint := range []string{
		"https://push.test/test-subscription",
		"https://PUSH.test/test-subscription",
		"https://push.test:443/test-subscription",
	} {
		if err := identity.CheckEndpoint(endpoint); err != nil {
			t.Errorf("%s: %v", endpoint, err)
		}
	}
	for _, endpoint := range []string{
		"https://unreviewed.test/test-subscription",
		"http://push.test/test-subscription",
		"https://push.test:8443/test-subscription",
		"https://test-user:test-password@push.test/test-subscription",
		"https://push.test/test-subscription#test-fragment",
		"https://push.test:test-port/test-subscription",
		"/test-subscription",
	} {
		if err := identity.CheckEndpoint(endpoint); !errors.Is(err, ErrUnsupportedEndpoint) {
			t.Errorf("%s: %v, want ErrUnsupportedEndpoint", endpoint, err)
		}
	}
}
