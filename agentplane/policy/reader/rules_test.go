package reader

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestRelistRemovesNamesThatVanishedWhileTheWatchWasDown(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-stays", testRule()))
	vanishes := f.policySet("test-vanishes", testRule())
	f.create(vanishes)
	f.start()
	names := []string{"test-stays", "test-vanishes", "test-arrives"}
	f.eventually("the first list", func() bool {
		got := f.index.Resolve(names)
		return got.Rules() == 2 && slices.Equal(got.Missing, []string{"test-arrives"})
	})

	// The watch is cut and cannot be resumed, so only a relist can show what changed meanwhile.
	release := f.faults.hold(PolicySets)
	f.faults.sever(PolicySets)
	listed := f.faults.listCount(PolicySets)
	f.remove(vanishes)
	f.create(f.policySet("test-arrives", testRule()))
	f.faults.expire(PolicySets)
	release()

	f.eventually("a relist", func() bool { return f.faults.listCount(PolicySets) > listed })
	f.eventually("the relist to be applied", func() bool {
		got := f.index.Resolve(names)
		return got.Rules() == 2 && slices.Equal(got.Missing, []string{"test-vanishes"})
	})
}

func TestAKindWhoseRequestsFailIsUnavailableUntilItsWatchResumes(t *testing.T) {
	f := newFixture(t)
	f.start()
	f.eventually("the first sync", f.health.Available)

	// Its requests fail while the rest of the API server stays healthy, and its open watch is cut, so
	// client-go's next request for it fails.
	restore := f.faults.fail(PolicySets)
	f.faults.sever(PolicySets)
	var status watchhealth.Status
	f.eventually("the view to turn unavailable", func() bool {
		status = f.health.Status()
		return !status.Available
	})
	if _, failing := status.Failing[PolicySets]; !status.Synced || !failing {
		t.Errorf("unavailable because of something else than the failing kind: %+v", status)
	}
	if _, failing := status.Failing[PolicyBindings]; failing {
		t.Errorf("policybindings failed although its requests kept succeeding: %+v", status)
	}

	restore()
	f.eventually("the view to recover once its requests succeed again", f.health.Available)
}

func TestTheConditionFollowsTheSetsABindingNames(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-early", testRule(), testRule()))
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

	missing := f.eventuallyCondition("test-grant", func(*metav1.Condition) bool { return true })
	if missing.Status != metav1.ConditionFalse || missing.Reason != policyapi.PolicySetUnavailable ||
		!strings.Contains(missing.Message, "test-late") || strings.Contains(missing.Message, "test-early") {
		t.Errorf("a binding naming a set that does not exist: %+v", missing)
	}
	written := f.binding("test-grant")
	if other := meta.FindStatusCondition(written.Status.Conditions, "KubernetesReady"); other == nil || other.Message != "test-message" {
		t.Errorf("another reader's condition did not survive: %+v", written.Status.Conditions)
	}
	if !slices.ContainsFunc(written.ManagedFields, func(entry metav1.ManagedFieldsEntry) bool {
		return entry.Manager == testCondition.FieldManager && entry.Operation == metav1.ManagedFieldsOperationApply && entry.Subresource == "status"
	}) {
		t.Errorf("the condition was not applied by %s: %+v", testCondition.FieldManager, written.ManagedFields)
	}

	late := f.policySet("test-late", testRule())
	f.create(late)
	ready := f.eventuallyCondition("test-grant", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionTrue })
	if ready.Reason != testCondition.Reason || ready.ObservedGeneration != written.Generation {
		t.Errorf("a binding whose sets all exist: %+v", ready)
	}

	// Another rule changes what is enforced, not whether it is: the message follows, the transition does not.
	late.Spec.Rules = append(late.Spec.Rules, testRule())
	f.update(late)
	recounted := f.eventuallyCondition("test-grant", func(c *metav1.Condition) bool { return c.Message != ready.Message })
	if recounted.Status != metav1.ConditionTrue || !recounted.LastTransitionTime.Equal(&ready.LastTransitionTime) {
		t.Errorf("a set that changed without changing readiness: %+v", recounted)
	}

	f.remove(late)
	f.eventuallyCondition("test-grant", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionFalse })
}

func TestTheConditionIsNotWrittenBeforeEveryKindHasListed(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-set", testRule()))
	f.create(f.policyBinding("test-grant", "test-set"))
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	watcher, err := client.NewWithWatch(apiConfig, client.Options{Scheme: apiScheme})
	if err != nil {
		t.Fatal(err)
	}
	events, err := watcher.Watch(ctx, &policyapi.PolicyBindingList{}, client.InNamespace(f.namespace))
	if err != nil {
		t.Fatal(err)
	}
	defer events.Stop()

	// With the sets unread, a binding naming one would be judged to name a set that does not exist.
	release := f.faults.hold(PolicySets)
	f.start()
	f.eventually("the binding to be read", func() bool {
		_, err := f.reader.bindings.Get("test-grant")
		return err == nil
	})
	release()
	f.eventuallyCondition("test-grant", func(*metav1.Condition) bool { return true })

	for event := range events.ResultChan() {
		if binding, ok := event.Object.(*policyapi.PolicyBinding); ok {
			if first := meta.FindStatusCondition(binding.Status.Conditions, testCondition.Type); first != nil {
				if first.Status != metav1.ConditionTrue {
					t.Errorf("the first condition written was %+v", first)
				}
				return
			}
		}
	}
	t.Error("no condition was written")
}

func TestTheConditionIsNotRewrittenWhenUnchanged(t *testing.T) {
	f := newFixture(t)
	f.create(f.policySet("test-set", testRule()))
	f.create(f.policyBinding("test-first", "test-set"))
	f.start()
	f.eventuallyCondition("test-first", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionTrue })
	f.eventually("the reader to see its own write", func() bool {
		binding, err := f.reader.bindings.Get("test-first")
		return err == nil && meta.FindStatusCondition(binding.Status.Conditions, testCondition.Type) != nil
	})
	written := f.faults.patchCount("test-first")

	// The queue is in order, so the second binding is judged after the first is judged again.
	f.reader.enqueueBindings()
	f.create(f.policyBinding("test-second", "test-set"))
	f.eventuallyCondition("test-second", func(c *metav1.Condition) bool { return c.Status == metav1.ConditionTrue })

	if got := f.faults.patchCount("test-first"); got != written {
		t.Errorf("a binding already carrying its condition was written %d more times", got-written)
	}
}
