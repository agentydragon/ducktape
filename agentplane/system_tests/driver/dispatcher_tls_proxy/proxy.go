package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/netip"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type dispatcherProxy struct {
	listener         net.Listener
	server           *grpc.Server
	upstream         *grpc.ClientConn
	rootCertificates []byte
}

func newDispatcherProxy(target, caFile string) (*dispatcherProxy, error) {
	if err := validatePortForwardTarget(target); err != nil {
		return nil, err
	}
	clientTLS := &environment_runtime_transport.ClientTLS{
		CAFile:    caFile,
		ServerURI: environment_runtime_transport.SessionDispatcherURI,
	}
	upstreamCredentials, err := clientTLS.Credentials()
	if err != nil {
		return nil, fmt.Errorf("configure verified Dispatcher TLS: %w", err)
	}
	upstream, err := grpc.NewClient(
		"passthrough:///"+target,
		grpc.WithTransportCredentials(upstreamCredentials),
	)
	if err != nil {
		return nil, fmt.Errorf("create Dispatcher client: %w", err)
	}

	localTLS, rootCertificates, err := newLocalTLS()
	if err != nil {
		_ = upstream.Close()
		return nil, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		_ = upstream.Close()
		return nil, fmt.Errorf("listen for local Dispatcher client: %w", err)
	}
	forwarder := &dispatcherForwarder{upstream: upstream}
	server := grpc.NewServer(
		grpc.Creds(credentials.NewTLS(localTLS)),
		grpc.ForceServerCodec(rawCodec{}),
		grpc.UnknownServiceHandler(forwarder.forward),
	)
	return &dispatcherProxy{
		listener:         listener,
		server:           server,
		upstream:         upstream,
		rootCertificates: rootCertificates,
	}, nil
}

func (p *dispatcherProxy) close() {
	p.server.Stop()
	_ = p.listener.Close()
	_ = p.upstream.Close()
}

func validatePortForwardTarget(target string) error {
	host, portText, err := net.SplitHostPort(target)
	if err != nil {
		return fmt.Errorf("invalid port-forward target %q: %w", target, err)
	}
	ip, err := netip.ParseAddr(host)
	if err != nil || !ip.IsLoopback() {
		return fmt.Errorf("port-forward target host %q must be a loopback IP", host)
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		return fmt.Errorf("port-forward target port %q is invalid", portText)
	}
	return nil
}

func newLocalTLS() (*tls.Config, []byte, error) {
	now := time.Now()
	rootKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generate local proxy CA key: %w", err)
	}
	rootSerial, err := randomSerial()
	if err != nil {
		return nil, nil, fmt.Errorf("generate local proxy CA serial: %w", err)
	}
	rootTemplate := &x509.Certificate{
		SerialNumber:          rootSerial,
		Subject:               pkix.Name{CommonName: "Agentplane live test local CA"},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
		MaxPathLen:            0,
	}
	rootDER, err := x509.CreateCertificate(rand.Reader, rootTemplate, rootTemplate, &rootKey.PublicKey, rootKey)
	if err != nil {
		return nil, nil, fmt.Errorf("create local proxy CA: %w", err)
	}
	root, err := x509.ParseCertificate(rootDER)
	if err != nil {
		return nil, nil, fmt.Errorf("parse local proxy CA: %w", err)
	}

	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generate local proxy leaf key: %w", err)
	}
	leafSerial, err := randomSerial()
	if err != nil {
		return nil, nil, fmt.Errorf("generate local proxy leaf serial: %w", err)
	}
	leafTemplate := &x509.Certificate{
		SerialNumber:          leafSerial,
		Subject:               pkix.Name{CommonName: "localhost"},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:              []string{"localhost"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")},
		BasicConstraintsValid: true,
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTemplate, root, &leafKey.PublicKey, rootKey)
	if err != nil {
		return nil, nil, fmt.Errorf("create local proxy leaf: %w", err)
	}
	leafKeyDER, err := x509.MarshalECPrivateKey(leafKey)
	if err != nil {
		return nil, nil, fmt.Errorf("marshal local proxy leaf key: %w", err)
	}
	chain := append(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}),
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: rootDER})...,
	)
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: leafKeyDER})
	certificate, err := tls.X509KeyPair(chain, keyPEM)
	if err != nil {
		return nil, nil, fmt.Errorf("load local proxy certificate: %w", err)
	}
	rootPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: rootDER})
	return &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{certificate}}, rootPEM, nil
}

func randomSerial() (*big.Int, error) {
	return rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
}

type dispatcherForwarder struct {
	upstream *grpc.ClientConn
}

func (f *dispatcherForwarder) forward(_ any, downstream grpc.ServerStream) error {
	method, okay := grpc.MethodFromServerStream(downstream)
	if !okay {
		return status.Error(codes.Internal, "gRPC method is unavailable")
	}
	incoming, _ := metadata.FromIncomingContext(downstream.Context())
	outgoing := metadata.MD{}
	for key, values := range incoming {
		if strings.HasPrefix(key, "grpc-") {
			continue
		}
		outgoing[key] = append([]string(nil), values...)
	}

	ctx, cancel := context.WithCancel(metadata.NewOutgoingContext(downstream.Context(), outgoing))
	defer cancel()
	upstream, err := f.upstream.NewStream(
		ctx,
		&grpc.StreamDesc{ClientStreams: true, ServerStreams: true},
		method,
		grpc.ForceCodec(rawCodec{}),
	)
	if err != nil {
		return err
	}
	requestDone := make(chan error, 1)
	go func() {
		for {
			var request []byte
			if err := downstream.RecvMsg(&request); err != nil {
				if errors.Is(err, io.EOF) {
					requestDone <- upstream.CloseSend()
					return
				}
				requestDone <- err
				cancel()
				return
			}
			if err := upstream.SendMsg(request); err != nil {
				requestDone <- err
				cancel()
				return
			}
		}
	}()

	header, err := upstream.Header()
	if err != nil {
		downstream.SetTrailer(upstream.Trailer())
		return err
	}
	if err := downstream.SendHeader(header); err != nil {
		return err
	}
	for {
		var response []byte
		if err := upstream.RecvMsg(&response); err != nil {
			downstream.SetTrailer(upstream.Trailer())
			if errors.Is(err, io.EOF) {
				select {
				case requestErr := <-requestDone:
					if requestErr != nil {
						return requestErr
					}
				default:
				}
				return nil
			}
			return err
		}
		if err := downstream.SendMsg(response); err != nil {
			return err
		}
	}
}

type rawCodec struct{}

func (rawCodec) Marshal(value any) ([]byte, error) {
	message, okay := value.([]byte)
	if !okay {
		return nil, fmt.Errorf("raw gRPC codec cannot marshal %T", value)
	}
	return message, nil
}

func (rawCodec) Unmarshal(data []byte, value any) error {
	message, okay := value.(*[]byte)
	if !okay || message == nil {
		return fmt.Errorf("raw gRPC codec cannot unmarshal into %T", value)
	}
	*message = append((*message)[:0], data...)
	return nil
}

func (rawCodec) Name() string { return "proto" }
