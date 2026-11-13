package settings

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPublicRootsRotateBetweenProviderRequests(t *testing.T) {
	server := func() *httptest.Server {
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		template := &x509.Certificate{
			SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "fixture"},
			NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
			IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, IsCA: true, BasicConstraintsValid: true,
			KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		}
		der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
		if err != nil {
			t.Fatal(err)
		}
		s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") }))
		s.TLS = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}}
		s.StartTLS()
		return s
	}
	first := server()
	defer first.Close()
	second := server()
	defer second.Close()

	path := filepath.Join(t.TempDir(), "roots.pem")
	writeRoots := func(data []byte) {
		t.Helper()
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	cert := func(s *httptest.Server) []byte {
		return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: s.Certificate().Raw})
	}
	t.Setenv("SSL_CERT_FILE", path)
	writeRoots(cert(first))
	base := &http.Client{}
	connection := ConnectionConfig{}
	client, err := connection.HTTPClient(base)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.Get(first.URL)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()

	// A changed file must replace the transport's roots without restarting the process.
	writeRoots(cert(second))
	rotated, err := connection.HTTPClient(base)
	if err != nil {
		t.Fatal(err)
	}
	response, err = rotated.Get(second.URL)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response, err = rotated.Get(first.URL); err == nil {
		response.Body.Close()
		t.Fatal("removed root still trusts the first server")
	}
	writeRoots([]byte("invalid"))
	if _, err := connection.HTTPClient(base); err == nil {
		t.Fatal("invalid replacement bundle accepted")
	}
}
