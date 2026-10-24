package sandboxes

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"

	"k8s.io/client-go/rest"
	"kubevirt.io/client-go/kubecli"
	kvcorev1 "kubevirt.io/client-go/kubevirt/typed/core/v1"
)

// ConsoleUnavailableError means Kubernetes refused an attach because this Sandbox has no
// attachable VM console, for example because it is a Pod Sandbox or its console is already held.
type ConsoleUnavailableError struct {
	Name   string
	Status int
}

func (e *ConsoleUnavailableError) Error() string {
	return fmt.Sprintf("sandbox %s has no attachable VM console: Kubernetes answered %d", e.Name, e.Status)
}

// ConsoleSource opens a Sandbox VM's output-only KubeVirt console stream.
type ConsoleSource interface {
	Open(ctx context.Context, namespace, name string) (io.ReadCloser, error)
}

type serialConsoleOpener func(namespace, name string) (kvcorev1.StreamInterface, error)

// ConsoleConnector connects through KubeVirt's supported typed client. The typed client applies
// the Kubernetes transport configuration and ServiceAccount credentials to the subresource.
type ConsoleConnector struct{ open serialConsoleOpener }

func NewConsoleConnector(config *rest.Config) (*ConsoleConnector, error) {
	if config == nil {
		return nil, errors.New("Kubernetes REST config is required")
	}
	client, err := kubecli.GetKubevirtClientFromRESTConfig(config)
	if err != nil {
		return nil, fmt.Errorf("configure KubeVirt console client: %w", err)
	}
	return &ConsoleConnector{open: func(namespace, name string) (kvcorev1.StreamInterface, error) {
		return client.VirtualMachineInstance(namespace).SerialConsole(name, nil)
	}}, nil
}

func (c *ConsoleConnector) Open(ctx context.Context, namespace, name string) (io.ReadCloser, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if c == nil || c.open == nil || namespace == "" || name == "" {
		return nil, errors.New("KubeVirt console connector, namespace and name are required")
	}
	connection, err := c.open(namespace, name)
	if err != nil {
		var statusError interface{ GetStatusCode() int }
		if errors.As(err, &statusError) && statusError.GetStatusCode() != 0 {
			return nil, &ConsoleUnavailableError{Name: name, Status: statusError.GetStatusCode()}
		}
		return nil, err
	}
	if connection == nil {
		return nil, errors.New("KubeVirt console client returned an empty stream")
	}
	if err := ctx.Err(); err != nil {
		_ = connection.AsConn().Close()
		return nil, err
	}
	input, inputWriter := io.Pipe()
	output, outputWriter := io.Pipe()
	streamDone := make(chan struct{})
	go func() {
		streamErr := connection.Stream(kvcorev1.StreamOptions{In: input, Out: outputWriter})
		_ = outputWriter.CloseWithError(streamErr)
		_ = input.Close()
		close(streamDone)
	}()
	var closeOnce sync.Once
	closeStream := func() error {
		var closeErr error
		closeOnce.Do(func() {
			_ = inputWriter.Close()
			_ = output.Close()
			closeErr = connection.AsConn().Close()
		})
		return closeErr
	}
	go func() {
		select {
		case <-ctx.Done():
			_ = closeStream()
		case <-streamDone:
		}
	}()
	return &consoleReader{ReadCloser: output, closeStream: closeStream}, nil
}

type consoleReader struct {
	io.ReadCloser
	closeStream func() error
}

func (r *consoleReader) Close() error {
	err := r.ReadCloser.Close()
	if closeErr := r.closeStream(); err == nil {
		err = closeErr
	}
	return err
}
