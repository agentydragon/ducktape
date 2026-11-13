package environments

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

var (
	watchAPI        *kubeapiserver.Server
	watchNamespaces atomic.Int32
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		watchAPI = server
		return nil
	}))
}

type watchFixture struct {
	objects      client.WithWatch
	rpc          environmentpb.EnvironmentServiceClient
	target       *identitypb.EnvironmentTarget
	ctx          context.Context
	metadataRead <-chan struct{}
	readerCtx    context.Context
	reviews      *switchableReviews
}

type switchableReviews struct {
	inner   auth.TokenReviews
	revoked atomic.Bool
	calls   atomic.Int32
}

type observedPodWatch struct {
	watch.Interface
	selected chan struct{}
}

func (w *observedPodWatch) ResultChan() <-chan watch.Event {
	select {
	case w.selected <- struct{}{}:
	default:
	}
	return w.Interface.ResultChan()
}

func TestWatchEnvironmentDoesNotReadForUnrelatedPods(t *testing.T) {
	var reads atomic.Int32
	pods := watch.NewRaceFreeFake()
	selected := make(chan struct{}, 2)
	objects := interceptor.NewClient(baseReader(t), interceptor.Funcs{
		Get: func(ctx context.Context, source client.WithWatch, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
			reads.Add(1)
			return source.Get(ctx, key, object, options...)
		},
		Watch: func(_ context.Context, _ client.WithWatch, object client.ObjectList, _ ...client.ListOption) (watch.Interface, error) {
			if _, ok := object.(*corev1.PodList); ok {
				return &observedPodWatch{Interface: pods, selected: selected}, nil
			}
			return watch.NewRaceFreeFake(), nil
		},
	})
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	ctx, cancel := context.WithCancel(bearing(t, matchingToken))
	defer cancel()
	stream, err := rpc.WatchEnvironment(ctx, &environmentpb.WatchEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-selected:
	case <-time.After(time.Second):
		t.Fatal("initial Pod watch select was not reached")
	}
	before := reads.Load()
	pods.Add(&corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "unrelated", Namespace: discoveryNamespace}})
	select {
	case <-selected:
	case <-time.After(time.Second):
		t.Fatal("unrelated Pod event was not consumed")
	}
	if got := reads.Load(); got != before {
		t.Fatalf("unrelated Pod event caused %d additional object reads", got-before)
	}
}

func (r *switchableReviews) Create(ctx context.Context, review *authenticationv1.TokenReview, opts metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.calls.Add(1)
	if r.revoked.Load() {
		return &authenticationv1.TokenReview{}, nil
	}
	return r.inner.Create(ctx, review, opts)
}

func newWatchFixture(t *testing.T) watchFixture {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	t.Cleanup(cancel)
	namespace := fmt.Sprintf("test-environment-watch-%d", watchNamespaces.Add(1))
	account, err := watchAPI.ServiceAccount(ctx, auth.Account{Namespace: namespace, Name: sandboxName})
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	objects, err := client.NewWithWatch(watchAPI.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	sandbox := sandboxObject("")
	sandbox.Namespace = namespace
	if err := objects.Create(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	account.OwnerReferences = executionAccountObject(sandbox.UID).OwnerReferences
	if err := objects.Update(ctx, account); err != nil {
		t.Fatal(err)
	}
	metadataRead := make(chan struct{})
	var metadataOnce sync.Once
	observedObjects := interceptor.NewClient(objects, interceptor.Funcs{
		Get: func(ctx context.Context, source client.WithWatch, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
			err := source.Get(ctx, key, object, options...)
			if err == nil && object.GetAnnotations()["test.example/ignored"] == "changed" {
				metadataOnce.Do(func() { close(metadataRead) })
			}
			return err
		},
	})
	readerAccount := auth.Account{Namespace: namespace, Name: "inventory-reader"}
	if _, err := watchAPI.ServiceAccount(ctx, readerAccount); err != nil {
		t.Fatal(err)
	}
	manager, err := lifecycle.NewManager(lifecycle.Options{Namespace: namespace, Objects: observedObjects, WakeBudget: unusedWakeBudget{}})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(namespace, observedObjects, runtimeconfig.Catalog{}, manager, sets.New(readerAccount), ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	service.collectionAuthInterval = 50 * time.Millisecond
	reviews := &switchableReviews{inner: watchAPI.TokenReviews()}
	resolver, err := auth.NewResolver(reviews, dispatcherAudience, []string{namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, Methods(resolver), func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	})
	token, err := watchAPI.UnboundToken(ctx, auth.Account{Namespace: namespace, Name: sandboxName}, dispatcherAudience)
	if err != nil {
		t.Fatal(err)
	}
	readerToken, err := watchAPI.UnboundToken(ctx, readerAccount, dispatcherAudience)
	if err != nil {
		t.Fatal(err)
	}
	return watchFixture{
		reviews:   reviews,
		readerCtx: metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken),
		objects:   objects, rpc: environmentpb.NewEnvironmentServiceClient(connection),
		target:       &identitypb.EnvironmentTarget{Namespace: namespace, Name: sandboxName, Uid: string(sandbox.UID)},
		ctx:          metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token),
		metadataRead: metadataRead,
	}
}

func (f watchFixture) sandbox(t *testing.T) *sandboxapi.Sandbox {
	t.Helper()
	var sandbox sandboxapi.Sandbox
	if err := f.objects.Get(f.ctx, types.NamespacedName{Namespace: f.target.Namespace, Name: f.target.Name}, &sandbox); err != nil {
		t.Fatal(err)
	}
	return &sandbox
}

func TestWatchEnvironmentSendsCurrentStateUpdatesAndFreshReconnectSnapshot(t *testing.T) {
	f := newWatchFixture(t)
	sandbox := f.sandbox(t)
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	if err := f.objects.Status().Update(f.ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	got, err := f.rpc.GetEnvironment(f.ctx, &environmentpb.GetEnvironmentRequest{Target: f.target})
	if err != nil || got.GetObservedState().GetOperatingMode() != environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED {
		t.Fatalf("GetEnvironment on a suspended target = %v, %v", got, err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	stream, err := f.rpc.WatchEnvironment(ctx, &environmentpb.WatchEnvironmentRequest{Target: f.target})
	if err != nil {
		t.Fatal(err)
	}
	initial, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	got.ObservedState.ObservedAt, initial.Environment.ObservedState.ObservedAt = nil, nil
	if !proto.Equal(got, initial.Environment) {
		t.Fatalf("initial watch snapshot = %v, want current Get snapshot %v", initial, got)
	}

	// A metadata-only change must not produce a duplicate snapshot just because observed_at changed.
	sandbox = f.sandbox(t)
	sandbox.Annotations = map[string]string{"test.example/ignored": "changed"}
	if err := f.objects.Update(f.ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	select {
	case <-f.metadataRead:
	case <-f.ctx.Done():
		t.Fatal("watch did not reread the metadata-only update")
	}
	sandbox = f.sandbox(t)
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeRunning
	sandbox.Status.Conditions = []metav1.Condition{{
		Type: sandboxapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "RuntimeReady", LastTransitionTime: metav1.Now(),
	}}
	if err := f.objects.Status().Update(f.ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	ready, err := stream.Recv()
	if err != nil || ready.GetEnvironment().GetObservedState().GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_READY {
		t.Fatalf("next snapshot = %v, %v, want Ready without an unchanged intermediate snapshot", ready, err)
	}
	cancel()
	if _, err := stream.Recv(); status.Code(err) != codes.Canceled {
		t.Fatalf("canceled watch = %v, want Canceled", err)
	}

	reconnected, err := f.rpc.WatchEnvironment(f.ctx, &environmentpb.WatchEnvironmentRequest{Target: f.target})
	if err != nil {
		t.Fatal(err)
	}
	fresh, err := reconnected.Recv()
	if err != nil || fresh.GetEnvironment().GetObservedState().GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_READY {
		t.Fatalf("reconnected initial snapshot = %v, %v, want current Ready state", fresh, err)
	}
}

func TestWatchEnvironmentCoalescesCurrentPodStatusAndFencesReplacement(t *testing.T) {
	f := newWatchFixture(t)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "vm-launcher", Namespace: f.target.Namespace},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "runner", Image: "test"}}},
	}
	if err := f.objects.Create(f.ctx, pod); err != nil {
		t.Fatal(err)
	}
	sandbox := f.sandbox(t)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: pod.Name, UID: string(pod.UID)}
	if err := f.objects.Status().Update(f.ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	stream, err := f.rpc.WatchEnvironment(f.ctx, &environmentpb.WatchEnvironmentRequest{Target: f.target})
	if err != nil {
		t.Fatal(err)
	}
	initial, err := stream.Recv()
	if err != nil || initial.GetEnvironment().GetKubernetesDiagnostics().GetPod().GetUid() != string(pod.UID) {
		t.Fatalf("initial Pod snapshot = %v, %v", initial, err)
	}
	pod.Status.Phase = corev1.PodRunning
	if err := f.objects.Status().Update(f.ctx, pod); err != nil {
		t.Fatal(err)
	}
	updated, err := stream.Recv()
	if err != nil || updated.GetEnvironment().GetKubernetesDiagnostics().GetPod().GetStatus().AsMap()["phase"] != "Running" {
		t.Fatalf("Pod status snapshot = %v, %v", updated, err)
	}
	if err := f.objects.Delete(f.ctx, pod); err != nil {
		t.Fatal(err)
	}
	removedPod, err := stream.Recv()
	if err != nil || removedPod.GetEnvironment().GetKubernetesDiagnostics().GetPod() != nil || removedPod.GetEnvironment().GetObservedState().GetRuntimeIncarnationUid() != string(pod.UID) {
		t.Fatalf("deleted current Pod snapshot = %v, %v", removedPod, err)
	}
	replacement := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: pod.Name, Namespace: pod.Namespace},
		Spec:       pod.Spec,
	}
	if err := f.objects.Create(f.ctx, replacement); err != nil {
		t.Fatal(err)
	}
	if replacement.UID == pod.UID {
		t.Fatal("replacement Pod reused the previous UID")
	}
	got, err := f.rpc.GetEnvironment(f.ctx, &environmentpb.GetEnvironmentRequest{Target: f.target})
	if err != nil || got.GetKubernetesDiagnostics().GetPod() != nil {
		t.Fatalf("same-name replacement GetEnvironment = %v, %v, want no current Pod", got, err)
	}
}

func TestWatchEnvironmentEndsWhenTargetOrExecutionAccountIsNoLongerAccessible(t *testing.T) {
	for _, change := range []string{"replace target", "delete target", "replace execution account", "remove account owner"} {
		t.Run(change, func(t *testing.T) {
			f := newWatchFixture(t)
			stream, err := f.rpc.WatchEnvironment(f.ctx, &environmentpb.WatchEnvironmentRequest{Target: f.target})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := stream.Recv(); err != nil {
				t.Fatal(err)
			}
			switch change {
			case "replace target", "delete target":
				if err := f.objects.Delete(f.ctx, f.sandbox(t)); err != nil {
					t.Fatal(err)
				}
				if change == "replace target" {
					replacement := sandboxObject("")
					replacement.Namespace = f.target.Namespace
					if err := f.objects.Create(f.ctx, replacement); err != nil {
						t.Fatal(err)
					}
				}
			case "replace execution account", "remove account owner":
				var account corev1.ServiceAccount
				if err := f.objects.Get(f.ctx, types.NamespacedName{Namespace: f.target.Namespace, Name: sandboxName}, &account); err != nil {
					t.Fatal(err)
				}
				if change == "replace execution account" {
					if err := f.objects.Delete(f.ctx, &account); err != nil {
						t.Fatal(err)
					}
					replacement := executionAccountObject(types.UID(f.target.Uid))
					replacement.Namespace, replacement.UID = f.target.Namespace, ""
					if err := f.objects.Create(f.ctx, replacement); err != nil {
						t.Fatal(err)
					}
				} else {
					account.OwnerReferences = nil
					if err := f.objects.Update(f.ctx, &account); err != nil {
						t.Fatal(err)
					}
				}
			}
			if _, err := stream.Recv(); status.Code(err) != codes.NotFound {
				t.Fatalf("watch after %s = %v, want NotFound", change, err)
			}
		})
	}
}

func TestWatchEnvironmentDelegationUsesTheEffectiveCaller(t *testing.T) {
	rpc := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_WatchEnvironment_FullMethodName,
	})
	effective := auth.Account{Namespace: discoveryNamespace, Name: sandboxName}
	ctx, cancel := context.WithTimeout(delegatedBearing(t, actionsToken, effective, executionUID), 5*time.Second)
	defer cancel()
	stream, err := rpc.WatchEnvironment(ctx, &environmentpb.WatchEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	got, err := stream.Recv()
	if err != nil || got.GetEnvironment().GetExecutionAccount().GetUid() != executionUID {
		t.Fatalf("delegated watch = %v, %v, want effective caller's environment", got, err)
	}
	for _, denied := range []struct {
		name, token, uid string
		want             codes.Code
	}{
		{name: "untrusted actor", token: untrustedToken, uid: executionUID, want: codes.PermissionDenied},
		{name: "recreated caller", token: actionsToken, uid: "replaced-uid", want: codes.PermissionDenied},
	} {
		t.Run(denied.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(delegatedBearing(t, denied.token, effective, denied.uid), 5*time.Second)
			defer cancel()
			refused, err := rpc.WatchEnvironment(ctx, &environmentpb.WatchEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
			})
			if err == nil {
				_, err = refused.Recv()
			}
			if status.Code(err) != denied.want {
				t.Fatalf("watch error = %v, want %v", err, denied.want)
			}
		})
	}
}

func TestWatchEnvironmentClosesAllSourcesOnWatchLoss(t *testing.T) {
	for _, failure := range []string{"closed", "expired", "opening second watch"} {
		t.Run(failure, func(t *testing.T) {
			sources := make(chan *watch.RaceFreeFakeWatcher, 3)
			objects := interceptor.NewClient(baseReader(t), interceptor.Funcs{
				Watch: func(_ context.Context, _ client.WithWatch, object client.ObjectList, _ ...client.ListOption) (watch.Interface, error) {
					if _, second := object.(*corev1.ServiceAccountList); second && failure == "opening second watch" {
						return nil, errors.New("injected source failure")
					}
					source := watch.NewRaceFreeFake()
					sources <- source
					return source, nil
				},
			})
			rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
			ctx, cancel := context.WithTimeout(bearing(t, matchingToken), 5*time.Second)
			defer cancel()
			stream, err := rpc.WatchEnvironment(ctx, &environmentpb.WatchEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
			})
			if err != nil {
				t.Fatal(err)
			}
			if failure == "opening second watch" {
				if _, err := stream.Recv(); status.Code(err) != codes.Unavailable {
					t.Fatalf("failed watch setup = %v, want Unavailable", err)
				}
			} else {
				if _, err := stream.Recv(); err != nil {
					t.Fatal(err)
				}
			}
			wantSources := 3
			if failure == "opening second watch" {
				wantSources = 1
			}
			opened := make([]*watch.RaceFreeFakeWatcher, 0, wantSources)
			for i := 0; i < wantSources; i++ {
				select {
				case source := <-sources:
					opened = append(opened, source)
				case <-ctx.Done():
					t.Fatalf("only %d Kubernetes watches were opened", len(opened))
				}
			}
			first := opened[0]
			switch failure {
			case "closed":
				first.Stop()
			case "expired":
				first.Error(&metav1.Status{Status: metav1.StatusFailure, Reason: metav1.StatusReasonExpired, Code: 410})
			}
			if failure != "opening second watch" {
				if _, err := stream.Recv(); status.Code(err) != codes.Unavailable {
					t.Fatalf("lost watch = %v, want Unavailable", err)
				}
			}
			for index, source := range opened {
				if !source.IsStopped() {
					t.Fatalf("source %d was not stopped when watch ended", index)
				}
			}
		})
	}
}

func receiveCollection(t *testing.T, stream grpc.ServerStreamingClient[environmentpb.WatchEnvironmentsResponse]) []*environmentpb.EnvironmentDescriptor {
	t.Helper()
	var found []*environmentpb.EnvironmentDescriptor
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		switch value := frame.Frame.(type) {
		case *environmentpb.WatchEnvironmentsResponse_Snapshot:
			found = append(found, value.Snapshot.Environments...)
		case *environmentpb.WatchEnvironmentsResponse_SnapshotComplete:
			return found
		default:
			t.Fatalf("unexpected frame %v", frame)
		}
	}
}

func TestWatchEnvironmentsScopesAndIncludesSandboxesWithoutAccounts(t *testing.T) {
	f := newWatchFixture(t)
	other := sandboxObject("")
	other.Namespace = f.target.Namespace
	other.Name = "other"
	if err := f.objects.Create(f.ctx, other); err != nil {
		t.Fatal(err)
	}
	own, err := f.rpc.WatchEnvironments(f.ctx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	ownRows := receiveCollection(t, own)
	if len(ownRows) != 1 || ownRows[0].GetTarget().GetName() != sandboxName {
		t.Fatalf("own snapshot = %v", ownRows)
	}
	all, err := f.rpc.WatchEnvironments(f.readerCtx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	allRows := receiveCollection(t, all)
	if len(allRows) != 2 || allRows[0].GetTarget().GetName() != "other" || allRows[1].GetTarget().GetName() != sandboxName {
		t.Fatalf("reader snapshot = %v", allRows)
	}
	if err := f.objects.Delete(f.ctx, other); err != nil {
		t.Fatal(err)
	}
	after, err := all.Recv()
	if err != nil || after.GetRemoved().GetName() != other.Name || after.GetRemoved().GetUid() != string(other.UID) {
		t.Fatalf("after deletion = %v, %v; want UID-fenced removal", after, err)
	}
}

func TestCollectionWatchUpsertsAndRemovesOnAccountLoss(t *testing.T) {
	f := newWatchFixture(t)
	stream, err := f.rpc.WatchEnvironments(f.ctx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	initial := receiveCollection(t, stream)
	if len(initial) != 1 {
		t.Fatalf("initial = %v", initial)
	}
	var metadataSandbox sandboxapi.Sandbox
	if err := f.objects.Get(f.ctx, types.NamespacedName{Namespace: f.target.Namespace, Name: f.target.Name}, &metadataSandbox); err != nil {
		t.Fatal(err)
	}
	metadataSandbox.Annotations = map[string]string{"test.example/ignored": "changed"}
	if err := f.objects.Update(f.ctx, &metadataSandbox); err != nil {
		t.Fatal(err)
	}
	select {
	case <-f.metadataRead:
	case <-f.ctx.Done():
		t.Fatal("collection watch did not reread the metadata-only update")
	}
	sandbox := f.sandbox(t)
	sandbox.Spec.WakeOnSend = true
	if err := f.objects.Update(f.ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	changed, err := stream.Recv()
	if err != nil || !changed.GetUpsert().GetConfiguration().GetWakeOnSend() {
		t.Fatalf("upsert = %v, %v", changed, err)
	}
	var account corev1.ServiceAccount
	key := types.NamespacedName{Namespace: f.target.Namespace, Name: sandboxName}
	if err := f.objects.Get(f.ctx, key, &account); err != nil {
		t.Fatal(err)
	}
	account.OwnerReferences = nil
	if err := f.objects.Update(f.ctx, &account); err != nil {
		t.Fatal(err)
	}
	removed, err := stream.Recv()
	if err != nil || removed.GetRemoved().GetUid() != f.target.Uid {
		t.Fatalf("account loss = %v, %v", removed, err)
	}
}

func TestCollectionWatchTracksExecutionAccountChangesForBroadReaders(t *testing.T) {
	f := newWatchFixture(t)
	stream, err := f.rpc.WatchEnvironments(f.readerCtx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	initial := receiveCollection(t, stream)
	if len(initial) != 1 || initial[0].GetExecutionAccount().GetUid() == "" {
		t.Fatalf("initial broad-reader snapshot = %v", initial)
	}
	initialAccountUID := initial[0].GetExecutionAccount().GetUid()
	var account corev1.ServiceAccount
	key := types.NamespacedName{Namespace: f.target.Namespace, Name: f.target.Name}
	if err := f.objects.Get(f.ctx, key, &account); err != nil {
		t.Fatal(err)
	}
	account.OwnerReferences = nil
	if err := f.objects.Update(f.ctx, &account); err != nil {
		t.Fatal(err)
	}
	lost, err := stream.Recv()
	if err != nil || lost.GetUpsert() == nil || lost.GetUpsert().GetExecutionAccount() != nil {
		t.Fatalf("broad-reader account-loss update = %v, %v", lost, err)
	}
	if err := f.objects.Get(f.ctx, key, &account); err != nil {
		t.Fatal(err)
	}
	account.OwnerReferences = executionAccountObject(types.UID(f.target.Uid)).OwnerReferences
	if err := f.objects.Update(f.ctx, &account); err != nil {
		t.Fatal(err)
	}
	restored, err := stream.Recv()
	if err != nil || restored.GetUpsert().GetExecutionAccount().GetUid() != initialAccountUID {
		t.Fatalf("broad-reader account-restored update = %v, %v", restored, err)
	}
}

func TestCollectionWatchRevalidatesIdleCredentialWithoutRenewal(t *testing.T) {
	f := newWatchFixture(t)
	stream, err := f.rpc.WatchEnvironments(f.readerCtx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	receiveCollection(t, stream)
	first := f.reviews.calls.Load()
	for f.reviews.calls.Load() < first+2 {
		select {
		case <-time.After(10 * time.Millisecond):
		case <-f.ctx.Done():
			t.Fatal("idle credentials were not revalidated")
		}
	}
	other := sandboxObject("")
	other.Namespace, other.Name = f.target.Namespace, "created"
	if err := f.objects.Create(f.ctx, other); err != nil {
		t.Fatal(err)
	}
	frame, err := stream.Recv()
	if err != nil || frame.GetUpsert().GetTarget().GetUid() != string(other.UID) {
		t.Fatalf("stream did not remain open: %v, %v", frame, err)
	}
	f.reviews.revoked.Store(true)
	_, err = stream.Recv()
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("revoked idle stream: %v, want Unauthenticated", err)
	}
}

func TestCollectionWatchTracksIncarnationPodChanges(t *testing.T) {
	f := newWatchFixture(t)
	stream, err := f.rpc.WatchEnvironments(f.readerCtx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	receiveCollection(t, stream)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "vm-launcher", Namespace: f.target.Namespace},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "runner", Image: "test"}}},
	}
	if err := f.objects.Create(f.ctx, pod); err != nil {
		t.Fatal(err)
	}
	sandbox := f.sandbox(t)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: pod.Name, UID: string(pod.UID)}
	if err := f.objects.Status().Update(f.ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	frame, err := stream.Recv()
	if err != nil || frame.GetUpsert().GetKubernetesDiagnostics().GetPod().GetUid() != string(pod.UID) {
		t.Fatalf("incarnation upsert = %v, %v", frame, err)
	}
	pod.Status.Phase = corev1.PodRunning
	if err := f.objects.Status().Update(f.ctx, pod); err != nil {
		t.Fatal(err)
	}
	frame, err = stream.Recv()
	if err != nil || frame.GetUpsert().GetKubernetesDiagnostics().GetPod().GetStatus().AsMap()["phase"] != "Running" {
		t.Fatalf("Pod status upsert = %v, %v", frame, err)
	}
}

func TestCollectionWatchFencesSameNameReplacement(t *testing.T) {
	f := newWatchFixture(t)
	stream, err := f.rpc.WatchEnvironments(f.readerCtx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	receiveCollection(t, stream)
	old := f.sandbox(t)
	if err := f.objects.Delete(f.ctx, old); err != nil {
		t.Fatal(err)
	}
	removed, err := stream.Recv()
	if err != nil || removed.GetRemoved().GetUid() != string(old.UID) {
		t.Fatalf("removed = %v, %v", removed, err)
	}
	replacement := sandboxObject("")
	replacement.Namespace = f.target.Namespace
	if err := f.objects.Create(f.ctx, replacement); err != nil {
		t.Fatal(err)
	}
	updated, err := stream.Recv()
	if err != nil || updated.GetUpsert().GetTarget().GetUid() != string(replacement.UID) || replacement.UID == old.UID {
		t.Fatalf("replacement = %v, %v (old uid %s)", updated, err, old.UID)
	}
}
