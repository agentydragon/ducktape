package sandboxinventory

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"

	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	deliveryDatabase *postgres.Database
	deliveryAPI      *kubeapiserver.Server
)

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
		deliveryDatabase = database
		return kubeapiserver.Run(m, func(server *kubeapiserver.Server) error { deliveryAPI = server; return nil })
	}))
}

type observedCatalog struct {
	*KubernetesSandboxes
	scans atomic.Int32
}

func (c *observedCatalog) ListSandboxes(ctx context.Context) ([]dispatcher.SandboxSnapshot, error) {
	c.scans.Add(1)
	return c.KubernetesSandboxes.ListSandboxes(ctx)
}

type deliveryRuntime struct {
	harnesshostpb.UnimplementedHarnessHostServer
	opened   chan string
	commands chan string
}

func (r *deliveryRuntime) CreateSession(_ context.Context, request *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	r.opened <- request.SessionId
	return &harnesshostpb.CreateSessionResponse{}, nil
}

func (r *deliveryRuntime) SubmitInput(_ context.Context, request *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	r.commands <- request.CommandId
	return &harnesshostpb.SubmitInputResponse{}, nil
}

type noWake struct{}

func (noWake) RequestWake(context.Context, *lifecycle.Target, lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	return nil, errors.New("test Sandbox should not need wake")
}

type deliveryFixture struct {
	ctx        context.Context
	queueStore *store.Store
	dsn        string
	objects    client.Client
	sandbox    *sandboxapi.Sandbox
	identity   *identitypb.Identity
	runtime    *deliveryRuntime
	sender     *dispatcher.HarnessHostClient
	catalog    *observedCatalog
}

func newDeliveryFixture(t *testing.T) *deliveryFixture {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(cancel)
	scheme := runtime.NewScheme()
	for _, register := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme} {
		if err := register(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.New(deliveryAPI.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	namespace := "test-delivery-" + uuid.NewString()
	if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatal(err)
	}
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "test-execution"}, Spec: policyapi.IdentitySpec{Description: "test execution identity"}}
	if err := objects.Create(ctx, identity); err != nil {
		t.Fatal(err)
	}
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: "test-delivery", Namespace: namespace}, Spec: sandboxapi.SandboxSpec{Template: "test-template", Running: true, Identity: &sandboxapi.IdentityRef{Name: identity.Name}}}
	if err := objects.Create(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	sandbox.Status.Conditions = []metav1.Condition{{Type: sandboxapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "TestReady", LastTransitionTime: metav1.Now()}}
	if err := objects.Status().Update(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	inventory, err := cache.New(deliveryAPI.Config, cache.Options{Scheme: scheme, DefaultNamespaces: map[string]cache.Config{namespace: {}}})
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := NewKubernetesSandboxes(inventory, objects, namespace)
	if err != nil {
		t.Fatal(err)
	}
	cacheCtx, stopCache := context.WithCancel(ctx)
	cacheDone := make(chan error, 1)
	go func() { cacheDone <- inventory.Start(cacheCtx) }()
	t.Cleanup(func() {
		stopCache()
		if err := <-cacheDone; err != nil {
			t.Error(err)
		}
	})
	if !catalog.WaitSynced(ctx) {
		t.Fatal("test inventory did not sync")
	}
	ca := transporttesting.NewCA(t)
	certificate := ca.Issue(t, peertls.SessionDispatcherURI)
	serverTLS, err := certificate.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(serverTLS)))
	receiver := &deliveryRuntime{opened: make(chan string, 256), commands: make(chan string, 256)}
	harnesshostpb.RegisterHarnessHostServer(server, receiver)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	senderCertificate := ca.Issue(t, peertls.SessionsURI)
	dsn := deliveryDatabase.Clone(t)
	return &deliveryFixture{
		ctx: ctx, queueStore: store.NewStore(postgres.Open(t, dsn), 256), dsn: dsn, objects: objects, sandbox: sandbox,
		identity: &identitypb.Identity{Name: identity.Name, Uid: string(identity.UID)}, runtime: receiver,
		catalog: &observedCatalog{KubernetesSandboxes: catalog}, sender: &dispatcher.HarnessHostClient{Namespace: namespace, Timeout: 5 * time.Second, TargetTemplate: listener.Addr().String(), GatewayTLS: &peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.SessionDispatcherURI, ClientURI: peertls.SessionsURI, CertFile: senderCertificate.CertFile, KeyFile: senderCertificate.KeyFile}},
	}
}

func (f *deliveryFixture) listen(t *testing.T, name string) *wakeups.Hub {
	t.Helper()
	target := postgres.URLWithApplicationName(t, f.dsn, name)
	hub, err := wakeups.Listen(f.ctx, target.String(), store.NotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	return hub
}

func (f *deliveryFixture) start(t *testing.T, hub *wakeups.Hub) {
	t.Helper()
	ctx, cancel := context.WithCancel(f.ctx)
	worker := &dispatcher.DeliveryWorker{Store: f.queueStore, Catalog: f.catalog, Wake: noWake{}, Delivery: f.sender, Hub: hub, Ready: make(chan struct{})}
	done := make(chan struct{})
	var runErr error
	go func() { runErr = worker.Run(ctx); close(done) }()
	t.Cleanup(func() {
		cancel()
		<-done
		if !errors.Is(runErr, context.Canceled) {
			t.Errorf("worker stopped: %v", runErr)
		}
	})
	select {
	case <-worker.Ready:
	case <-done:
		t.Fatalf("worker stopped before Ready: %v", runErr)
	case <-f.ctx.Done():
		t.Fatal(f.ctx.Err())
	}
}

func (f *deliveryFixture) create(t *testing.T) string {
	t.Helper()
	id := uuid.NewString()
	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}
	_, err := f.queueStore.CreateSessionWithPlacementForCaller(f.ctx, id, f.sandbox.Name, string(f.sandbox.UID), spec, nil, nil, store.SessionExecution{Identity: f.identity}, store.ServiceCaller{SPIFFEID: peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func (f *deliveryFixture) command(t *testing.T, session string) string {
	t.Helper()
	id := uuid.NewString()
	_, err := f.queueStore.EnqueueForCaller(f.ctx, session, f.sandbox.Name, string(f.sandbox.UID), &commandpb.Command{CommandId: id, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "test input"}}}, nil, store.ServiceCaller{SPIFFEID: peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func (f *deliveryFixture) await(t *testing.T, channel <-chan string, expected string) {
	t.Helper()
	select {
	case id := <-channel:
		if id != expected {
			t.Fatalf("delivery = %q, want %q", id, expected)
		}
	case <-f.ctx.Done():
		t.Fatal(f.ctx.Err())
	}
}

func (f *deliveryFixture) until(t *testing.T, condition func(context.Context) (bool, error)) {
	t.Helper()
	if err := wait.PollUntilContextCancel(f.ctx, 10*time.Millisecond, true, condition); err != nil {
		t.Fatal(err)
	}
}

func (f *deliveryFixture) accepted(t *testing.T, id string) {
	t.Helper()
	f.until(t, func(ctx context.Context) (bool, error) {
		s, err := f.queueStore.GetSession(ctx, id)
		return err == nil && s.State == store.SessionAcceptedState, err
	})
}

func TestStartupRecoversMissedNotifyAndKeyedBurst(t *testing.T) {
	f := newDeliveryFixture(t)
	// No LISTEN connection exists when this commit's notification is sent.
	session := f.create(t)
	f.start(t, f.listen(t, "test-startup-listener"))
	f.await(t, f.runtime.opened, session)
	f.accepted(t, session)
	scans := f.catalog.scans.Load()
	// Keep the consumer busy while many distinct Session keys arrive; no bounded
	// notification channel may coalesce these into one lost routing key.
	var sessions []string
	for range 40 {
		sessions = append(sessions, f.create(t))
	}
	seen := map[string]bool{}
	for range sessions {
		select {
		case id := <-f.runtime.opened:
			seen[id] = true
		case <-f.ctx.Done():
			t.Fatal(f.ctx.Err())
		}
	}
	for _, id := range sessions {
		if !seen[id] {
			t.Fatalf("lost Session key %s", id)
		}
	}
	if got := f.catalog.scans.Load(); got != scans {
		t.Fatalf("ordinary NOTIFY caused inventory scans: %d -> %d", scans, got)
	}
}

func TestReconnectRecoversNotifyCommittedWhileListenerDisconnected(t *testing.T) {
	f := newDeliveryFixture(t)
	role := "test_listener_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := f.queueStore.DB.ExecContext(f.ctx, `CREATE ROLE `+role+` LOGIN PASSWORD 'test-listener-password'`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := f.queueStore.DB.Exec(`DROP ROLE ` + role); err != nil {
			t.Error(err)
		}
	})
	target := postgres.URLWithApplicationName(t, f.dsn, "test-gap-listener")
	target.User = url.UserPassword(role, "test-listener-password")
	gap, err := wakeups.Listen(f.ctx, target.String(), store.NotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = gap.Close() })
	f.start(t, gap)
	generation := gap.Generation()
	if _, err := f.queueStore.DB.ExecContext(f.ctx, `ALTER ROLE `+role+` NOLOGIN; SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='test-gap-listener'`); err != nil {
		t.Fatal(err)
	}
	f.until(t, func(context.Context) (bool, error) { return !gap.Connected() && gap.Generation() != generation, nil })
	// The role cannot reconnect, so this commit's NOTIFY has no listener.
	session := f.create(t)
	select {
	case id := <-f.runtime.opened:
		t.Fatalf("disconnected listener delivered %s", id)
	default:
	}
	if _, err := f.queueStore.DB.ExecContext(f.ctx, `ALTER ROLE `+role+` LOGIN`); err != nil {
		t.Fatal(err)
	}
	f.await(t, f.runtime.opened, session)
	f.accepted(t, session)
	if gap.Generation() == generation {
		t.Fatal("listener generation did not change")
	}
}

func TestClaimErrorsRetryWithoutRescan(t *testing.T) {
	for _, sessionClaim := range []bool{true, false} {
		t.Run(fmt.Sprint(sessionClaim), func(t *testing.T) {
			f := newDeliveryFixture(t)
			f.start(t, f.listen(t, "test-claim-error-listener"))
			var session string
			if !sessionClaim {
				session = f.create(t)
				f.await(t, f.runtime.opened, session)
				f.accepted(t, session)
			}
			table, state := "dispatch", store.DeliveringState
			if sessionClaim {
				table, state = "session", store.SessionInProgressState
			}
			// A sequence increment survives the aborted transaction. The first claim
			// raises a real serialization error; a later claim can commit without any
			// intervening notification from the failed write.
			if _, err := f.queueStore.DB.ExecContext(f.ctx, `CREATE SEQUENCE test_claims;
   CREATE FUNCTION test_fail_claim() RETURNS trigger LANGUAGE plpgsql AS $$
   BEGIN IF nextval('test_claims')=1 THEN RAISE EXCEPTION 'test transient claim failure' USING ERRCODE='40001'; END IF; RETURN NEW; END $$;
   CREATE TRIGGER test_fail_claim BEFORE UPDATE ON `+table+` FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state AND NEW.state='`+state+`') EXECUTE FUNCTION test_fail_claim()`); err != nil {
				t.Fatal(err)
			}
			scans := f.catalog.scans.Load()
			if sessionClaim {
				session = f.create(t)
				f.await(t, f.runtime.opened, session)
				f.accepted(t, session)
			} else {
				command := f.command(t, session)
				f.await(t, f.runtime.commands, command)
			}
			var attempts int
			if err := f.queueStore.DB.QueryRowContext(f.ctx, "SELECT last_value FROM test_claims").Scan(&attempts); err != nil || attempts != 2 {
				t.Fatalf("claim attempts: %d %v", attempts, err)
			}
			if got := f.catalog.scans.Load(); got != scans {
				t.Fatalf("claim retry depended on a full scan: %d -> %d", scans, got)
			}
		})
	}
}

func TestSandboxDeletionCleansQueuedWorkAndFencesReplacement(t *testing.T) {
	f := newDeliveryFixture(t)
	f.sandbox.Status.Conditions[0].Status = metav1.ConditionFalse
	if err := f.objects.Status().Update(f.ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	f.until(t, func(ctx context.Context) (bool, error) {
		view, found, err := f.catalog.GetSandbox(ctx, f.sandbox.Name)
		return found && view.Blocker != "", err
	})
	f.start(t, f.listen(t, "test-deletion-listener"))
	session := f.create(t)
	command := f.command(t, session)
	f.until(t, func(ctx context.Context) (bool, error) {
		s, err := f.queueStore.GetSession(ctx, session)
		return err == nil && s.Reason != "", err
	})
	scans := f.catalog.scans.Load()
	if err := f.objects.Delete(f.ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	replacement := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: f.sandbox.Name, Namespace: f.sandbox.Namespace}, Spec: f.sandbox.Spec}
	if err := f.objects.Create(f.ctx, replacement); err != nil {
		t.Fatal(err)
	}
	f.until(t, func(ctx context.Context) (bool, error) {
		_, err := f.queueStore.GetDispatch(ctx, command)
		if errors.Is(err, store.ErrNotFound) {
			return true, nil
		}
		return false, err
	})
	if _, err := f.queueStore.EnqueueForCaller(f.ctx, session, f.sandbox.Name, string(f.sandbox.UID), &commandpb.Command{CommandId: uuid.NewString(), Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "test stale input"}}}, nil, store.ServiceCaller{SPIFFEID: peertls.AppURI}); !errors.Is(err, store.ErrSandboxDeleted) {
		t.Fatalf("deleted UID admitted work: %v", err)
	}
	if got := f.catalog.scans.Load(); got != scans {
		t.Fatalf("Sandbox deletion caused a full scan: %d -> %d", scans, got)
	}
	select {
	case id := <-f.runtime.opened:
		t.Fatalf("deleted queued Session delivered: %s", id)
	default:
	}
}

func TestLeaseExpiryAndTwoReplicasPreserveHeadOrder(t *testing.T) {
	f := newDeliveryFixture(t)
	session := f.create(t)
	// Model a crashed holder with in-flight work. Both replicas' startup scans
	// see it and schedule this exact Session at the held lease's expiry.
	owner, err := f.queueStore.AcquireSessionLease(f.ctx, session)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.queueStore.SettleSession(f.ctx, session, *owner, []string{store.SessionWaitingState}, store.SessionInProgressState, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.queueStore.DB.ExecContext(f.ctx, `UPDATE session_delivery_lease SET expires_at=clock_timestamp()+interval '2 seconds' WHERE session_id=$1`, session); err != nil {
		t.Fatal(err)
	}
	first, second := f.command(t, session), f.command(t, session)
	f.start(t, f.listen(t, "test-replica-one"))
	f.start(t, f.listen(t, "test-replica-two"))
	f.await(t, f.runtime.opened, session)
	f.await(t, f.runtime.commands, first)
	f.await(t, f.runtime.commands, second)
	f.until(t, func(ctx context.Context) (bool, error) {
		d, err := f.queueStore.GetDispatch(ctx, second)
		return err == nil && d.State == store.AdmittedState, err
	})
	if got := f.catalog.scans.Load(); got != 2 {
		t.Fatalf("lease recovery rescanned inventory: %d", got)
	}
	select {
	case id := <-f.runtime.opened:
		t.Fatalf("Session materialized twice: %s", id)
	default:
	}
	select {
	case id := <-f.runtime.commands:
		t.Fatalf("command delivered twice: %s", id)
	default:
	}
}
