// TLS serving for kubespand's gRPC API.
//
// Watches for secrets.API in COSI state and starts a TLS gRPC listener on the
// configured port. This replaces the need for a separate apid process — kubespand
// directly serves mTLS using the certificates obtained via the trustd CSR flow.
//
// Ref: internal/app/apid/pkg/provider/provider.go (TLS certificate handling)
package api

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"time"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	pemx509 "github.com/siderolabs/crypto/x509"
	"github.com/siderolabs/gen/xslices"
	"github.com/siderolabs/talos/pkg/machinery/resources/secrets"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

// TLSServer watches for secrets.API and serves gRPC with mTLS on a TCP port.
// It provides the same API surface as apid (ReadOnlyState + MachineService).
type TLSServer struct {
	st     state.State
	addr   string
	logger *zap.Logger
}

// NewTLSServer creates a TLS server that will watch for secrets.API and serve
// when certificates become available.
func NewTLSServer(st state.State, addr string, logger *zap.Logger) *TLSServer {
	return &TLSServer{
		st:     st,
		addr:   addr,
		logger: logger,
	}
}

// Run watches for secrets.API and starts the TLS gRPC server.
// Blocks until ctx is cancelled.
func (s *TLSServer) Run(ctx context.Context) error {
	if s.addr == "" {
		return nil
	}

	s.logger.Info("TLS server waiting for secrets.API", zap.String("addr", s.addr))

	apiCerts, err := s.waitForAPICerts(ctx)
	if err != nil {
		return fmt.Errorf("waiting for API certs: %w", err)
	}

	s.logger.Info("TLS server got certificates, starting mTLS listener")

	tlsConfig, err := buildTLSConfig(apiCerts)
	if err != nil {
		return fmt.Errorf("building TLS config: %w", err)
	}

	srv := grpc.NewServer(grpc.Creds(credentials.NewTLS(tlsConfig)))
	v1alpha1.RegisterStateServer(srv, NewReadOnlyState(s.st))
	RegisterMachineService(srv)

	lis, err := net.Listen("tcp", s.addr)
	if err != nil {
		return fmt.Errorf("listening on %s: %w", s.addr, err)
	}

	s.logger.Info("TLS server listening", zap.String("addr", s.addr))

	go func() {
		<-ctx.Done()
		s.logger.Info("TLS server shutting down")
		srv.GracefulStop()
	}()

	if err := srv.Serve(lis); err != nil {
		return fmt.Errorf("serving TLS gRPC: %w", err)
	}

	return nil
}

// waitForAPICerts polls the COSI state for secrets.API until it appears.
func (s *TLSServer) waitForAPICerts(ctx context.Context) (*secrets.API, error) {
	for {
		apiCerts, err := safe.StateGetByID[*secrets.API](ctx, s.st, secrets.APIID)
		if err == nil {
			return apiCerts, nil
		}

		s.logger.Debug("secrets.API not yet available, retrying", zap.Error(err))

		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(1 * time.Second):
		}
	}
}

// buildTLSConfig creates a *tls.Config for mutual TLS from secrets.API.
// Ref: internal/app/apid/pkg/provider/provider.go certificateProvider.Update
func buildTLSConfig(apiCerts *secrets.API) (*tls.Config, error) {
	spec := apiCerts.TypedSpec()

	serverCert, err := tls.X509KeyPair(spec.Server.Crt, spec.Server.Key)
	if err != nil {
		return nil, fmt.Errorf("parsing server cert: %w", err)
	}

	caPEM := bytes.Join(
		xslices.Map(
			spec.AcceptedCAs,
			func(cert *pemx509.PEMEncodedCertificate) []byte {
				return cert.Crt
			},
		),
		nil,
	)

	caPool := x509.NewCertPool()
	if !caPool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("failed to parse CA certs")
	}

	return &tls.Config{
		Certificates: []tls.Certificate{serverCert},
		ClientCAs:    caPool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS12,
	}, nil
}
