// Package control dials the companion's local Unix-socket gRPC service.
package control

import (
	"errors"
	"path/filepath"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// Dial uses the filesystem-protected Unix socket, never a network endpoint.
func Dial(socket string) (*grpc.ClientConn, error) {
	if !filepath.IsAbs(socket) {
		return nil, errors.New("executor companion socket must be absolute")
	}
	return grpc.NewClient(
		"unix://"+socket,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDisableRetry(),
		grpc.WithNoProxy(),
	)
}
