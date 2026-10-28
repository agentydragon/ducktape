package session

import (
	"context"
	"path/filepath"
	"sync"
	"testing"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

func withTestSessionFacilities(t *testing.T, config Config) Config {
	t.Helper()
	var workers sync.WaitGroup
	host := supervision.NewHost(config.Context, config.State, runnerlifecycle.Namespace, &workers, func(err error) {
		if err != nil {
			t.Error(err)
		}
	})
	launch := func(ctx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		return host.Launch(ctx, owner, 0, supervision.NewSubprocessSupervisor(nil), processConfig)
	}
	var err error
	config.Scripts, err = scripts.New(scripts.Config{LaunchProcess: launch})
	if err != nil {
		t.Fatal(err)
	}
	config.BootstrapHost, err = bootstrap.NewHost(bootstrap.Config{}, bootstrap.Dependencies{
		Context: config.Context, State: config.State, Workers: &workers,
		Admission: func(ctx context.Context, _ runnerlifecycle.Desired, work func(context.Context) error) error {
			return work(ctx)
		},
		OnError: func(err error) {
			if err != nil {
				t.Error(err)
			}
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	config.LaunchProcess = launch
	config.EnsureWorkspace = func(_ context.Context, cwd string) (string, error) {
		return workspace.Resolve(cwd, config.WorkspaceRoot)
	}
	config.ResolveModelConfig = func(context.Context, runnerpb.Harness, string) (*int, error) { return nil, nil }
	return config
}

func TestSessionManagerLoadsOneOwnerAndPublishesRecovery(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root := t.TempDir()
	store, err := retained.NewStore(filepath.Join(root, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: root, Model: "test-model",
	}, "echo setup", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("one-session", record); err != nil {
		t.Fatal(err)
	}
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	publishErrors := make(chan error, 16)
	var publicationMu sync.Mutex
	publications := map[string][]runnerpb.SetupState{}
	m, err := NewManager(withTestSessionFacilities(t, Config{
		Context: ctx, State: resources, Store: store, WorkspaceRoot: filepath.Dir(root),
		Destination: func(string) string { return "test-destination" },
		PublishSeal: func(string, *journal.SealState) error { return nil },
		PublishAck:  func(string, retained.Acknowledgement) error { return nil },
		OnError: func(err error) {
			if err != nil {
				publishErrors <- err
			}
		},
		OnChange: func() {
			for _, id := range []string{"one-session", "fresh-session"} {
				input, found, err := ReadInput(ctx, resources, id)
				if err != nil {
					publishErrors <- err
				}
				if found {
					publicationMu.Lock()
					publications[id] = append(publications[id], input.Setup)
					publicationMu.Unlock()
				}
			}
		},
		ReconcileConcurrency: 1,
	}))
	if err != nil {
		t.Fatal(err)
	}
	const loaders = 8
	loaded := make([]*Session, loaders)
	loadErrors := make(chan error, loaders+1)
	var workers sync.WaitGroup
	workers.Add(1)
	go func() {
		defer workers.Done()
		if err := m.Startup(ctx); err != nil {
			loadErrors <- err
		}
	}()
	for i := range loaded {
		workers.Add(1)
		go func() {
			defer workers.Done()
			s, err := m.Load(ctx, "one-session")
			loaded[i] = s
			if err != nil {
				loadErrors <- err
			}
		}()
	}
	workers.Wait()
	close(loadErrors)
	for err := range loadErrors {
		t.Fatal(err)
	}
	for _, session := range loaded[1:] {
		if session != loaded[0] {
			t.Fatal("concurrent durable loads opened different Session owners")
		}
	}
	publicationMu.Lock()
	retainedPublications := append([]runnerpb.SetupState(nil), publications["one-session"]...)
	publicationMu.Unlock()
	if len(retainedPublications) == 0 {
		t.Fatal("retained session was never published")
	}
	for _, setup := range retainedPublications {
		if setup != runnerpb.SetupState_SETUP_STATE_INTERRUPTED {
			t.Fatalf("retained setup was published before recovery: %v", retainedPublications)
		}
	}
	input, found, err := ReadInput(ctx, resources, "one-session")
	if err != nil || !found || input.Setup != runnerpb.SetupState_SETUP_STATE_INTERRUPTED {
		t.Fatalf("recovered setup was not published: %+v, found=%t, err=%v", input, found, err)
	}
	entries, err := loaded[0].journal.Since(ctx, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].GetEvent().GetSetupInterrupted() == nil {
		t.Fatalf("durable setup recovery not recorded: %v", entries)
	}
	freshRecord, err := retained.NewRecord(&runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: root, Model: "test-model",
	}, "echo fresh setup", nil)
	if err != nil {
		t.Fatal(err)
	}
	type loadResult struct {
		session *Session
		err     error
	}
	competingLoad := make(chan loadResult, 1)
	started := make(chan struct{})
	fresh, err := m.ReserveFresh(ctx, "fresh-session", func() error {
		if err := store.Write("fresh-session", freshRecord); err != nil {
			return err
		}
		go func() {
			close(started)
			s, err := m.Load(ctx, "fresh-session")
			competingLoad <- loadResult{s, err}
		}()
		<-started
		select {
		case result := <-competingLoad:
			t.Fatalf("retained load crossed the fresh reservation: %+v", result)
		default:
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	result := <-competingLoad
	if result.err != nil || result.session != fresh {
		t.Fatalf("competing retained load did not share the fresh Session owner: %+v, fresh=%p", result, fresh)
	}
	input, found, err = ReadInput(ctx, resources, "fresh-session")
	if err != nil || !found || input.Setup != runnerpb.SetupState_SETUP_STATE_RUNNING {
		t.Fatalf("fresh setup was not published as running: %+v, found=%t, err=%v", input, found, err)
	}
	waitResult := make(chan error, 1)
	go func() { waitResult <- fresh.waitSetup(ctx) }()
	if err := fresh.Emit(ctx, observation(&eventpb.SetupInterrupted{}), nil); err != nil {
		t.Fatal(err)
	}
	m.publishSession(fresh, false)
	if err := <-waitResult; err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-publishErrors:
		t.Fatal(err)
	default:
	}
	if err := loaded[0].journal.Close(); err != nil {
		t.Fatal(err)
	}
	if err := fresh.journal.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestCodexExitBeforeProcessInstall(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	store, err := retained.NewStore(filepath.Join(root, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CODEX, Cwd: root, Model: "test-model",
	}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("codex-session", record); err != nil {
		t.Fatal(err)
	}
	directory, err := store.Directory("codex-session")
	if err != nil {
		t.Fatal(err)
	}
	j, err := journal.Open(ctx, filepath.Join(directory, "journal.sqlite"), record.EventSourceID)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = j.Close() })
	s := newSession(sessionDependencies{
		ctx: ctx, state: state.WrapCore(namespaced.NewState(inmem.Build)), store: store,
		changed: func() {}, fail: func(err error) { t.Error(err) }, publish: func(*Session, bool) {},
	}, "codex-session", &record, j)
	attachment := s.appServerAttachment()
	s.mu.Lock()
	exitEpoch := s.codexExitEpoch
	s.mu.Unlock()
	// The shared host has selected a process but has not returned it to Session yet.
	if err := attachment.End(ctx, 17); err != nil {
		t.Fatal(err)
	}
	if err := s.installCodexProcess(nil, exitEpoch); err == nil {
		t.Fatal("an exited Codex process was attached after its End callback")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.process != nil || s.codexAppServer != nil || s.processID != "" {
		t.Fatal("the exited process was left attached to the Session")
	}
}
