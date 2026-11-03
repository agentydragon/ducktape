package firecracker

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const maxManagementMessageBytes = 1 << 20

type credentialFiles struct {
	TLSCertPath  string
	TLSKeyPath   string
	CABundlePath string
}

type clockSetter func(time.Time) error

func validateCredentials(request *managementpb.SynchronizeRequest) error {
	if request.GetTlsCert() == "" || request.GetTlsKey() == "" || request.GetCaBundle() == "" {
		return errors.New("tls_cert, tls_key, and ca_bundle must be non-empty")
	}
	if _, err := tls.X509KeyPair([]byte(request.GetTlsCert()), []byte(request.GetTlsKey())); err != nil {
		return fmt.Errorf("parse runtime TLS certificate and key: %w", err)
	}
	trust := x509.NewCertPool()
	if !trust.AppendCertsFromPEM([]byte(request.GetCaBundle())) {
		return errors.New("ca_bundle contains no certificates")
	}
	return nil
}

func readSeed(seedDir string) (*managementpb.SynchronizeRequest, error) {
	paths := []string{"tls.crt", "tls.key", "ca-certificates.crt"}
	values := make([]string, len(paths))
	for index, name := range paths {
		path := filepath.Join(seedDir, name)
		info, err := os.Lstat(path)
		if err != nil {
			return nil, fmt.Errorf("inspect credential seed %q: %w", path, err)
		}
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("credential seed %q is not a regular file", path)
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read credential seed %q: %w", path, err)
		}
		if len(contents) > maxManagementMessageBytes {
			return nil, fmt.Errorf("credential seed %q exceeds %d bytes", path, maxManagementMessageBytes)
		}
		values[index] = string(contents)
	}
	request := &managementpb.SynchronizeRequest{TlsCert: values[0], TlsKey: values[1], CaBundle: values[2]}
	if err := validateCredentials(request); err != nil {
		return nil, fmt.Errorf("validate credential seed: %w", err)
	}
	if proto.Size(request) > maxManagementMessageBytes {
		return nil, fmt.Errorf("credential seed exceeds %d bytes", maxManagementMessageBytes)
	}
	return request, nil
}

func writeAtomic(path string, contents []byte, mode os.FileMode) error {
	directory := filepath.Dir(path)
	file, err := os.CreateTemp(directory, ".agentplane-thaw-*")
	if err != nil {
		return fmt.Errorf("create temporary credential in %q: %w", directory, err)
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if err := file.Chmod(mode); err != nil {
		_ = file.Close()
		return fmt.Errorf("set mode on temporary credential %q: %w", temporary, err)
	}
	if _, err := file.Write(contents); err != nil {
		_ = file.Close()
		return fmt.Errorf("write temporary credential %q: %w", temporary, err)
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("sync temporary credential %q: %w", temporary, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close temporary credential %q: %w", temporary, err)
	}
	if err := os.Rename(temporary, path); err != nil {
		return fmt.Errorf("install credential %q: %w", path, err)
	}
	directoryFile, err := os.Open(directory)
	if err != nil {
		return fmt.Errorf("open credential directory %q: %w", directory, err)
	}
	defer directoryFile.Close()
	if err := directoryFile.Sync(); err != nil {
		return fmt.Errorf("sync credential directory %q: %w", directory, err)
	}
	return nil
}

func applyCredentials(files credentialFiles, request *managementpb.SynchronizeRequest) error {
	if err := writeAtomic(files.TLSCertPath, []byte(request.GetTlsCert()), 0o600); err != nil {
		return err
	}
	if err := writeAtomic(files.TLSKeyPath, []byte(request.GetTlsKey()), 0o600); err != nil {
		return err
	}
	if err := writeAtomic(files.CABundlePath, []byte(request.GetCaBundle()), 0o644); err != nil {
		return err
	}
	return nil
}

type managementServer struct {
	managementpb.UnimplementedGuestManagementServer
	bootID        string
	files         credentialFiles
	setClock      clockSetter
	onFirstSync   func() error
	mu            sync.Mutex
	readyNotified bool
}

func (server *managementServer) GetStatus(context.Context, *managementpb.GetStatusRequest) (*managementpb.GetStatusResponse, error) {
	return &managementpb.GetStatusResponse{BootId: server.bootID}, nil
}

func (server *managementServer) Synchronize(_ context.Context, request *managementpb.SynchronizeRequest) (*managementpb.SynchronizeResponse, error) {
	if request.GetExpectedBootId() != server.bootID {
		return nil, status.Error(codes.FailedPrecondition, "guest boot ID changed before synchronization")
	}
	if proto.Size(request) > maxManagementMessageBytes {
		return nil, status.Errorf(codes.ResourceExhausted, "synchronization message exceeds %d bytes", maxManagementMessageBytes)
	}
	if request.GetUnixNano() <= 0 {
		return nil, status.Error(codes.InvalidArgument, "unix_nano must be positive")
	}
	if err := validateCredentials(request); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}

	server.mu.Lock()
	defer server.mu.Unlock()
	if err := server.setClock(time.Unix(0, request.GetUnixNano())); err != nil {
		return nil, status.Errorf(codes.Internal, "set guest clock: %v", err)
	}
	if err := applyCredentials(server.files, request); err != nil {
		return nil, status.Errorf(codes.Internal, "install runtime credentials: %v", err)
	}
	if !server.readyNotified {
		if err := server.onFirstSync(); err != nil {
			return nil, status.Errorf(codes.Internal, "notify systemd synchronization readiness: %v", err)
		}
		server.readyNotified = true
	}
	return &managementpb.SynchronizeResponse{BootId: server.bootID}, nil
}

func notifySystemdReady() error {
	address := os.Getenv("NOTIFY_SOCKET")
	if address == "" {
		return errors.New("NOTIFY_SOCKET is unset")
	}
	connection, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: address, Net: "unixgram"})
	if err != nil {
		return fmt.Errorf("connect to systemd notify socket %q: %w", address, err)
	}
	defer connection.Close()
	if _, err := connection.Write([]byte("READY=1\nSTATUS=host synchronization accepted; Runtime may start\n")); err != nil {
		return fmt.Errorf("notify systemd readiness: %w", err)
	}
	return nil
}

func serve(ctx context.Context, seedDir string, runtimeTLSDir string, runtimeTrustDir string) error {
	if os.Geteuid() != 0 {
		return errors.New("firecracker guest management agent must run as root")
	}
	if err := os.MkdirAll(runtimeTLSDir, 0o700); err != nil {
		return fmt.Errorf("create private runtime TLS directory: %w", err)
	}
	if err := os.Chmod(runtimeTLSDir, 0o700); err != nil {
		return fmt.Errorf("set private runtime TLS directory mode: %w", err)
	}
	if err := os.MkdirAll(runtimeTrustDir, 0o755); err != nil {
		return fmt.Errorf("create runtime trust directory: %w", err)
	}
	if err := os.Chmod(runtimeTrustDir, 0o755); err != nil {
		return fmt.Errorf("set runtime trust directory mode: %w", err)
	}
	seed, err := readSeed(seedDir)
	if err != nil {
		return err
	}
	files := credentialFiles{
		TLSCertPath:  filepath.Join(runtimeTLSDir, "tls.crt"),
		TLSKeyPath:   filepath.Join(runtimeTLSDir, "tls.key"),
		CABundlePath: filepath.Join(runtimeTrustDir, "ca-certificates.crt"),
	}
	if err := applyCredentials(files, seed); err != nil {
		return fmt.Errorf("install initial credential seed: %w", err)
	}
	bootIDBytes, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return fmt.Errorf("read kernel boot ID: %w", err)
	}
	bootID := strings.TrimSpace(string(bootIDBytes))
	if bootID == "" {
		return errors.New("kernel boot ID is empty")
	}
	return serveVsock(ctx, 52, &managementServer{
		bootID:      bootID,
		files:       files,
		setClock:    setRealtimeClock,
		onFirstSync: notifySystemdReady,
	})
}

func Run(ctx context.Context) error {
	return serve(
		ctx,
		"/run/agentplane/credential-seed",
		"/run/agentplane/environment-runtime-private/environment-runtime-tls",
		"/run/agentplane/environment-runtime-trust",
	)
}
