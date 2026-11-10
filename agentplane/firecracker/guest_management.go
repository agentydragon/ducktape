package firecracker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/firecracker/workloadidentity"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"github.com/agentydragon/ducktape/agentplane/firecracker/vsock"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	guestManagementPort         = 52
	guestManagementReadyTimeout = 2 * time.Minute
	guestManagementRPCTimeout   = 30 * time.Second
	maxGuestManagementMessage   = 1 << 20
)

// GuestConnection owns credential delivery for one running continuation.
type GuestConnection struct {
	BootID string
	close  func()
	once   sync.Once
}

func (guest *GuestConnection) Close() {
	guest.once.Do(func() {
		if guest.close != nil {
			guest.close()
		}
	})
}

// ConnectGuest verifies the guest boot identity before exposing the host's
// Workload API and obtaining the clock/credential readiness barrier.
func ConnectGuest(ctx context.Context, socketPath, expectedBootID, credentialsDir string) (*GuestConnection, error) {
	connection, err := grpc.NewClient(
		"passthrough:///firecracker-guest-management",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return vsock.Dial(ctx, socketPath, guestManagementPort)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDisableRetry(),
		grpc.WithDefaultCallOptions(
			grpc.MaxCallSendMsgSize(maxGuestManagementMessage),
			grpc.MaxCallRecvMsgSize(maxGuestManagementMessage),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("create guest management gRPC client: %w", err)
	}
	defer connection.Close()

	client := managementpb.NewGuestManagementClient(connection)
	// Readiness includes the kernel and NixOS cold boot. It must not consume the
	// credential update's RPC budget, and only this read waits for connectivity.
	readyCtx, cancelReady := context.WithTimeout(ctx, guestManagementReadyTimeout)
	statusResponse, err := client.GetStatus(readyCtx, &managementpb.GetStatusRequest{}, grpc.WaitForReady(true))
	cancelReady()
	if err != nil {
		return nil, fmt.Errorf("get guest management status: %w", err)
	}
	bootID := statusResponse.GetBootId()
	if bootID == "" {
		return nil, errors.New("guest management returned no boot ID")
	}
	if expectedBootID != "" && bootID != expectedBootID {
		return nil, errors.New("restored guest boot ID differs from checkpoint")
	}

	deliveryCtx, stopDelivery := context.WithCancel(ctx)
	delivery, err := serveGuestCredentials(deliveryCtx, socketPath, credentialsDir)
	if err != nil {
		stopDelivery()
		return nil, err
	}
	success := false
	defer func() {
		if !success {
			stopDelivery()
			delivery()
		}
	}()

	rpcCtx, cancel := context.WithTimeout(ctx, guestManagementRPCTimeout)
	defer cancel()
	response, err := client.Synchronize(rpcCtx, &managementpb.SynchronizeRequest{
		UnixNano:       time.Now().UnixNano(),
		ExpectedBootId: bootID,
	})
	if err != nil {
		return nil, fmt.Errorf("synchronize guest management: %w", err)
	}
	if response.GetBootId() != bootID {
		return nil, errors.New("guest boot ID changed during synchronization")
	}
	success = true
	return &GuestConnection{BootID: bootID, close: func() { stopDelivery(); delivery() }}, nil
}

// The host initiates connections to the root-owned guest port. gRPC client/server
// roles are independent of who establishes the byte stream: the guest subscribes
// to our standard Workload API over each accepted connection.
func serveGuestCredentials(ctx context.Context, socketPath, directory string) (func(), error) {
	source := workloadidentity.New()
	var current Credentials
	reload := func() error {
		value, err := ReadCredentials(directory)
		if err != nil {
			return err
		}
		if value == current {
			return nil
		}
		response, err := workloadidentity.ParsePEM([]byte(value.TLSCert), []byte(value.TLSKey), []byte(value.WorkloadCA))
		if err != nil {
			return err
		}
		if err := source.Set(response); err != nil {
			return err
		}
		current = value
		return nil
	}
	if err := reload(); err != nil {
		return nil, fmt.Errorf("load Workload API credentials: %w", err)
	}
	listener := vsock.DialListener(ctx, func(ctx context.Context) (net.Conn, error) { return vsock.Dial(ctx, socketPath, 53) })
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxGuestManagementMessage))
	workload.RegisterSpiffeWorkloadAPIServer(server, source)
	done := make(chan struct{})
	go func() { defer close(done); _ = server.Serve(listener) }()
	pollingDone := make(chan struct{})
	go func() {
		defer close(pollingDone)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := reload(); err != nil {
					slog.Warn("Workload API projected credentials unavailable", "error", err)
				}
			}
		}
	}()
	return func() { server.Stop(); _ = listener.Close(); <-done; <-pollingDone }, nil
}
