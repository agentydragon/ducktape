package firecracker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	intentPath  = "/run/agentplane/firecracker-intent/running"
	vmmBinary   = "/usr/local/bin/firecracker"
	apiSocket   = "/work/firecracker.sock"
	vsockSocket = "/work/vsock"
)

type phase string

const (
	starting   phase = "starting"
	running    phase = "running"
	suspending phase = "suspending"
	suspended  phase = "suspended"
	resuming   phase = "resuming"
	failed     phase = "failed"
)

type launcherStatus struct {
	PodUID string `json:"pod_uid"`
	Phase  phase  `json:"phase"`
	Error  string `json:"error,omitempty"`
}

type networkController interface {
	Setup(context.Context) error
	Enable(context.Context) error
	Disable(context.Context) error
}

type virtualMachine interface {
	Done() <-chan struct{}
	ExitError() error
	Close() error
}

// Supervisor owns exactly one continuation. Any uncertain outcome remains fenced
// until an operator replaces the Pod; restarting it never starts a second VM.
type Supervisor struct {
	config       Config
	storage      *Storage
	network      networkController
	mu           sync.RWMutex
	status       launcherStatus
	networkOwned bool
	intent       string
	apiPath      string
	vsockPath    string
	start        func(string, string, string) (virtualMachine, error)
	synchronize  func(context.Context, string, string, string) (*GuestConnection, error)
	reachable    func(context.Context) bool
}

func NewSupervisor(config Config) *Supervisor {
	return &Supervisor{
		config: config, storage: NewStorage(config, vmmBinary),
		network: NewNetwork(config.RelayPort, config.PlatformRoutePorts),
		status:  launcherStatus{PodUID: config.PodUID, Phase: starting},
		intent:  intentPath, apiPath: apiSocket, vsockPath: vsockSocket,
		start:       func(binary, socket, log string) (virtualMachine, error) { return StartVMM(binary, socket, log) },
		synchronize: ConnectGuest, reachable: guestReachable,
	}
}

func (s *Supervisor) setPhase(value phase, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status.Phase = value
	if err != nil {
		s.status.Error = err.Error()
	}
}

func (s *Supervisor) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	status := s.status
	s.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	switch r.URL.Path {
	case "/state":
		if err := json.NewEncoder(w).Encode(status); err != nil {
			slog.Warn("write status", "error", err)
		}
	case "/readyz":
		if status.Phase != running || !s.reachable(r.Context()) {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
		_, _ = w.Write([]byte("{}\n"))
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func readIntent(path string) (bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return false, err
	}
	switch strings.TrimSpace(string(data)) {
	case "true":
		return true, nil
	case "false":
		return false, nil
	default:
		return false, fmt.Errorf("Firecracker intent must be exactly true or false")
	}
}

func guestReachable(ctx context.Context) bool {
	conn, err := (&net.Dialer{Timeout: 500 * time.Millisecond}).DialContext(ctx, "tcp", net.JoinHostPort(GuestV4, "9090"))
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

func waitFor(ctx context.Context, timeout time.Duration, predicate func() (bool, error)) error {
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		ready, err := predicate()
		if err != nil {
			return err
		}
		if ready {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

func (s *Supervisor) Run(ctx context.Context) error {
	if err := os.MkdirAll(s.storage.Work, 0700); err != nil {
		return err
	}
	if err := os.MkdirAll("/work", 0700); err != nil {
		return err
	}
	listener, err := net.Listen("tcp", ":9092")
	if err != nil {
		return err
	}
	server := &http.Server{Handler: s, ReadHeaderTimeout: 5 * time.Second}
	serverDone := make(chan error, 1)
	go func() { serverDone <- server.Serve(listener) }()
	runCtx, cancel := context.WithCancelCause(ctx)
	defer cancel(context.Canceled)
	go func() {
		select {
		case err := <-serverDone:
			cancel(fmt.Errorf("status HTTP server stopped: %w", err))
		case <-runCtx.Done():
		}
	}()
	defer func() { _ = server.Close() }()
	lock, lockErr := s.storage.LockOwner()
	if lockErr == nil {
		defer lock.Close()
	}
	err = lockErr
	if err == nil {
		err = s.runOwned(runCtx)
	}
	if err != nil && ctx.Err() == nil {
		if runCtx.Err() != nil {
			err = context.Cause(runCtx)
		}
		s.setPhase(failed, err)
		slog.Error("Firecracker launcher failed closed", "error", err)
		// Cleanup has a fresh budget even when an HTTP/API operation was cancelled.
		fenceCtx, stop := context.WithTimeout(context.Background(), 20*time.Second)
		if s.networkOwned {
			if fenceErr := s.network.Disable(fenceCtx); fenceErr != nil {
				slog.Error("network fence failed", "error", fenceErr)
			}
		}
		stop()
		// Keep status available and never retry uncertain starts or checkpoint writes.
		select {
		case <-ctx.Done():
		case <-runCtx.Done():
		}
		return err
	}
	return ctx.Err()
}

func (s *Supervisor) runOwned(ctx context.Context) error {
	if _, err := os.Stat(s.storage.ActivePath); err == nil {
		return fmt.Errorf("prior VM continuation has an uncertain outcome")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if _, err := s.storage.ReadManifest(); err != nil {
		return err
	}
	s.networkOwned = true
	if err := s.network.Setup(ctx); err != nil {
		return err
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		intent, err := readIntent(s.intent)
		if err != nil {
			return err
		}
		if !intent {
			s.setPhase(suspended, nil)
			if err := waitFor(ctx, 0, func() (bool, error) { return readIntent(s.intent) }); err != nil {
				return err
			}
		}
		manifest, err := s.storage.ReadManifest()
		if err != nil {
			return err
		}
		if manifest == nil {
			if err := s.storage.Prepare(ctx); err != nil {
				return err
			}
		}
		if err := s.runContinuation(ctx, manifest); err != nil {
			return err
		}
	}
}

func (s *Supervisor) configure(ctx context.Context, api *API) error {
	if err := api.Request(ctx, "PUT", "/machine-config", struct {
		VCPUCount int `json:"vcpu_count"`
		MemoryMiB int `json:"mem_size_mib"`
	}{s.config.VCPUCount, s.config.MemoryMiB}); err != nil {
		return err
	}
	data, err := os.ReadFile(filepath.Join(s.config.BootDirectory, "init"))
	if err != nil {
		return err
	}
	init := strings.TrimSpace(string(data))
	if !filepath.IsAbs(init) || strings.ContainsAny(init, " \t\r\n") {
		return fmt.Errorf("boot init must be one absolute guest path")
	}
	if err := api.Request(ctx, "PUT", "/boot-source", struct {
		Kernel string `json:"kernel_image_path"`
		Initrd string `json:"initrd_path,omitempty"`
		Args   string `json:"boot_args"`
	}{s.storage.Paths["kernel"], s.storage.Paths["initrd"], "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rootwait rootfstype=ext4 rw init=" + init}); err != nil {
		return err
	}
	for index, drive := range []struct {
		name     string
		readonly bool
	}{
		{"rootfs", false},
		{"runner", true},
		{"state", false},
		{"workspace", false},
		{"config", true},
		{"launch", true},
		{"trust", true},
	} {
		if err := api.Request(ctx, "PUT", "/drives/"+drive.name, struct {
			ID       string `json:"drive_id"`
			Path     string `json:"path_on_host"`
			Root     bool   `json:"is_root_device"`
			Readonly bool   `json:"is_read_only"`
		}{drive.name, s.storage.Paths[drive.name], index == 0, drive.readonly}); err != nil {
			return err
		}
	}
	if err := api.Request(ctx, "PUT", "/network-interfaces/eth0", struct {
		ID  string `json:"iface_id"`
		Tap string `json:"host_dev_name"`
		MAC string `json:"guest_mac"`
	}{"eth0", Tap, "06:00:ac:10:00:02"}); err != nil {
		return err
	}
	if err := api.Request(ctx, "PUT", "/vsock", struct {
		CID    int    `json:"guest_cid"`
		Socket string `json:"uds_path"`
	}{3, s.vsockPath}); err != nil {
		return err
	}
	return api.Request(ctx, "PUT", "/actions", struct {
		Action string `json:"action_type"`
	}{"InstanceStart"})
}

func (s *Supervisor) runContinuation(ctx context.Context, manifest *Manifest) (err error) {
	if manifest == nil {
		s.setPhase(starting, nil)
	} else {
		s.setPhase(resuming, nil)
	}
	if err := s.storage.MarkActive(); err != nil {
		return err
	}
	for _, path := range []string{s.apiPath, s.vsockPath} {
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	child, err := s.start(vmmBinary, s.apiPath, filepath.Join(s.storage.Work, "firecracker.log"))
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, child.Close()) }()
	if err := waitFor(ctx, 15*time.Second, func() (bool, error) {
		select {
		case <-child.Done():
			return false, fmt.Errorf("Firecracker exited before API readiness: %v", child.ExitError())
		default:
		}
		_, err := os.Stat(s.apiPath)
		if errors.Is(err, os.ErrNotExist) {
			return false, nil
		}
		return err == nil, err
	}); err != nil {
		return err
	}
	api := NewAPI(s.apiPath, apiTimeout)
	// Snapshot operations include RAM file I/O and fsync, unlike control requests.
	snapshotAPI := NewAPI(s.apiPath, snapshotAPITimeout)
	if manifest == nil {
		if err := s.configure(ctx, api); err != nil {
			return err
		}
	} else {
		if err := s.storage.Restore(ctx, manifest); err != nil {
			return err
		}
		generation := filepath.Join(s.storage.Work, manifest.Generation)
		if err := snapshotAPI.Request(ctx, "PUT", "/snapshot/load", struct {
			State  string `json:"snapshot_path"`
			Memory string `json:"mem_file_path"`
			Resume bool   `json:"resume_vm"`
		}{filepath.Join(generation, "vmstate"), filepath.Join(generation, "memory"), true}); err != nil {
			return err
		}
	}
	expectedBoot := ""
	if manifest != nil {
		expectedBoot = manifest.BootID
	}
	guest, err := s.synchronize(ctx, s.vsockPath, expectedBoot, "/run/agentplane/environment-runtime-tls")
	if err != nil {
		return err
	}
	defer guest.Close()
	bootID := guest.BootID
	slog.Info("guest thaw acknowledged", "boot_id", bootID)
	if err := s.network.Enable(ctx); err != nil {
		return err
	}
	if err := waitFor(ctx, 30*time.Second, func() (bool, error) { return s.reachable(ctx), nil }); err != nil {
		return err
	}
	s.setPhase(running, nil)
	if err := waitFor(ctx, 0, func() (bool, error) {
		select {
		case <-child.Done():
			return false, fmt.Errorf("Firecracker exited while running: %v", child.ExitError())
		default:
		}
		intent, err := readIntent(s.intent)
		return !intent, err
	}); err != nil {
		return err
	}
	s.setPhase(suspending, nil)
	if err := s.network.Disable(ctx); err != nil {
		return err
	}
	guest.Close() // Stop credential transport before the paused snapshot boundary.
	if err := api.Request(ctx, "PATCH", "/vm", struct {
		State string `json:"state"`
	}{"Paused"}); err != nil {
		return err
	}
	capture, err := os.MkdirTemp(s.storage.Work, ".capture-")
	if err != nil {
		return err
	}
	memory, vmstate := filepath.Join(capture, "memory"), filepath.Join(capture, "vmstate")
	if err := snapshotAPI.Request(ctx, "PUT", "/snapshot/create", struct {
		Type   string `json:"snapshot_type"`
		State  string `json:"snapshot_path"`
		Memory string `json:"mem_file_path"`
	}{"Full", vmstate, memory}); err != nil {
		return err
	}
	checkpoint, err := s.storage.Checkpoint(ctx, memory, vmstate, bootID)
	if err != nil {
		return err
	}
	// Close reaps the VMM before committing the observable suspended state.
	if err := child.Close(); err != nil {
		return err
	}
	if err := s.storage.FinishCheckpoint(checkpoint, capture); err != nil {
		return err
	}
	if err := s.storage.ClearActive(); err != nil {
		return err
	}
	s.setPhase(suspended, nil)
	return nil
}
