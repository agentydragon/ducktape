// Package environmentruntimetransporttesting creates short-lived mTLS fixtures for tests.
package environmentruntimetransporttesting

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
)

// CA is a test certificate authority whose own certificate is written to RootFile.
type CA struct {
	certificate *x509.Certificate
	privateKey  *ecdsa.PrivateKey
	// chain is appended to every leaf's tls.crt: the intermediates up to, not including, the root.
	chain    []byte
	RootFile string
}

// NewCA creates an ephemeral ECDSA root CA for one test.
func NewCA(t *testing.T) *CA {
	t.Helper()
	now := time.Now().UTC()
	return NewCAWithValidity(t, now.Add(-time.Minute), now.Add(24*time.Hour))
}

// NewCAWithValidity creates an ephemeral root with explicit validity bounds.
// It supports expiration checks without making the normal fixture time-sensitive.
func NewCAWithValidity(t *testing.T, notBefore, notAfter time.Time) *CA {
	t.Helper()
	return newCA(t, nil, notBefore, notAfter)
}

// Intermediate creates a CA that ca signs. Its leaves carry the intermediate in
// tls.crt, as cert-manager writes them, so they verify against either ca's
// RootFile or the intermediate's own RootFile alone.
func (ca *CA) Intermediate(t *testing.T) *CA {
	t.Helper()
	now := time.Now().UTC()
	return newCA(t, ca, now.Add(-time.Minute), now.Add(24*time.Hour))
}

func newCA(t *testing.T, parent *CA, notBefore, notAfter time.Time) *CA {
	t.Helper()
	privateKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate test CA key: %v", err)
	}
	serial, err := randomSerial()
	if err != nil {
		t.Fatalf("generate test CA serial: %v", err)
	}
	name := "runner transport test CA"
	if parent != nil {
		name = "runner transport test intermediate CA"
	}
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	issuer, signer := template, privateKey
	if parent != nil {
		issuer, signer = parent.certificate, parent.privateKey
	}
	der, err := x509.CreateCertificate(rand.Reader, template, issuer, &privateKey.PublicKey, signer)
	if err != nil {
		t.Fatalf("create test CA certificate: %v", err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parse test CA certificate: %v", err)
	}
	encoded := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	rootFile := filepath.Join(t.TempDir(), "ca.pem")
	writeFile(t, rootFile, encoded, 0o644)
	ca := &CA{certificate: certificate, privateKey: privateKey, RootFile: rootFile}
	if parent != nil {
		ca.chain = append(append([]byte(nil), encoded...), parent.chain...)
	}
	return ca
}

// Issue creates a dual-use client/server certificate for uri and returns the
// file-backed credentials consumed by environment_runtime_transport.Config.
func (ca *CA) Issue(t *testing.T, uri string) environment_runtime_transport.Config {
	t.Helper()
	return ca.IssueWithUsage(t, uri, x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)
}

// IssueWithUsage creates a certificate with explicit extended key usages. It
// lets tests verify that client and server authentication require the intended
// usage instead of accepting an unconstrained certificate.
func (ca *CA) IssueWithUsage(t *testing.T, rawURI string, usages ...x509.ExtKeyUsage) environment_runtime_transport.Config {
	t.Helper()
	now := time.Now().UTC()
	return ca.IssueWithValidity(t, rawURI, now.Add(-time.Minute), now.Add(24*time.Hour), usages...)
}

// IssueWithValidity creates a leaf with explicit validity bounds and EKUs.
func (ca *CA) IssueWithValidity(
	t *testing.T,
	rawURI string,
	notBefore, notAfter time.Time,
	usages ...x509.ExtKeyUsage,
) environment_runtime_transport.Config {
	t.Helper()
	identity, err := url.Parse(rawURI)
	if err != nil || identity.Scheme == "" || identity.Host == "" {
		t.Fatalf("parse test identity URI %q: %v", rawURI, err)
	}
	privateKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate test leaf key: %v", err)
	}
	serial, err := randomSerial()
	if err != nil {
		t.Fatalf("generate test leaf serial: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: rawURI},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           append([]x509.ExtKeyUsage(nil), usages...),
		URIs:                  []*url.URL{identity},
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca.certificate, &privateKey.PublicKey, ca.privateKey)
	if err != nil {
		t.Fatalf("create test leaf certificate: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(privateKey)
	if err != nil {
		t.Fatalf("marshal test leaf key: %v", err)
	}
	directory := t.TempDir()
	certFile := filepath.Join(directory, "tls.crt")
	keyFile := filepath.Join(directory, "tls.key")
	writeFile(t, certFile, append(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), ca.chain...), 0o644)
	writeFile(t, keyFile, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}), 0o600)
	return environment_runtime_transport.Config{CertFile: certFile, KeyFile: keyFile, CAFile: ca.RootFile}
}

func randomSerial() (*big.Int, error) {
	return rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
}

func writeFile(t *testing.T, path string, contents []byte, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, contents, mode); err != nil {
		t.Fatalf("write test certificate file %s: %v", path, err)
	}
}
