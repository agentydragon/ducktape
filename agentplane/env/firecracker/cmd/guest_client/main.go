package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

const (
	maxRequestBytes = 1 << 20
	vsockPort       = 52
)

func readSynchronizeRequest(reader io.Reader) (*managementpb.SynchronizeRequest, error) {
	encoded, err := io.ReadAll(io.LimitReader(reader, maxRequestBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read SynchronizeRequest from stdin: %w", err)
	}
	if len(encoded) > maxRequestBytes {
		return nil, fmt.Errorf("SynchronizeRequest exceeds %d bytes", maxRequestBytes)
	}
	request := new(managementpb.SynchronizeRequest)
	if err := proto.Unmarshal(encoded, request); err != nil {
		return nil, fmt.Errorf("decode SynchronizeRequest from stdin: %w", err)
	}
	if proto.Size(request) > maxRequestBytes {
		return nil, fmt.Errorf("SynchronizeRequest exceeds %d bytes", maxRequestBytes)
	}
	return request, nil
}

func dialVsock(ctx context.Context, socketPath string, port uint32) (net.Conn, error) {
	connection, err := (&net.Dialer{}).DialContext(ctx, "unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("connect to Firecracker vsock proxy %q: %w", socketPath, err)
	}
	if deadline, ok := ctx.Deadline(); ok {
		if err := connection.SetDeadline(deadline); err != nil {
			_ = connection.Close()
			return nil, fmt.Errorf("set Firecracker vsock handshake deadline: %w", err)
		}
	}
	if _, err := fmt.Fprintf(connection, "CONNECT %d\n", port); err != nil {
		_ = connection.Close()
		return nil, fmt.Errorf("request Firecracker vsock port %d: %w", port, err)
	}
	var response strings.Builder
	for range 128 {
		var next [1]byte
		if _, err := io.ReadFull(connection, next[:]); err != nil {
			_ = connection.Close()
			return nil, fmt.Errorf("read Firecracker vsock handshake: %w", err)
		}
		response.WriteByte(next[0])
		if next[0] == '\n' {
			line := response.String()
			if !strings.HasPrefix(line, "OK ") {
				_ = connection.Close()
				return nil, fmt.Errorf("Firecracker vsock rejected port %d: %q", port, strings.TrimSpace(line))
			}
			if err := connection.SetDeadline(time.Time{}); err != nil {
				_ = connection.Close()
				return nil, fmt.Errorf("clear Firecracker vsock handshake deadline: %w", err)
			}
			return connection, nil
		}
	}
	_ = connection.Close()
	return nil, errors.New("Firecracker vsock handshake exceeded 128 bytes")
}

func synchronize(ctx context.Context, socketPath string, request *managementpb.SynchronizeRequest) (*managementpb.SynchronizeResponse, error) {
	connection, err := grpc.NewClient(
		"passthrough:///agentplane-firecracker-guest",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return dialVsock(ctx, socketPath, vsockPort)
		}),
	)
	if err != nil {
		return nil, fmt.Errorf("create guest management gRPC client: %w", err)
	}
	defer connection.Close()
	client := managementpb.NewGuestManagementClient(connection)
	status, err := client.GetStatus(ctx, &managementpb.GetStatusRequest{}, grpc.WaitForReady(true))
	if err != nil {
		return nil, fmt.Errorf("get guest status: %w", err)
	}
	if status.GetBootId() == "" {
		return nil, errors.New("guest returned an empty boot ID")
	}
	if request.GetExpectedBootId() != "" && request.GetExpectedBootId() != status.GetBootId() {
		return nil, errors.New("guest boot ID changed since the caller prepared synchronization")
	}
	request.UnixNano = time.Now().UnixNano()
	request.ExpectedBootId = status.GetBootId()
	response, err := client.Synchronize(ctx, request)
	if err != nil {
		return nil, fmt.Errorf("synchronize guest: %w", err)
	}
	if response.GetBootId() != status.GetBootId() {
		return nil, errors.New("guest boot ID changed during synchronization")
	}
	return response, nil
}

func run(arguments []string, stdin io.Reader, stdout io.Writer) error {
	flags := flag.NewFlagSet("agentplane-firecracker-guest-client", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	socketPath := flags.String("socket", "", "Firecracker vsock proxy Unix socket")
	timeout := flags.Duration("timeout", 30*time.Second, "guest management RPC timeout")
	if err := flags.Parse(arguments); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments: %q", flags.Args())
	}
	if *socketPath == "" {
		return errors.New("--socket is required")
	}
	if *timeout <= 0 {
		return errors.New("--timeout must be positive")
	}
	request, err := readSynchronizeRequest(stdin)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	response, err := synchronize(ctx, *socketPath, request)
	if err != nil {
		return err
	}
	encoded, err := proto.Marshal(response)
	if err != nil {
		return fmt.Errorf("encode SynchronizeResponse: %w", err)
	}
	if _, err := stdout.Write(encoded); err != nil {
		return fmt.Errorf("write SynchronizeResponse to stdout: %w", err)
	}
	return nil
}

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "Firecracker guest client failed: %v\n", err)
		os.Exit(1)
	}
}
