package iron_proxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ironsh/iron-proxy/internal/certcache"
	"github.com/ironsh/iron-proxy/internal/proxy"
	"github.com/ironsh/iron-proxy/internal/transform"
	"golang.org/x/net/http2"
)

var expectStreaming = flag.Bool("iron-streaming-patch", false, "expect the optional streaming patch to be applied")

type testCA struct {
	certificate *x509.Certificate
	key         *ecdsa.PrivateKey
	pool        *x509.CertPool
	pem         []byte
}

func makeCA(t *testing.T) testCA {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "iron reuse test"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(certificate)
	return testCA{certificate: certificate, key: key, pool: pool, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

func (ca testCA) leaf(t *testing.T) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(2), DNSNames: []string{"localhost"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca.certificate, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// The public proxy API binds its own listeners and exposes their addresses only through logs.
type startupLogs struct{ addresses chan map[string]string }

func (l startupLogs) Write(data []byte) (int, error) {
	var entry map[string]any
	if err := json.Unmarshal(data, &entry); err != nil {
		return 0, err
	}
	if message, ok := entry["msg"].(string); ok && strings.HasSuffix(message, "proxy starting") {
		if address, ok := entry["addr"].(string); ok {
			l.addresses <- map[string]string{message: address}
		}
	}
	return len(data), nil
}

func startIron(t *testing.T, ca testCA, transforms ...transform.Transformer) string {
	t.Helper()
	cache, err := certcache.NewFromCA(ca.certificate, ca.key, 16, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	logs := startupLogs{addresses: make(chan map[string]string, 3)}
	logger := slog.New(slog.NewJSONHandler(logs, nil))
	pipeline := transform.NewPipeline(transforms, transform.BodyLimits{MaxRequestBodyBytes: 65536}, logger)
	server := proxy.New(proxy.Options{
		HTTPAddr: "127.0.0.1:0", HTTPSAddr: "127.0.0.1:0", TunnelAddr: "127.0.0.1:0",
		CertCache: cache, Pipeline: transform.NewPipelineHolder(pipeline), Logger: logger,
	})
	done := make(chan error, 1)
	go func() { done <- server.ListenAndServe() }()
	address := ""
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	for range 3 {
		select {
		case event := <-logs.addresses:
			if target, ok := event["tunnel proxy starting"]; ok {
				address = target
			}
		case err := <-done:
			t.Fatal(err)
		case <-ctx.Done():
			t.Fatal("proxy did not publish its bound listeners")
		}
	}
	t.Cleanup(func() {
		shutdown, stop := context.WithTimeout(context.Background(), 3*time.Second)
		defer stop()
		if err := server.Shutdown(shutdown); err != nil {
			t.Error(err)
		}
		<-done
	})
	return address
}

func connect(t *testing.T, address, target string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	if _, err := fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: "CONNECT"})
	if err != nil || response.StatusCode != 200 {
		t.Fatalf("CONNECT: response=%v error=%v", response, err)
	}
	return conn
}

func h2Client(t *testing.T, ca testCA, address, target string, encrypted bool) *http2.ClientConn {
	t.Helper()
	conn := connect(t, address, target)
	if encrypted {
		secured := tls.Client(conn, &tls.Config{RootCAs: ca.pool, ServerName: "localhost", NextProtos: []string{"h2"}})
		if err := secured.HandshakeContext(t.Context()); err != nil {
			t.Fatal(err)
		}
		conn = secured
	}
	client, err := (&http2.Transport{}).NewClientConn(conn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { client.Close() })
	return client
}

func upstreamServer(t *testing.T, ca testCA, handler http.HandlerFunc) string {
	t.Helper()
	server := httptest.NewUnstartedServer(handler)
	server.EnableHTTP2 = true
	server.TLS = &tls.Config{Certificates: []tls.Certificate{ca.leaf(t)}}
	server.StartTLS()
	t.Cleanup(server.Close)
	_, port, err := net.SplitHostPort(server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	return net.JoinHostPort("localhost", port)
}

func TestIronTransport(t *testing.T) {
	interception, upstream := makeCA(t), makeCA(t)
	caPath := filepath.Join(t.TempDir(), "upstream.pem")
	if err := os.WriteFile(caPath, upstream.pem, 0600); err != nil {
		t.Fatal(err)
	}
	// Upstream verification has no injected root-pool option; configure the standard system loader
	// before any subtest uses it. Every TLS upstream below uses this same CA.
	t.Setenv("SSL_CERT_FILE", caPath)
	t.Setenv("SSL_CERT_DIR", t.TempDir())

	t.Run("finite TLS HTTP2 response and trailers", func(t *testing.T) {
		target := upstreamServer(t, upstream, func(w http.ResponseWriter, r *http.Request) {
			if r.ProtoMajor != 2 {
				t.Errorf("upstream protocol: %s", r.Proto)
			}
			w.Header().Set("Trailer", "Grpc-Status")
			w.Write([]byte("frame"))
			w.Header().Set("Grpc-Status", "0")
		})
		client := h2Client(t, interception, startIron(t, interception), target, true)
		request, err := http.NewRequestWithContext(t.Context(), "POST", "https://"+target+"/rpc", nil)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.RoundTrip(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		if err != nil || string(body) != "frame" || response.Trailer.Get("Grpc-Status") != "0" {
			t.Fatalf("body=%q trailers=%v error=%v", body, response.Trailer, err)
		}
	})

	t.Run("interactive TLS HTTP2 metadata and frames", func(t *testing.T) {
		headersSent := make(chan struct{})
		target := upstreamServer(t, upstream, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/grpc")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			close(headersSent)
			frame := make([]byte, 5)
			if _, err := io.ReadFull(r.Body, frame); err == nil {
				w.Write(frame)
				w.(http.Flusher).Flush()
			}
			io.Copy(io.Discard, r.Body)
		})
		client := h2Client(t, interception, startIron(t, interception), target, true)
		reader, writer := io.Pipe()
		defer writer.Close()
		ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
		defer cancel()
		request, err := http.NewRequestWithContext(ctx, "POST", "https://"+target+"/rpc", reader)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.RoundTrip(request)
		select {
		case <-headersSent:
		default:
			t.Fatal("upstream never flushed headers; probe did not reach the discriminating condition")
		}
		if *expectStreaming {
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			frame := []byte{0, 0, 0, 0, 0}
			if _, err := writer.Write(frame); err != nil {
				t.Fatal(err)
			}
			echo := make([]byte, len(frame))
			if _, err := io.ReadFull(response.Body, echo); err != nil || !bytes.Equal(echo, frame) {
				t.Fatalf("interactive frame=%v error=%v", echo, err)
			}
			writer.Close()
			t.Log("patched transport delivered initial metadata and a response frame before request EOF")
			return
		}
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("expected the pinned transport's missing-flush blocker, got %v", err)
		}
		t.Log("observed blocker: upstream flushed initial metadata, client waited until cancellation")
	})

	t.Run("cleartext HTTP2 inside CONNECT is refused", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
			t.Error("h2c probe unexpectedly reached upstream")
		}))
		defer server.Close()
		target := strings.TrimPrefix(server.URL, "http://")
		client := h2Client(t, interception, startIron(t, interception), target, false)
		ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
		defer cancel()
		request, err := http.NewRequestWithContext(ctx, "POST", "http://"+target+"/rpc", nil)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.RoundTrip(request)
		if err == nil {
			response.Body.Close()
			t.Fatal("expected the pinned transport's missing h2c support")
		}
		if errors.Is(err, context.DeadlineExceeded) {
			t.Fatal("h2c probe timed out instead of observing protocol refusal")
		}
		t.Logf("observed h2c protocol refusal: %v", err)
	})
}

func TestIronBufferedBody(t *testing.T) {
	body := transform.NewBufferedBody(io.NopCloser(bytes.NewReader([]byte("abcdefgh"))), 4)
	read, err := io.ReadAll(body)
	if err != nil || string(read) != "abcd" {
		t.Fatalf("buffered read=%q error=%v", read, err)
	}
	body.Reset()
	forwarded, err := io.ReadAll(body.StreamingReader())
	if err != nil || string(forwarded) != "abcd" {
		t.Fatalf("forwarded=%q error=%v", forwarded, err)
	}
	t.Log("observed blocker: a body read at the configured cap silently truncates what is forwarded")

	unread := transform.NewBufferedBody(io.NopCloser(bytes.NewReader([]byte("abcdefgh"))), 4)
	forwarded, err = io.ReadAll(unread.StreamingReader())
	if err != nil || string(forwarded) != "abcdefgh" {
		t.Fatalf("unread body lost bytes: %q %v", forwarded, err)
	}
}

func TestIronPlainForwardProxy(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ordinary HTTP"))
	}))
	defer upstream.Close()
	proxyURL, err := url.Parse("http://" + startIron(t, makeCA(t)))
	if err != nil {
		t.Fatal(err)
	}
	transport := &http.Transport{Proxy: http.ProxyURL(proxyURL)}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
	response, err := client.Get(upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil || response.StatusCode != 200 || string(body) != "ordinary HTTP" {
		t.Fatalf("status=%d body=%q error=%v", response.StatusCode, body, err)
	}
}
