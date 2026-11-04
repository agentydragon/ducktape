package firecracker

import (
	"context"
	"errors"
	"fmt"
	"net"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"github.com/agentydragon/ducktape/agentplane/firecracker/vsock"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	guestManagementPort       = 52
	guestManagementRPCTimeout = 30 * time.Second
	maxGuestManagementMessage = 1 << 20
)

// SynchronizeGuest reads one coherent TLS credential generation, checks the
// guest identity, then sends one synchronization request over Firecracker vsock.
func SynchronizeGuest(ctx context.Context, socketPath, expectedBootID, credentialsDir string) (string, error) {
	credentials, err := ReadCredentials(credentialsDir)
	if err != nil {
		return "", fmt.Errorf("read guest management credentials: %w", err)
	}

	rpcCtx, cancel := context.WithTimeout(ctx, guestManagementRPCTimeout)
	defer cancel()

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
		return "", fmt.Errorf("create guest management gRPC client: %w", err)
	}
	defer connection.Close()

	client := managementpb.NewGuestManagementClient(connection)
	statusResponse, err := client.GetStatus(rpcCtx, &managementpb.GetStatusRequest{}, grpc.WaitForReady(true))
	if err != nil {
		return "", fmt.Errorf("get guest management status: %w", err)
	}
	bootID := statusResponse.GetBootId()
	if bootID == "" {
		return "", errors.New("guest management returned no boot ID")
	}
	if expectedBootID != "" && bootID != expectedBootID {
		return "", errors.New("restored guest boot ID differs from checkpoint")
	}

	response, err := client.Synchronize(rpcCtx, &managementpb.SynchronizeRequest{
		UnixNano:       time.Now().UnixNano(),
		TlsCert:        credentials.TLSCert,
		TlsKey:         credentials.TLSKey,
		CaBundle:       credentials.CABundle,
		ExpectedBootId: bootID,
	})
	if err != nil {
		return "", fmt.Errorf("synchronize guest management: %w", err)
	}
	if response.GetBootId() != bootID {
		return "", errors.New("guest boot ID changed during synchronization")
	}
	return bootID, nil
}
