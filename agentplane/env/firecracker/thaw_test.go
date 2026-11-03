package firecracker

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type testConnection struct {
	reader *bytes.Reader
	output bytes.Buffer
}

func (connection *testConnection) Read(destination []byte) (int, error) {
	return connection.reader.Read(destination)
}

func (connection *testConnection) Write(source []byte) (int, error) {
	return connection.output.Write(source)
}

func testThawMessage(t *testing.T) thawMessage {
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
	return thawMessage{
		UnixNano: now.UnixNano(),
		TLSCert:  string(certificate),
		TLSKey:   string(privateKey),
		CABundle: string(certificate),
	}
}

func encodedMessage(t *testing.T, message thawMessage) []byte {
	t.Helper()
	encoded, err := json.Marshal(message)
	if err != nil {
		t.Fatal(err)
	}
	return append(encoded, '\n')
}

func TestReadThawMessageValidatesBoundedSingleLineJSON(t *testing.T) {
	message := testThawMessage(t)
	decoded, err := readThawMessage(bytes.NewReader(encodedMessage(t, message)))
	if err != nil {
		t.Fatal(err)
	}
	if decoded != message {
		t.Fatal("decoded thaw message differs from the request")
	}

	unknownField := strings.TrimSpace(string(encodedMessage(t, message)))
	unknownField = strings.TrimSuffix(unknownField, "}") + `,"unexpected":true}` + "\n"
	tooLarge := strings.Repeat("x", maxThawMessageBytes+1) + "\n"
	withoutNewline := strings.TrimSuffix(string(encodedMessage(t, message)), "\n")
	secondValue := string(encodedMessage(t, message)) + `{"unix_nano":1}` + "\n"
	invalidCertificate := message
	invalidCertificate.TLSCert = "not a certificate"

	for name, input := range map[string][]byte{
		"unknown field":        []byte(unknownField),
		"oversized":            []byte(tooLarge),
		"missing newline":      []byte(withoutNewline),
		"multiple JSON values": []byte(secondValue),
		"invalid certificate":  encodedMessage(t, invalidCertificate),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := readThawMessage(bytes.NewReader(input)); err == nil {
				t.Fatal("accepted invalid thaw input")
			}
		})
	}
}

func TestHandleThawWritesPrivateCredentialsAndPreservesBootID(t *testing.T) {
	message := testThawMessage(t)
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
	ready := false
	readyCalls := 0
	clockCalls := 0
	for range 2 {
		connection := &testConnection{reader: bytes.NewReader(encodedMessage(t, message))}
		if err := handleThaw(
			connection,
			2,
			2,
			"test-boot-id",
			files,
			func(value time.Time) error {
				clockCalls++
				if value.UnixNano() != message.UnixNano {
					t.Fatalf("clock time = %s, want request time", value)
				}
				return nil
			},
			func() error {
				readyCalls++
				return nil
			},
			&ready,
		); err != nil {
			t.Fatal(err)
		}
		var acknowledgement thawAcknowledgement
		if err := json.Unmarshal(bytes.TrimSpace(connection.output.Bytes()), &acknowledgement); err != nil {
			t.Fatal(err)
		}
		if acknowledgement.BootID != "test-boot-id" {
			t.Fatalf("boot ID = %q, want test-boot-id", acknowledgement.BootID)
		}
	}
	if clockCalls != 2 || readyCalls != 1 {
		t.Fatalf("clock calls = %d and readiness calls = %d, want 2 and 1", clockCalls, readyCalls)
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

func TestHandleThawRejectsNonHostPeerBeforeReading(t *testing.T) {
	connection := &testConnection{reader: bytes.NewReader([]byte("invalid"))}
	called := false
	err := handleThaw(
		connection,
		3,
		2,
		"test-boot-id",
		credentialFiles{},
		func(time.Time) error {
			called = true
			return nil
		},
		func() error { return nil },
		new(bool),
	)
	if err == nil {
		t.Fatal("accepted a non-host peer")
	}
	if called {
		t.Fatal("attempted to set the clock for a rejected peer")
	}
	if connection.output.Len() != 0 {
		t.Fatal("wrote an acknowledgement for a rejected peer")
	}
}

var _ io.ReadWriter = (*testConnection)(nil)
