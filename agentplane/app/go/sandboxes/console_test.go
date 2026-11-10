package sandboxes

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"testing"

	kvcorev1 "kubevirt.io/client-go/kubevirt/typed/core/v1"
)

type fakeConsoleStream struct {
	output []byte
	conn   net.Conn
}

func (s *fakeConsoleStream) Stream(options kvcorev1.StreamOptions) error {
	_, err := options.Out.Write(s.output)
	return err
}

func (s *fakeConsoleStream) AsConn() net.Conn { return s.conn }

type fakeConsoleStatusError struct{ status int }

func (e fakeConsoleStatusError) Error() string      { return "websocket refused" }
func (e fakeConsoleStatusError) GetStatusCode() int { return e.status }

func TestConsoleConnectorStreamsBytesFromTypedClient(t *testing.T) {
	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() { _ = serverConn.Close() })
	wantOutput := []byte("console output\x00")
	var gotNamespace, gotName string
	connector := &ConsoleConnector{open: func(namespace, name string) (kvcorev1.StreamInterface, error) {
		gotNamespace, gotName = namespace, name
		return &fakeConsoleStream{output: wantOutput, conn: clientConn}, nil
	}}
	stream, err := connector.Open(t.Context(), "sandbox-ns", "sandbox-vmi")
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()
	gotOutput, err := io.ReadAll(stream)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(gotOutput, wantOutput) || gotNamespace != "sandbox-ns" || gotName != "sandbox-vmi" {
		t.Fatalf("console output=%q namespace=%q name=%q", gotOutput, gotNamespace, gotName)
	}
}

func TestConsoleConnectorReportsKubeVirtRefusalAsUnavailable(t *testing.T) {
	connector := &ConsoleConnector{open: func(string, string) (kvcorev1.StreamInterface, error) {
		return nil, fakeConsoleStatusError{status: 409}
	}}
	_, err := connector.Open(t.Context(), "sandbox-ns", "sandbox-vmi")
	var unavailable *ConsoleUnavailableError
	if !errors.As(err, &unavailable) || unavailable.Status != 409 ||
		unavailable.Error() != "sandbox sandbox-vmi has no attachable VM console: Kubernetes answered 409" {
		t.Fatalf("console refusal = %v", err)
	}
}

func TestConsoleConnectorHonorsCanceledContextBeforeAttach(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	opened := false
	connector := &ConsoleConnector{open: func(string, string) (kvcorev1.StreamInterface, error) {
		opened = true
		return nil, nil
	}}
	if _, err := connector.Open(ctx, "sandbox-ns", "sandbox-vmi"); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled console attach = %v", err)
	}
	if opened {
		t.Fatal("canceled attach reached KubeVirt")
	}
}
