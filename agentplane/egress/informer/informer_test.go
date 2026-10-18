package informer

import (
	"context"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestIndexFollowsTheAPIServer(t *testing.T) {
	f := newFixture(t)
	secret := f.secret(f.credentials, "test-secret", "first-value")
	f.create(secret)
	// Only the credentials namespace is read, and the index keys a Secret by name alone.
	f.create(f.secret(f.elsewhere, "test-elsewhere", "other-namespace-value"))
	credential := f.credential("test-cred", "test-secret")
	f.create(credential)
	f.create(f.credential("test-cred-elsewhere", "test-elsewhere"))
	f.create(f.policySet("test-egress", egressRule("test-cred"), egressRule("test-cred-elsewhere")))
	grant := f.policyBinding("test-grant", "test-egress")
	f.create(grant)
	f.start()

	allowed := func(substitution string) policy.Decision {
		result := policy.Allowed{Binding: "test-grant", Policy: "test-egress", Rule: 0}
		if substitution != "" {
			result.Rewrites = substituted(substitution)
		}
		return result
	}

	f.eventually("the index to sync", func() bool { return f.index.Available(time.Now()) })
	f.eventuallyDecides("test-cred", allowed("first-value"))
	f.eventuallyDecides("test-cred-elsewhere", policy.Denied{Reason: decision.CredentialUnavailable})

	secret.Data["token"] = []byte("second-value")
	f.update(secret)
	f.eventuallyDecides("test-cred", allowed("second-value"))

	f.remove(credential)
	f.eventuallyDecides("test-cred", allowed(""))

	f.remove(grant)
	f.eventuallyDecides("test-cred", policy.Denied{Reason: decision.NoBinding})
}

func TestRelistRemovesNamesThatVanishedWhileTheWatchWasDown(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-stays", egressRule("")))
	vanishes := f.policySet("test-vanishes", egressRule(""))
	f.create(vanishes)
	f.start()
	names := []string{"test-stays", "test-vanishes", "test-arrives"}
	f.eventually("the first list", func() bool {
		got := f.index.Resolve(names)
		return got.Rules == 2 && slices.Equal(got.Missing, []string{"test-arrives"})
	})

	// The watch is cut and cannot be resumed, so only a relist can show what changed meanwhile.
	release := f.faults.hold(policy.PolicySets)
	f.faults.sever(policy.PolicySets)
	listed := f.faults.listCount(policy.PolicySets)
	f.remove(vanishes)
	f.create(f.policySet("test-arrives", egressRule("")))
	f.faults.expire(policy.PolicySets)
	release()

	f.eventually("a relist", func() bool { return f.faults.listCount(policy.PolicySets) > listed })
	f.eventually("the relist to be applied", func() bool {
		got := f.index.Resolve(names)
		return got.Rules == 2 && slices.Equal(got.Missing, []string{"test-vanishes"})
	})
}

func TestAKindThatStopsCompletingCyclesGoesStale(t *testing.T) {
	f := newFixture(t)
	f.start()
	kinds := []policy.Kind{policy.PolicySets, policy.Bindings, policy.Credentials, policy.Secrets}
	f.eventually("every kind to complete a watch period", func() bool {
		status := f.index.Status(time.Now())
		for _, kind := range kinds {
			// A completed cycle is at least a period old by the time it is recorded; the seed is not.
			if age, recorded := status.Ages[kind]; !recorded || age > time.Since(f.started)-watchPeriod/2 {
				return false
			}
		}
		return status.Synced && status.Available
	})

	// Its watches stop returning while the rest of the API server stays healthy.
	f.faults.hold(policy.PolicySets)
	var status policy.Status
	f.eventually("the index to turn unavailable", func() bool {
		status = f.index.Status(time.Now())
		return !status.Available
	})
	if !status.Synced || status.Ages[policy.PolicySets] <= status.StaleAfter {
		t.Errorf("unavailable because of something else than the wedged kind: %+v", status)
	}
	for _, kind := range kinds[1:] {
		if status.Ages[kind] > status.StaleAfter {
			t.Errorf("%s went stale although its watches kept returning: %+v", kind, status)
		}
	}
}

func TestEgressReadyFollowsTheSetsABindingNames(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-early", egressRule(""), egressRule("")))
	f.create(f.policyBinding("test-grant", "test-early", "test-late"))
	// Another reader's condition, which this one must leave alone.
	grant := f.binding("test-grant")
	grant.Status.Conditions = []metav1.Condition{{
		Type: "KubernetesReady", Status: metav1.ConditionTrue, Reason: "Applied", Message: "test-message", LastTransitionTime: metav1.Now(),
	}}
	if err := apiClient.Status().Update(context.Background(), grant); err != nil {
		t.Fatal(err)
	}
	f.start()

	missing := f.eventuallyEgressReady("test-grant", func(*metav1.Condition) bool { return true })
	if missing.Status != metav1.ConditionFalse || missing.Reason != policyapi.PolicySetUnavailable ||
		!strings.Contains(missing.Message, "test-late") || strings.Contains(missing.Message, "test-early") {
		t.Errorf("a binding naming a set that does not exist: %+v", missing)
	}
	written := f.binding("test-grant")
	if other := meta.FindStatusCondition(written.Status.Conditions, "KubernetesReady"); other == nil || other.Message != "test-message" {
		t.Errorf("another reader's condition did not survive: %+v", written.Status.Conditions)
	}
	if !slices.ContainsFunc(written.ManagedFields, func(entry metav1.ManagedFieldsEntry) bool {
		return entry.Manager == FieldManager && entry.Operation == metav1.ManagedFieldsOperationApply && entry.Subresource == "status"
	}) {
		t.Errorf("EgressReady was not applied by %s: %+v", FieldManager, written.ManagedFields)
	}

	late := f.policySet("test-late", egressRule(""))
	f.create(late)
	ready := f.eventuallyEgressReady("test-grant", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionTrue })
	if ready.Reason != enforced || ready.ObservedGeneration != written.Generation {
		t.Errorf("a binding whose sets all exist: %+v", ready)
	}

	// Another rule changes what is enforced, not whether it is: the message follows, the transition does not.
	late.Spec.Rules = append(late.Spec.Rules, egressRule(""))
	f.update(late)
	recounted := f.eventuallyEgressReady("test-grant", func(c *metav1.Condition) bool { return c.Message != ready.Message })
	if recounted.Status != metav1.ConditionTrue || !recounted.LastTransitionTime.Equal(&ready.LastTransitionTime) {
		t.Errorf("a set that changed without changing readiness: %+v", recounted)
	}

	f.remove(late)
	f.eventuallyEgressReady("test-grant", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionFalse })
}

func TestEgressReadyIsNotWrittenBeforeEveryKindHasListed(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-egress", egressRule("")))
	f.create(f.policyBinding("test-grant", "test-egress"))
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	watcher, err := client.NewWithWatch(apiConfig, client.Options{Scheme: apiScheme})
	if err != nil {
		t.Fatal(err)
	}
	events, err := watcher.Watch(ctx, &policyapi.PolicyBindingList{}, client.InNamespace(f.rules))
	if err != nil {
		t.Fatal(err)
	}
	defer events.Stop()

	// With the sets unread, a binding naming one would be judged to name a set that does not exist.
	release := f.faults.hold(policy.PolicySets)
	f.start()
	f.eventually("the binding to be read", func() bool {
		_, err := f.informer.bindings.Get("test-grant")
		return err == nil
	})
	release()
	f.eventuallyEgressReady("test-grant", func(*metav1.Condition) bool { return true })

	for event := range events.ResultChan() {
		if binding, ok := event.Object.(*policyapi.PolicyBinding); ok {
			if first := meta.FindStatusCondition(binding.Status.Conditions, EgressReady); first != nil {
				if first.Status != metav1.ConditionTrue {
					t.Errorf("the first EgressReady written was %+v", first)
				}
				return
			}
		}
	}
	t.Error("no EgressReady was written")
}

func TestEgressReadyIsNotRewrittenWhenUnchanged(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-egress", egressRule("")))
	f.create(f.policyBinding("test-first", "test-egress"))
	f.start()
	f.eventuallyEgressReady("test-first", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionTrue })
	f.eventually("the informer to see its own write", func() bool {
		binding, err := f.informer.bindings.Get("test-first")
		return err == nil && meta.FindStatusCondition(binding.Status.Conditions, EgressReady) != nil
	})
	written := f.faults.patchCount("test-first")

	// The queue is in order, so the second binding is judged after the first is judged again.
	f.informer.enqueueBindings()
	f.create(f.policyBinding("test-second", "test-egress"))
	f.eventuallyEgressReady("test-second", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionTrue })

	if got := f.faults.patchCount("test-first"); got != written {
		t.Errorf("a binding already carrying its condition was written %d more times", got-written)
	}
}

func TestEgressReadyNamesWhatIsUnavailable(t *testing.T) {
	refused := egressReady(policy.Resolution{
		Missing: []string{"test-late"}, Refused: []policy.Refusal{{Set: "test-broken", Reason: "test reason"}}, Rules: 3,
	})
	if refused.Status != metav1.ConditionFalse || refused.Reason != policyapi.PolicySetUnavailable ||
		!strings.Contains(refused.Message, "test-late") || !strings.Contains(refused.Message, "test-broken") {
		t.Errorf("a binding with a missing and a refused set: %+v", refused)
	}
	if ready := egressReady(policy.Resolution{Rules: 3}); ready.Status != metav1.ConditionTrue || ready.Reason != enforced {
		t.Errorf("a binding whose sets all parse: %+v", ready)
	}
}

// The CRD schema rejects every credential this parser refuses, so only one stored under an older
// schema can reach it.
func TestACredentialThatDoesNotParseIsRemoved(t *testing.T) {
	index := policy.NewIndex(time.Hour)
	index.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-egress"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{egressRule("test-cred")}},
	})
	index.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "test-grant"},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: []string{"test-egress"}},
	})
	index.PutSecret(resources.NewSecret("test-secret", map[string]string{"token": "test-value"}))
	informer := &Informer{index: index}

	credential := credentialObject("test-rules", "test-cred", "test-secret")
	informer.putCredential(credential)
	if got, want := decide(index, "test-cred"), (policy.Allowed{
		Binding: "test-grant", Policy: "test-egress", Rewrites: substituted("test-value"),
	}); !reflect.DeepEqual(got, want) {
		t.Fatalf("decision = %#v, want %#v", got, want)
	}

	credential.Spec.Targets = nil
	informer.putCredential(credential)
	if got, want := decide(index, "test-cred"), (policy.Allowed{Binding: "test-grant", Policy: "test-egress"}); !reflect.DeepEqual(got, want) {
		t.Errorf("decision = %#v, want %#v", got, want)
	}
}
