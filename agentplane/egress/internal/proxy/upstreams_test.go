package proxy

import (
	"crypto/tls"
	"encoding/pem"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
)

func TestUpstreamRootsRotateWithoutRestart(t *testing.T) {
	firstCA, firstRoots := makeCA(t, "first")
	secondCA, secondRoots := makeCA(t, "second")
	server := func(cert tls.Certificate) *httptest.Server {
		s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) }))
		s.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
		s.StartTLS()
		return s
	}
	first := server(issue(t, firstCA, "localhost"))
	defer first.Close()
	second := server(issue(t, secondCA, "localhost"))
	defer second.Close()
	path := filepath.Join(t.TempDir(), "roots.pem")
	write := func(data []byte) {
		t.Helper()
		// Rename matches a projected ConfigMap update and works even if PEM sizes match.
		tmp := path + ".new"
		if err := os.WriteFile(tmp, data, 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(tmp, path); err != nil {
			t.Fatal(err)
		}
	}
	write(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: firstCA.Certificate[0]}))
	upstreams := newUpstreams(firstRoots, path)
	defer upstreams.closeIdle()
	request := func(s *httptest.Server) error {
		parsed, err := url.Parse(s.URL)
		if err != nil {
			return err
		}
		_, port, err := net.SplitHostPort(parsed.Host)
		if err != nil {
			return err
		}
		target := net.JoinHostPort("localhost", port)
		transport, err := upstreams.transport(upstreamKey{target: target, dialed: parsed.Host, scheme: "https"})
		if err != nil {
			return err
		}
		response, err := (&http.Client{Transport: transport}).Get("https://" + target)
		if err == nil {
			response.Body.Close()
		}
		return err
	}
	if err := request(first); err != nil {
		t.Fatal(err)
	}
	write(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: secondCA.Certificate[0]}))
	if err := request(second); err != nil {
		t.Fatal(err)
	}
	if !upstreams.roots.Equal(secondRoots) {
		t.Fatal("new roots not installed")
	}
	if err := request(first); err == nil {
		t.Fatal("removed root still trusted")
	}
	write([]byte("invalid"))
	if err := request(second); err == nil {
		t.Fatal("invalid replacement bundle accepted")
	}
}
