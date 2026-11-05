package firecracker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"github.com/agentydragon/ducktape/agentplane/firecracker/vsock"
	"github.com/agentydragon/ducktape/agentplane/firecracker/workloadidentity"
	"github.com/coreos/go-systemd/v22/daemon"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const maxManagementMessageBytes = 1 << 20

type clockSetter func(time.Time) error

type managementServer struct {
	managementpb.UnimplementedGuestManagementServer
	bootID        string
	setClock      clockSetter
	refresh       func(context.Context) error
	onFirstSync   func() error
	mu            sync.Mutex
	readyNotified bool
}

func (server *managementServer) GetStatus(context.Context, *managementpb.GetStatusRequest) (*managementpb.GetStatusResponse, error) {
	return &managementpb.GetStatusResponse{BootId: server.bootID}, nil
}

func (server *managementServer) Synchronize(ctx context.Context, request *managementpb.SynchronizeRequest) (*managementpb.SynchronizeResponse, error) {
	if request.GetExpectedBootId() != server.bootID {
		return nil, status.Error(codes.FailedPrecondition, "guest boot ID changed before synchronization")
	}
	if request.GetUnixNano() <= 0 {
		return nil, status.Error(codes.InvalidArgument, "unix_nano must be positive")
	}
	server.mu.Lock()
	defer server.mu.Unlock()
	if err := server.setClock(time.Unix(0, request.GetUnixNano())); err != nil {
		return nil, status.Errorf(codes.Internal, "set guest clock: %v", err)
	}
	// A restored guest contains its old credential state and stream. A fresh
	// subscription receives the host's current full snapshot before network admission.
	if err := server.refresh(ctx); err != nil {
		return nil, status.Errorf(codes.Unavailable, "refresh Workload API credentials: %v", err)
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
	sent, err := daemon.SdNotify(false, daemon.SdNotifyReady+"\nSTATUS=host synchronization accepted; Runtime may start\n")
	if err != nil {
		return err
	}
	if !sent {
		return errors.New("NOTIFY_SOCKET is unset")
	}
	return nil
}

func Run(ctx context.Context) error {
	if os.Geteuid() != 0 {
		return errors.New("firecracker guest management agent must run as root")
	}
	const directory = "/run/agentplane/environment-runtime-private/environment-runtime-tls"
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	if err := os.Chmod(directory, 0700); err != nil {
		return err
	}
	path := filepath.Join(directory, "workload.sock")
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	local, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	defer local.Close()
	if err := os.Chmod(path, 0600); err != nil {
		return err
	}

	host, err := listenHostOnly(53)
	if err != nil {
		return err
	}
	broker := vsock.AcceptDialer(host)
	defer broker.Close()
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	mirror := workloadidentity.New()
	subscriber := &credentialSubscriber{ctx: runCtx, dial: broker.DialContext, mirror: mirror}

	localServer := grpc.NewServer(grpc.MaxRecvMsgSize(maxManagementMessageBytes))
	workload.RegisterSpiffeWorkloadAPIServer(localServer, mirror)
	localDone := make(chan error, 1)
	go func() { localDone <- localServer.Serve(local) }()
	defer localServer.Stop()

	bootIDBytes, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return err
	}
	bootID := strings.TrimSpace(string(bootIDBytes))
	if bootID == "" {
		return errors.New("kernel boot ID is empty")
	}
	managementDone := make(chan error, 1)
	managementStopped := make(chan struct{})
	go func() {
		defer close(managementStopped)
		managementDone <- serveVsock(runCtx, 52, &managementServer{
			bootID: bootID, setClock: setRealtimeClock,
			refresh: subscriber.Refresh, onFirstSync: notifySystemdReady,
		})
	}()
	defer func() { cancel(); <-managementStopped; subscriber.Close() }()
	select {
	case <-ctx.Done():
		return nil
	case err := <-localDone:
		return fmt.Errorf("local Workload API stopped: %w", err)
	case err := <-managementDone:
		return err
	}
}

// The management process owns this subscription and mirrors it to a root-only
// Unix socket. Unprivileged guest programs cannot bind the privileged vsock
// listener, and cannot obtain credentials by dialing a host endpoint themselves.
type credentialSubscriber struct {
	ctx    context.Context
	dial   func(context.Context, string) (net.Conn, error)
	mirror *workloadidentity.Server
	client *workloadapi.Client
	cancel context.CancelFunc
	done   chan error
}

func (subscriber *credentialSubscriber) Close() {
	if subscriber.cancel != nil {
		subscriber.cancel()
		_ = subscriber.client.Close()
		<-subscriber.done
		subscriber.cancel = nil
	}
}

func (subscriber *credentialSubscriber) Refresh(ctx context.Context) error {
	subscriber.Close()
	client, err := workloadapi.New(
		ctx,
		workloadapi.WithAddr("unix:///firecracker-workload-api"),
		workloadapi.WithDialOptions(grpc.WithContextDialer(subscriber.dial),
			grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(maxManagementMessageBytes))),
	)
	if err != nil {
		return err
	}
	watchCtx, cancel := context.WithCancel(subscriber.ctx)
	subscriber.client, subscriber.cancel, subscriber.done = client, cancel, make(chan error, 1)
	watcher := &credentialWatcher{mirror: subscriber.mirror, first: make(chan error, 1)}
	go func() { subscriber.done <- client.WatchX509Context(watchCtx, watcher) }()
	select {
	case err := <-watcher.first:
		if err != nil {
			subscriber.Close()
		}
		return err
	case <-ctx.Done():
		subscriber.Close()
		return ctx.Err()
	case err := <-subscriber.done:
		// Put the terminal outcome back for Close to join the watcher exactly once.
		subscriber.done <- err
		subscriber.Close()
		return fmt.Errorf("Workload API subscription stopped: %w", err)
	}
}

type credentialWatcher struct {
	mirror *workloadidentity.Server
	first  chan error
	once   sync.Once
}

func (watcher *credentialWatcher) OnX509ContextUpdate(update *workloadapi.X509Context) {
	err := watcher.install(update)
	if err != nil {
		slog.Warn("reject Workload API credential update", "error", err)
	}
	watcher.once.Do(func() { watcher.first <- err })
}

func (watcher *credentialWatcher) OnX509ContextWatchError(err error) {
	if status.Code(err) != codes.Canceled && !errors.Is(err, context.Canceled) {
		slog.Warn("Workload API subscription interrupted", "error", err)
	}
	// go-spiffe reconnects with backoff; an existing valid credential remains
	// usable until its expiry. Boot/thaw still waits for a fresh successful update.
}

func (watcher *credentialWatcher) install(update *workloadapi.X509Context) error {
	if len(update.SVIDs) != 1 {
		return errors.New("expected exactly one Runtime identity")
	}
	svid := update.SVIDs[0]
	cert, key, err := svid.MarshalRaw()
	if err != nil {
		return err
	}
	bundle, err := update.Bundles.GetX509BundleForTrustDomain(svid.ID.TrustDomain())
	if err != nil {
		return err
	}
	var roots []byte
	for _, root := range bundle.X509Authorities() {
		roots = append(roots, root.Raw...)
	}
	return watcher.mirror.Set(&workload.X509SVIDResponse{Svids: []*workload.X509SVID{{
		SpiffeId: svid.ID.String(), X509Svid: cert, X509SvidKey: key, Bundle: roots,
	}}})
}
