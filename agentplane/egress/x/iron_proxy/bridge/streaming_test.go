package bridge

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/interop/grpc_testing"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const streamMethod = "/grpc.testing.TestService/FullDuplexCall"

type streamCall struct {
	headers metadata.MD
	done    chan struct{}
}

type echoService struct {
	grpc_testing.UnimplementedTestServiceServer
	calls chan *streamCall
}

func (s *echoService) FullDuplexCall(stream grpc_testing.TestService_FullDuplexCallServer) error {
	headers, _ := metadata.FromIncomingContext(stream.Context())
	call := &streamCall{headers: headers, done: make(chan struct{})}
	defer close(call.done)
	if err := stream.SendHeader(metadata.Pairs("stream-ready", "true")); err != nil {
		return err
	}
	s.calls <- call
	stream.SetTrailer(metadata.Pairs("stream-complete", "true"))
	for {
		message, err := stream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if err := stream.Send(&grpc_testing.StreamingOutputCallResponse{Payload: message.GetPayload()}); err != nil {
			return err
		}
	}
}

type streamTLS struct {
	certificate tls.Certificate
	roots       *x509.CertPool
	certPath    string
	keyPath     string
}

func newStreamTLS(t *testing.T) streamTLS {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1), DNSNames: []string{"localhost"},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true,
		KeyUsage:    x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	certPath, keyPath := filepath.Join(directory, "ca.pem"), filepath.Join(directory, "key.pem")
	for path, block := range map[string]*pem.Block{
		certPath: {Type: "CERTIFICATE", Bytes: der}, keyPath: {Type: "PRIVATE KEY", Bytes: keyDER},
	} {
		if err := os.WriteFile(path, pem.EncodeToMemory(block), 0600); err != nil {
			t.Fatal(err)
		}
	}
	roots := x509.NewCertPool()
	roots.AddCert(certificate)
	return streamTLS{certificate: tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, roots: roots, certPath: certPath, keyPath: keyPath}
}

func streamConnection(t *testing.T, address, target string, roots *x509.CertPool) *grpc.ClientConn {
	t.Helper()
	connection, err := grpc.NewClient(
		"passthrough:///"+target,
		grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: roots})),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", address)
			if err != nil {
				return nil, err
			}
			if deadline, ok := ctx.Deadline(); ok {
				_ = conn.SetDeadline(deadline)
			}
			if _, err := fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Bearer %s\r\n\r\n", target, target, testToken); err != nil {
				conn.Close()
				return nil, err
			}
			response, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: http.MethodConnect})
			if err != nil {
				conn.Close()
				return nil, err
			}
			if response.StatusCode != http.StatusOK {
				conn.Close()
				return nil, fmt.Errorf("CONNECT status %d", response.StatusCode)
			}
			_ = conn.SetDeadline(time.Time{})
			return conn, nil
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return connection
}

func receiveStreamCall(t *testing.T, calls <-chan *streamCall) *streamCall {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	select {
	case call := <-calls:
		if got := call.headers.Get("authorization"); len(got) != 1 || got[0] != "Bearer real-api-secret" {
			t.Fatalf("upstream authorization = %v", got)
		}
		for _, name := range []string{"proxy-authorization", "x-agentplane-workload-token"} {
			if got := call.headers.Get(name); len(got) != 0 {
				t.Fatalf("private metadata %s = %v", name, got)
			}
		}
		return call
	case <-ctx.Done():
		t.Fatal(ctx.Err())
		return nil
	}
}

func waitStreamDone(t *testing.T, call *streamCall) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	select {
	case <-call.done:
	case <-ctx.Done():
		t.Fatal("upstream did not observe cancellation:", ctx.Err())
	}
}

func TestIronBridgeStreaming(t *testing.T) {
	interception, upstreamTLS := newStreamTLS(t), newStreamTLS(t)
	service := &echoService{calls: make(chan *streamCall, 16)}
	upstreamGRPC := grpc.NewServer()
	grpc_testing.RegisterTestServiceServer(upstreamGRPC, service)
	t.Cleanup(upstreamGRPC.Stop)
	upstream := httptest.NewUnstartedServer(upstreamGRPC)
	upstream.EnableHTTP2 = true
	upstream.TLS = &tls.Config{Certificates: []tls.Certificate{upstreamTLS.certificate}}
	upstream.StartTLS()
	t.Cleanup(upstream.Close)
	_, port, err := net.SplitHostPort(upstream.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	target := net.JoinHostPort("localhost", port)

	for _, scenario := range []struct {
		name     string
		binary   string
		sendBody bool
	}{
		{name: "patched headers only", binary: "patched_binary"},
		{name: "stock headers only stalls after upstream headers", binary: "binary"},
		{name: "patched body forwarding stalls before admission", binary: "patched_binary", sendBody: true},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			lab := newTestBridge(t)
			lab.index.Rules.PutPolicySet(&policyapi.PolicySet{
				ObjectMeta: metav1.ObjectMeta{Name: "egress", Namespace: "policies"},
				Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
					Type: resources.EgressRuleType, Hosts: []string{"localhost"},
					Methods: []string{http.MethodPost}, Paths: []string{streamMethod},
					CredentialRef: &policyapi.CredentialRef{Name: "api"},
				}}},
			})
			address := startConfiguredIronProxy(t, lab.address, ironProxyConfig{
				binary: scenario.binary, caCert: interception.certPath, caKey: interception.keyPath,
				upstreamCA: upstreamTLS.certPath, sendRequestBody: scenario.sendBody,
			})
			connection := streamConnection(t, address, target, interception.roots)
			client := grpc_testing.NewTestServiceClient(connection)
			newStream := func(t *testing.T, ctx context.Context) grpc_testing.TestService_FullDuplexCallClient {
				t.Helper()
				ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(
					"authorization", "Bearer agentplane-credential-api",
					"x-agentplane-workload-token", "unused-projected-token",
				))
				stream, err := client.FullDuplexCall(ctx)
				if err != nil {
					t.Fatal(err)
				}
				return stream
			}
			duration := 10 * time.Second
			if scenario.binary == "binary" || scenario.sendBody {
				duration = 3 * time.Second
			}
			ctx, cancel := context.WithTimeout(t.Context(), duration)
			defer cancel()
			stream := newStream(t, ctx)
			headers, err := stream.Header()
			if scenario.binary == "binary" || scenario.sendBody {
				// gRPC can return nil metadata and nil error from Header when a stream
				// ends before headers arrive; Recv carries the terminal status.
				if (err != nil && status.Code(err) != codes.DeadlineExceeded) || len(headers) != 0 {
					t.Fatalf("blocked stream metadata = %v, error = %v", headers, err)
				}
				if _, err := stream.Recv(); status.Code(err) != codes.DeadlineExceeded {
					t.Fatalf("blocked stream error = %v, want deadline exceeded", err)
				}
				if scenario.sendBody {
					if got := lab.reviews.reviewCount(); got != 1 {
						t.Fatalf("TokenReviews = %d, want CONNECT only before body EOF", got)
					}
					select {
					case <-service.calls:
						t.Fatal("body-buffered stream unexpectedly reached upstream")
					default:
					}
				} else {
					call := receiveStreamCall(t, service.calls)
					waitStreamDone(t, call)
				}
				return
			}
			if err != nil || strings.Join(headers.Get("stream-ready"), "") != "true" {
				t.Fatalf("initial metadata = %v, error = %v", headers, err)
			}
			receiveStreamCall(t, service.calls)
			// Neither side closes its sending direction until all three responses arrive.
			for _, value := range []string{"first", "second", "third"} {
				if err := stream.Send(&grpc_testing.StreamingOutputCallRequest{Payload: &grpc_testing.Payload{Body: []byte(value)}}); err != nil {
					t.Fatal(err)
				}
				response, err := stream.Recv()
				if err != nil || string(response.GetPayload().GetBody()) != value {
					t.Fatalf("echo = %v, error = %v", response, err)
				}
			}
			if err := stream.CloseSend(); err != nil {
				t.Fatal(err)
			}
			if _, err := stream.Recv(); err != io.EOF {
				t.Fatalf("completed stream = %v, want EOF", err)
			}
			if got := stream.Trailer().Get("stream-complete"); len(got) != 1 || got[0] != "true" {
				t.Fatalf("trailers = %v", stream.Trailer())
			}

			for _, deadline := range []bool{false, true} {
				t.Run(fmt.Sprintf("cancellation deadline=%t", deadline), func(t *testing.T) {
					duration := 30 * time.Second
					if deadline {
						duration = time.Second
					}
					ctx, cancel := context.WithTimeout(t.Context(), duration)
					defer cancel()
					stream := newStream(t, ctx)
					if _, err := stream.Header(); err != nil {
						t.Fatal(err)
					}
					call := receiveStreamCall(t, service.calls)
					expected := codes.DeadlineExceeded
					if !deadline {
						cancel()
						expected = codes.Canceled
					}
					if _, err := stream.Recv(); status.Code(err) != expected {
						t.Fatalf("stream error = %v, want %v", err, expected)
					}
					waitStreamDone(t, call)
				})
			}
			lab.index.Rules.DeleteBinding("runner")
			denied := newStream(t, ctx)
			// Characterize the current rejection framing: the request is blocked, but the
			// HTTP refusal does not arrive as a gRPC permission-denied status.
			if _, err := denied.Recv(); status.Code(err) != codes.Internal {
				t.Fatalf("revoked binding error = %v, want the current Internal framing limitation", err)
			}
			if got := lab.reviews.reviewCount(); got != 5 {
				t.Fatalf("TokenReviews = %d, want one CONNECT and four inner requests", got)
			}
			select {
			case <-service.calls:
				t.Fatal("revoked stream reached upstream")
			default:
			}
		})
	}
}
