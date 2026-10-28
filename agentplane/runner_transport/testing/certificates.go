// Package runnertransporttesting creates short-lived mTLS fixtures for tests.
package runnertransporttesting

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

	"github.com/agentydragon/ducktape/agentplane/runner_transport"
)

// CA is a test certificate authority whose root is written to RootFile.
type CA struct {
	certificate *x509.Certificate
	privateKey  *ecdsa.PrivateKey
	RootFile    string
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
	privateKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate test CA key: %v", err)
	}
	serial, err := randomSerial()
	if err != nil {
		t.Fatalf("generate test CA serial: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "runner transport test CA"},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &privateKey.PublicKey, privateKey)
	if err != nil {
		t.Fatalf("create test CA certificate: %v", err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parse test CA certificate: %v", err)
	}
	rootFile := filepath.Join(t.TempDir(), "ca.pem")
	writeFile(t, rootFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644)
	return &CA{certificate: certificate, privateKey: privateKey, RootFile: rootFile}
}

// Issue creates a dual-use client/server certificate for uri and returns the
// file-backed credentials consumed by runner_transport.Config.
func (ca *CA) Issue(t *testing.T, uri string) runner_transport.Config {
	t.Helper()
	return ca.IssueWithUsage(t, uri, x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)
}

// IssueWithUsage creates a certificate with explicit extended key usages. It
// lets tests verify that client and server authentication require the intended
// usage instead of accepting an unconstrained certificate.
func (ca *CA) IssueWithUsage(t *testing.T, rawURI string, usages ...x509.ExtKeyUsage) runner_transport.Config {
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
) runner_transport.Config {
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
	writeFile(t, certFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644)
	writeFile(t, keyFile, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}), 0o600)
	return runner_transport.Config{CertFile: certFile, KeyFile: keyFile, CAFile: ca.RootFile}
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
