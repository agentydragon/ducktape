package firecracker

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

func testSynchronizeRequest(t *testing.T, bootID string, timestamp int64) *managementpb.SynchronizeRequest {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "firecracker-test.invalid"},
		NotBefore:    now.Add(-time.Minute),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	certificateDER, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	privateKeyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certificate := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificateDER})
	privateKey := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privateKeyDER})
	return &managementpb.SynchronizeRequest{
		UnixNano:       timestamp,
		TlsCert:        string(certificate),
		TlsKey:         string(privateKey),
		CaBundle:       string(certificate),
		ExpectedBootId: bootID,
	}
}

func managementClient(t *testing.T, service managementpb.GuestManagementServer) managementpb.GuestManagementClient {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxManagementMessageBytes))
	managementpb.RegisterGuestManagementServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(
		"passthrough:///guest-management-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return managementpb.NewGuestManagementClient(connection)
}

func TestGuestManagementSynchronizePreservesBootIDAndNotifiesOnce(t *testing.T) {
	directory := t.TempDir()
	tlsDirectory := filepath.Join(directory, "private")
	trustDirectory := filepath.Join(directory, "trust")
	if err := os.Mkdir(tlsDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(trustDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	files := credentialFiles{
		TLSCertPath:  filepath.Join(tlsDirectory, "tls.crt"),
		TLSKeyPath:   filepath.Join(tlsDirectory, "tls.key"),
		CABundlePath: filepath.Join(trustDirectory, "ca-certificates.crt"),
	}
	const bootID = "test-guest-boot-id"
	var clockValues []time.Time
	readyCalls := 0
	service := &managementServer{
		bootID: bootID,
		files:  files,
		setClock: func(value time.Time) error {
			clockValues = append(clockValues, value)
			return nil
		},
		onFirstSync: func() error {
			readyCalls++
			return nil
		},
	}
	client := managementClient(t, service)

	statusResponse, err := client.GetStatus(t.Context(), &managementpb.GetStatusRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if statusResponse.GetBootId() != bootID {
		t.Fatalf("boot ID = %q, want %q", statusResponse.GetBootId(), bootID)
	}
	var lastRequest *managementpb.SynchronizeRequest
	for _, timestamp := range []int64{1_900_000_000_000_000_001, 1_900_000_000_000_000_002} {
		lastRequest = testSynchronizeRequest(t, bootID, timestamp)
		response, err := client.Synchronize(t.Context(), lastRequest)
		if err != nil {
			t.Fatal(err)
		}
		if response.GetBootId() != bootID {
			t.Fatalf("synchronized boot ID = %q, want %q", response.GetBootId(), bootID)
		}
	}
	if len(clockValues) != 2 || clockValues[0].UnixNano() != 1_900_000_000_000_000_001 || clockValues[1].UnixNano() != 1_900_000_000_000_000_002 {
		t.Fatalf("clock values = %v, want the two request timestamps", clockValues)
	}
	if readyCalls != 1 {
		t.Fatalf("readiness notifications = %d, want 1", readyCalls)
	}
	for path, want := range map[string]string{
		files.TLSCertPath:  lastRequest.GetTlsCert(),
		files.TLSKeyPath:   lastRequest.GetTlsKey(),
		files.CABundlePath: lastRequest.GetCaBundle(),
	} {
		got, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("%s contents differ from the synchronized value", path)
		}
	}
	for path, mode := range map[string]os.FileMode{
		files.TLSCertPath:  0o600,
		files.TLSKeyPath:   0o600,
		files.CABundlePath: 0o644,
	} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Errorf("%s mode = %04o, want %04o", path, info.Mode().Perm(), mode)
		}
	}
}

func TestGuestManagementRejectsChangedBootIDBeforeMutation(t *testing.T) {
	directory := t.TempDir()
	files := credentialFiles{
		TLSCertPath:  filepath.Join(directory, "tls.crt"),
		TLSKeyPath:   filepath.Join(directory, "tls.key"),
		CABundlePath: filepath.Join(directory, "ca.crt"),
	}
	for path, contents := range map[string]string{
		files.TLSCertPath:  "old cert",
		files.TLSKeyPath:   "old key",
		files.CABundlePath: "old ca",
	} {
		if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	service := &managementServer{
		bootID: "current-boot-id",
		files:  files,
		setClock: func(time.Time) error {
			t.Fatal("changed boot ID reached the clock setter")
			return nil
		},
		onFirstSync: func() error {
			t.Fatal("changed boot ID reached readiness notification")
			return nil
		},
	}
	client := managementClient(t, service)
	_, err := client.Synchronize(t.Context(), testSynchronizeRequest(t, "stale-boot-id", time.Now().UnixNano()))
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Synchronize error code = %s, want %s", status.Code(err), codes.FailedPrecondition)
	}
	for path, want := range map[string]string{
		files.TLSCertPath:  "old cert",
		files.TLSKeyPath:   "old key",
		files.CABundlePath: "old ca",
	} {
		got, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("%s changed on stale synchronization", path)
		}
	}
}

func TestGuestManagementRejectsInvalidCredentialsAndOversizedRequests(t *testing.T) {
	directory := t.TempDir()
	service := &managementServer{
		bootID: "current-boot-id",
		files: credentialFiles{
			TLSCertPath:  filepath.Join(directory, "tls.crt"),
			TLSKeyPath:   filepath.Join(directory, "tls.key"),
			CABundlePath: filepath.Join(directory, "ca.crt"),
		},
		setClock: func(time.Time) error {
			t.Fatal("invalid request reached the clock setter")
			return nil
		},
		onFirstSync: func() error { return nil },
	}
	client := managementClient(t, service)
	invalid := testSynchronizeRequest(t, service.bootID, time.Now().UnixNano())
	invalid.TlsCert = "invalid certificate"
	if _, err := client.Synchronize(t.Context(), invalid); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid credential error code = %s, want %s", status.Code(err), codes.InvalidArgument)
	}
	oversized := &managementpb.SynchronizeRequest{
		UnixNano:       time.Now().UnixNano(),
		TlsCert:        strings.Repeat("x", maxManagementMessageBytes),
		ExpectedBootId: service.bootID,
	}
	if _, err := client.Synchronize(t.Context(), oversized); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("oversized request error code = %s, want %s", status.Code(err), codes.ResourceExhausted)
	}
}

func TestVsockAcceptsOnlyTheHostCID(t *testing.T) {
	if !isAllowedPeerCID(2) {
		t.Fatal("rejected the Firecracker host CID")
	}
	for _, peerCID := range []uint32{0, 3, 10} {
		if isAllowedPeerCID(peerCID) {
			t.Errorf("accepted non-host peer CID %d", peerCID)
		}
	}
}

func TestWriteAtomicExposesOnlyWholeCredentialFiles(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tls.crt")
	const size = 64 << 10
	if err := writeAtomic(path, bytes.Repeat([]byte{'a'}, size), 0o600); err != nil {
		t.Fatal(err)
	}
	started := make(chan struct{})
	done := make(chan struct{})
	readErrors := make(chan error, 1)
	go func() {
		close(started)
		for {
			contents, err := os.ReadFile(path)
			if err != nil {
				readErrors <- err
				return
			}
			if len(contents) != size {
				readErrors <- fmt.Errorf("observed a partial file with %d bytes", len(contents))
				return
			}
			for _, value := range contents {
				if value != contents[0] {
					readErrors <- errors.New("observed mixed old and new credential bytes")
					return
				}
			}
			select {
			case <-done:
				readErrors <- nil
				return
			default:
			}
		}
	}()
	<-started
	for index := range 8 {
		contents := bytes.Repeat([]byte{byte('b' + index%20)}, size)
		if err := writeAtomic(path, contents, 0o600); err != nil {
			close(done)
			t.Fatal(err)
		}
	}
	close(done)
	if err := <-readErrors; err != nil {
		t.Fatal(err)
	}
	entries, err := filepath.Glob(filepath.Join(filepath.Dir(path), ".agentplane-thaw-*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("temporary credential files remain: %v", entries)
	}
}
