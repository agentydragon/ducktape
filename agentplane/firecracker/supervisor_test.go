package firecracker

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
)

type eventLog struct {
	mu      sync.Mutex
	entries []string
}

func (l *eventLog) add(value string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.entries = append(l.entries, value)
}

func (l *eventLog) snapshot() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.entries...)
}

type testNetwork struct{ events *eventLog }

func (n testNetwork) Setup(context.Context) error   { n.events.add("setup"); return nil }
func (n testNetwork) Enable(context.Context) error  { n.events.add("enable"); return nil }
func (n testNetwork) Disable(context.Context) error { n.events.add("disable"); return nil }

type testVMM struct {
	done  chan struct{}
	once  sync.Once
	close func()
}

func TestConfigureOptionalInitrd(t *testing.T) {
	for _, withInitrd := range []bool{false, true} {
		t.Run(map[bool]string{false: "built-in drivers", true: "modular drivers"}[withInitrd], func(t *testing.T) {
			storage := testStorage(t)
			if err := os.WriteFile(filepath.Join(storage.config.BootDirectory, "init"), []byte("/usr/lib/systemd/systemd\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			if withInitrd {
				storage.Paths["initrd"] = filepath.Join(storage.liveDirectory, "initramfs.cpio")
			}
			s := NewSupervisor(storage.config)
			s.storage = storage
			var boot map[string]string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/boot-source" {
					if err := json.NewDecoder(r.Body).Decode(&boot); err != nil {
						t.Error(err)
					}
				}
				w.WriteHeader(http.StatusNoContent)
			}))
			defer server.Close()
			client := server.Client()
			client.Transport = &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", server.Listener.Addr().String())
			}}
			defer client.CloseIdleConnections()
			if err := s.configure(context.Background(), &API{client: client}); err != nil {
				t.Fatal(err)
			}
			initrd, present := boot["initrd_path"]
			if present != withInitrd || initrd != storage.Paths["initrd"] {
				t.Fatalf("initrd_path = %q (present %v), wanted %q (present %v)", initrd, present, storage.Paths["initrd"], withInitrd)
			}
			if boot["kernel_image_path"] != storage.Paths["kernel"] {
				t.Fatalf("unexpected boot source: %v", boot)
			}
		})
	}
}

func (v *testVMM) Done() <-chan struct{} { return v.done }
func (v *testVMM) ExitError() error      { return nil }
func (v *testVMM) Close() error          { v.once.Do(func() { v.close(); close(v.done) }); return nil }

func TestContinuationCheckpointOrderAndFailureFence(t *testing.T) {
	for _, syncFailure := range []bool{false, true} {
		t.Run(map[bool]string{false: "checkpoint", true: "identity failure"}[syncFailure], func(t *testing.T) {
			storage := testStorage(t)
			var events eventLog
			s := NewSupervisor(storage.config)
			s.storage = storage
			s.network = testNetwork{&events}
			s.apiPath = filepath.Join(t.TempDir(), "api.sock")
			s.intent = filepath.Join(t.TempDir(), "intent")
			s.reachable = func(context.Context) bool { return true }
			if err := os.MkdirAll(storage.config.BootDirectory, 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(storage.config.BootDirectory, "init"), []byte("/init"), 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(s.intent, []byte("false"), 0600); err != nil {
				t.Fatal(err)
			}
			s.synchronize = func(context.Context, string, string, string) (*GuestConnection, error) {
				events.add("thaw")
				if syncFailure {
					return nil, errors.New("guest identity mismatch")
				}
				return &GuestConnection{BootID: "boot-test", close: func() { events.add("credentials-stop") }}, nil
			}
			s.start = func(binary, socket, log string) (virtualMachine, error) {
				if _, err := os.Stat(storage.ActivePath); err != nil {
					t.Fatalf("VMM starts without durable active marker: %v", err)
				}
				listener, err := net.Listen("unix", socket)
				if err != nil {
					return nil, err
				}
				server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					switch r.URL.Path {
					case "/vm":
						events.add("pause")
					case "/snapshot/create":
						events.add("capture")
						var payload struct {
							Memory string `json:"mem_file_path"`
							State  string `json:"snapshot_path"`
						}
						if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
							t.Error(err)
							w.WriteHeader(400)
							return
						}
						for _, path := range []string{payload.Memory, payload.State} {
							if err := os.WriteFile(path, []byte("snapshot"), 0600); err != nil {
								t.Error(err)
								w.WriteHeader(500)
								return
							}
						}
					}
					w.WriteHeader(204)
				})}
				go func() { _ = server.Serve(listener) }()
				return &testVMM{done: make(chan struct{}), close: func() {
					if !syncFailure {
						if _, err := os.Stat(storage.ManifestPath); err != nil {
							t.Errorf("VMM exits before checkpoint committed: %v", err)
						}
					}
					if _, err := os.Stat(storage.ActivePath); err != nil {
						t.Errorf("active marker cleared before VMM exit: %v", err)
					}
					events.add("vmm-exit")
					_ = server.Close()
				}}, nil
			}
			err := s.runContinuation(context.Background(), nil)
			if syncFailure {
				if err == nil {
					t.Fatal("identity failure admitted")
				}
				if !reflect.DeepEqual(events.snapshot(), []string{"thaw", "vmm-exit"}) {
					t.Fatal(events.snapshot())
				}
				if _, err := os.Stat(storage.ActivePath); err != nil {
					t.Fatalf("uncertain outcome lost its fence: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(events.snapshot(), []string{"thaw", "enable", "disable", "credentials-stop", "pause", "capture", "vmm-exit"}) {
				t.Fatal(events.snapshot())
			}
			if _, err := os.Stat(storage.ActivePath); !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("completed checkpoint remains active: %v", err)
			}
			manifest, err := storage.ReadManifest()
			if err != nil || manifest == nil || manifest.BootID != "boot-test" {
				t.Fatalf("checkpoint: %+v, %v", manifest, err)
			}
			if s.status.Phase != suspended {
				t.Fatal(s.status)
			}
		})
	}
}

func TestIntentAndStatus(t *testing.T) {
	path := filepath.Join(t.TempDir(), "intent")
	for _, value := range []string{"true", "false", "", "TRUE", "1"} {
		if err := os.WriteFile(path, []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
		intent, err := readIntent(path)
		if value == "true" || value == "false" {
			if err != nil || intent != (value == "true") {
				t.Fatalf("%q: %v %v", value, intent, err)
			}
		} else if err == nil {
			t.Fatalf("accepted %q", value)
		}
	}
	s := NewSupervisor(Config{PodUID: "pod-test"})
	s.setPhase(suspended, nil)
	s.reachable = func(context.Context) bool { t.Fatal("probed suspended guest"); return true }
	response := httptest.NewRecorder()
	s.ServeHTTP(response, httptest.NewRequest("GET", "/readyz", nil))
	if response.Code != 503 {
		t.Fatal(response.Code)
	}
	response = httptest.NewRecorder()
	s.ServeHTTP(response, httptest.NewRequest("GET", "/state", nil))
	var state launcherStatus
	if err := json.Unmarshal(response.Body.Bytes(), &state); err != nil {
		t.Fatal(err)
	}
	if state.PodUID != "pod-test" || state.Phase != suspended {
		t.Fatal(state)
	}
}

func TestUncertainContinuationCannotColdBoot(t *testing.T) {
	storage := testStorage(t)
	if err := storage.MarkActive(); err != nil {
		t.Fatal(err)
	}
	s := NewSupervisor(storage.config)
	s.storage = storage
	var events eventLog
	s.network = testNetwork{&events}
	s.start = func(string, string, string) (virtualMachine, error) {
		t.Fatal("replayed an uncertain VM start")
		return nil, nil
	}
	if err := s.runOwned(t.Context()); err == nil {
		t.Fatal("uncertain continuation was accepted")
	}
	if len(events.snapshot()) != 0 {
		t.Fatal("uncertain continuation changed networking")
	}
	if _, err := os.Stat(storage.ActivePath); err != nil {
		t.Fatal("uncertain continuation lost its active marker", err)
	}
}
