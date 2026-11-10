package session

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/workspace"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

func startSessionOwner(t *testing.T, ctx context.Context, resources state.State, manager *Manager) {
	t.Helper()
	engine, err := cosiruntime.NewRuntime(resources, zap.L())
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(manager.OwnerController()); err != nil {
		t.Fatal(err)
	}
	engineCtx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- engine.Run(engineCtx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
			t.Errorf("Session owner engine: %v", err)
		}
	})
}

func TestSessionOwnerReconcilesRetainedRecord(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: root, Model: "test-model",
	}, "echo interrupted", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("retained", record); err != nil {
		t.Fatal(err)
	}
	failures := make(chan error, 1)
	m, err := NewManager(withTestSessionFacilities(t, Config{
		Context: ctx, State: resources, Store: store, WorkspaceRoot: filepath.Dir(root),
		OnChange: func() {}, OnError: func(err error) {
			if err != nil {
				failures <- err
			}
		},
		ReconcileConcurrency: 1,
	}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := m.Close(); err != nil {
			t.Error(err)
		}
	})
	startSessionOwner(t, ctx, resources, m)
	if err := m.Startup(ctx); err != nil {
		t.Fatal(err)
	}
	s, err := m.Wait(ctx, "retained")
	if err != nil {
		t.Fatal(err)
	}
	owner, found, err := ReadOwnerStatus(ctx, resources, "retained")
	if err != nil || !found || !owner.Ready || owner.Error != "" {
		t.Fatalf("owner status = %+v, found %v, err %v", owner, found, err)
	}
	entries, err := s.journal.Since(ctx, 0, 10)
	if err != nil || len(entries) != 1 || entries[0].GetEvent().GetSetupInterrupted() == nil {
		t.Fatalf("retained setup recovery = %v, err %v", entries, err)
	}
	select {
	case err := <-failures:
		t.Fatalf("Session owner failed: %v", err)
	default:
	}
}

func TestSessionOwnerReportsRecoveryFailure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: filepath.Dir(root), Model: "test-model",
	}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("invalid", record); err != nil {
		t.Fatal(err)
	}
	failures := make(chan error, 1)
	m, err := NewManager(withTestSessionFacilities(t, Config{
		Context: ctx, State: resources, Store: store, WorkspaceRoot: root,
		OnChange: func() {}, OnError: func(err error) {
			if err != nil {
				failures <- err
			}
		},
		ReconcileConcurrency: 1,
	}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := m.Close(); err != nil {
			t.Error(err)
		}
	})
	startSessionOwner(t, ctx, resources, m)
	if _, err := m.Wait(ctx, "invalid"); err == nil {
		t.Fatal("Session with an invalid workspace was marked ready")
	}
	owner, found, err := ReadOwnerStatus(ctx, resources, "invalid")
	if err != nil || !found || owner.Ready || owner.Error == "" {
		t.Fatalf("owner status = %+v, found %v, err %v", owner, found, err)
	}
	select {
	case <-failures:
	case <-ctx.Done():
		t.Fatal("Session recovery failure was not reported")
	}
}

func withTestSessionFacilities(t *testing.T, config Config) Config {
	t.Helper()
	if config.Environment == nil {
		config.Environment = func() Environment { return Environment{IncarnationUID: "test-incarnation"} }
	}
	var workers sync.WaitGroup
	host := supervision.NewHost(config.Context, config.State, lifecycle.Namespace, supervision.HarnessDomain, &workers, func(err error) {
		if err != nil {
			t.Error(err)
		}
	})
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	launch := func(ctx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		return host.Launch(ctx, owner, 0, supervisor, processConfig)
	}
	config.Scripts, err = scripts.New(scripts.Config{LaunchProcess: launch})
	if err != nil {
		t.Fatal(err)
	}
	bootstrapHost, err := bootstrap.NewHost(bootstrap.Config{}, bootstrap.Dependencies{
		Context: config.Context, State: config.State, Workers: &workers,
		Admission: func(ctx context.Context, _ lifecycle.EnvironmentDesired, work func(context.Context) error) error {
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
	config.Admission = testSessionAdmission(config.State)
	config.WaitBootstrap = bootstrapHost.Wait
	config.LaunchProcess = launch
	config.EnsureWorkspace = func(_ context.Context, cwd string) (string, error) {
		return workspace.Resolve(cwd, config.WorkspaceRoot)
	}
	config.ResolveModelConfig = func(context.Context, sessionpb.Harness, string) (*int, error) { return nil, nil }
	return config
}

func TestSessionManagerLoadsOneOwnerAndPublishesRecovery(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: root, Model: "test-model",
	}, "echo setup", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("one-session", record); err != nil {
		t.Fatal(err)
	}
	publishErrors := make(chan error, 16)
	var publicationMu sync.Mutex
	publications := map[string][]sessionpb.SetupState{}
	m, err := NewManager(withTestSessionFacilities(t, Config{
		Context: ctx, State: resources, Store: store, WorkspaceRoot: filepath.Dir(root),
		OnError: func(err error) {
			if err != nil {
				publishErrors <- err
			}
		},
		OnChange: func() {
			for _, id := range []string{"one-session", "fresh-session"} {
				input, found, err := ReadReconcileSnapshot(ctx, resources, id)
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
	inventoryChanged := m.InventoryChangesSince()
	startSessionOwner(t, ctx, resources, m)
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
			s, err := m.load(ctx, "one-session")
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
	select {
	case <-inventoryChanged:
	default:
		t.Fatal("loading a Session did not wake inventory observers")
	}
	publicationMu.Lock()
	retainedPublications := append([]sessionpb.SetupState(nil), publications["one-session"]...)
	publicationMu.Unlock()
	if len(retainedPublications) == 0 {
		t.Fatal("retained session was never published")
	}
	for _, setup := range retainedPublications {
		if setup != sessionpb.SetupState_SETUP_STATE_INTERRUPTED {
			t.Fatalf("retained setup was published before recovery: %v", retainedPublications)
		}
	}
	input, found, err := ReadReconcileSnapshot(ctx, resources, "one-session")
	if err != nil || !found || input.Setup != sessionpb.SetupState_SETUP_STATE_INTERRUPTED {
		t.Fatalf("recovered setup was not published: %+v, found=%t, err=%v", input, found, err)
	}
	entries, err := loaded[0].journal.Since(ctx, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].GetEvent().GetSetupInterrupted() == nil {
		t.Fatalf("durable setup recovery not recorded: %v", entries)
	}
	freshRecord, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: root, Model: "test-model",
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
			s, err := m.load(ctx, "fresh-session")
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
	input, found, err = ReadReconcileSnapshot(ctx, resources, "fresh-session")
	if err != nil || !found || input.Setup != sessionpb.SetupState_SETUP_STATE_RUNNING {
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

type failAcknowledgementModifyOnce struct {
	state.State
	failure error
	fail    bool
}

func (s *failAcknowledgementModifyOnce) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == retained.IngressAckType && s.fail {
		s.fail = false
		return s.failure
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func TestReopenRetryKeepsCOSIIngressAcknowledgementAfterJournalReopened(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root := t.TempDir()
	resources := &failAcknowledgementModifyOnce{State: state.WrapCore(namespaced.NewState(inmem.Build))}
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: root, Model: "test-model",
	}, "echo retained setup", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("reopen-session", record); err != nil {
		t.Fatal(err)
	}
	directory, err := store.Directory("reopen-session")
	if err != nil {
		t.Fatal(err)
	}
	sealedJournal, err := journal.Open(ctx, filepath.Join(directory, "journal.sqlite"), record.EventSourceId)
	if err != nil {
		t.Fatal(err)
	}
	sealCursor, err := sealedJournal.Seal(ctx, "previous-incarnation")
	if err != nil {
		t.Fatal(err)
	}
	if err := sealedJournal.Close(); err != nil {
		t.Fatal(err)
	}
	sealedAck := retained.Acknowledgement{Cursor: sealCursor, Sealed: true}
	if err := store.WriteAcknowledgement("reopen-session", sealedAck); err != nil {
		t.Fatal(err)
	}
	environment := Environment{IncarnationUID: "current-incarnation"}
	publishErr := errors.New("injected acknowledgement publication failure")
	resources.failure = publishErr
	resources.fail = true
	m, err := NewManager(withTestSessionFacilities(t, Config{
		Context: ctx, State: resources, Store: store, WorkspaceRoot: filepath.Dir(root), Environment: func() Environment { return environment },
		OnChange: func() {},
		OnError: func(err error) {
			if err != nil {
				t.Errorf("session manager: %v", err)
			}
		},
		ReconcileConcurrency: 1,
	}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := m.Close(); err != nil {
			t.Errorf("close session manager: %v", err)
		}
	})
	if _, err := m.load(ctx, "reopen-session"); !errors.Is(err, publishErr) {
		t.Fatalf("initial load error = %v, want %v after durable journal reopen", err, publishErr)
	}
	if m.Get("reopen-session") != nil {
		t.Fatal("failed reopen became visible in the Session registry")
	}
	if _, found, err := ReadReconcileSnapshot(ctx, resources, "reopen-session"); err != nil || found {
		t.Fatalf("failed reopen published a Session reconcile snapshot: found=%t, err=%v", found, err)
	}
	ack, err := store.ReadAcknowledgement("reopen-session")
	if err != nil || ack == nil || ack.Cursor != sealCursor || !ack.Sealed {
		t.Fatalf("acknowledgement after failed publication = %+v, err=%v; want retained sealed cursor", ack, err)
	}
	environment.Quiesced = true

	s, err := m.load(ctx, "reopen-session")
	if err != nil {
		t.Fatalf("retry load after durable journal reopen: %v", err)
	}
	ack, err = store.ReadAcknowledgement("reopen-session")
	if err != nil || ack == nil || ack.Cursor != sealCursor || ack.Sealed {
		t.Fatalf("reopened acknowledgement = %+v, err=%v; want cursor preserved and unsealed", ack, err)
	}
	entries, err := s.journal.Since(ctx, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	reopened, interrupted := 0, 0
	for _, entry := range entries {
		if entry.GetEvent().GetJournalReopened() != nil {
			reopened++
		}
		if entry.GetEvent().GetSetupInterrupted() != nil {
			interrupted++
		}
	}
	input, found, err := ReadReconcileSnapshot(ctx, resources, "reopen-session")
	if err != nil || !found || input.Setup != sessionpb.SetupState_SETUP_STATE_INTERRUPTED {
		t.Fatalf("retained setup state = %+v, found=%t, err=%v", input, found, err)
	}
	if reopened != 1 || interrupted != 1 || s.journal.SealState() != nil {
		t.Fatalf("reopen evidence: events=%v, seal=%+v", entries, s.journal.SealState())
	}
	sealCursor, err = s.journal.Seal(ctx, "previous-successor")
	if err != nil {
		t.Fatalf("seal reopened Session: %v", err)
	}
	if err := store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: sealCursor, Sealed: true}); err != nil {
		t.Fatalf("persist sealed acknowledgement: %v", err)
	}
	environment = Environment{IncarnationUID: "next-incarnation"}
	inventoryChanged := m.InventoryChangesSince()
	if err := m.Reopen(ctx, s); err != nil {
		t.Fatalf("reopen sealed Session: %v", err)
	}
	select {
	case <-inventoryChanged:
	default:
		t.Fatal("reopening a sealed Session did not wake inventory observers")
	}
	ack, err = store.ReadAcknowledgement(s.ID())
	if err != nil || ack == nil || ack.Cursor != sealCursor || ack.Sealed {
		t.Fatalf("reopened durable acknowledgement = %+v, err=%v; want exact cursor unsealed", ack, err)
	}
	badAck := retained.Acknowledgement{Cursor: s.journal.LastCursor() + 1, Sealed: true}
	if err := store.WriteAcknowledgement("reopen-session", badAck); err != nil {
		t.Fatal(err)
	}
	if err := m.Reopen(ctx, s); err == nil {
		t.Fatal("reopen accepted sealed acknowledgement beyond the durable journal cursor")
	}
	ack, err = store.ReadAcknowledgement("reopen-session")
	if err != nil || ack == nil || *ack != badAck {
		t.Fatalf("mismatched acknowledgement = %+v, err=%v; want preserved mismatch", ack, err)
	}
}

func TestCodexExitBeforeProcessInstall(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := retained.NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CODEX}}}, Cwd: root, Model: "test-model",
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
	j, err := journal.Open(ctx, filepath.Join(directory, "journal.sqlite"), record.EventSourceId)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = j.Close() })
	s := newSession(sessionDependencies{
		environmentState: func() Environment { return Environment{IncarnationUID: "test-incarnation"} },
		ctx:              ctx, state: resources, store: store,
		changed: func() {}, fail: func(err error) { t.Error(err) }, publish: func(*Session, bool) {},
	}, "codex-session", record, j)
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

func testSessionAdmission(resources state.State) lifecycle.EnvironmentAdmissionGuard {
	return func(ctx context.Context, expected lifecycle.EnvironmentDesired, work func(context.Context) error) error {
		current, found, err := lifecycle.ReadEnvironmentDesired(ctx, resources)
		if err != nil {
			return err
		}
		if !found || current != expected || current.Mode != lifecycle.Running {
			return nil
		}
		return work(ctx)
	}
}
