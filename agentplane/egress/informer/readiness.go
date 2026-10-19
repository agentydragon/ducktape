package informer

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyapply "github.com/agentydragon/ducktape/agentplane/policy/client/apply"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	// EgressReady is the condition type this proxy keeps on each PolicyBinding.
	EgressReady = "EgressReady"
	// FieldManager owns the EgressReady condition, so server-side apply leaves other readers' conditions
	// on the binding alone.
	FieldManager = "agentplane-egress"

	enforced = "Enforced"
	// messageLimit is the CRD's maxLength for a condition message.
	messageLimit = 32768
)

// egressReady is the condition a binding's named sets give this proxy: ready when every set exists
// and parses. The sets that do parse grant either way.
func egressReady(resolution policy.Resolution) metav1.Condition {
	if len(resolution.Missing) == 0 && len(resolution.Refused) == 0 {
		return metav1.Condition{
			Type: EgressReady, Status: metav1.ConditionTrue, Reason: enforced,
			Message: fmt.Sprintf("egress rules: %d", resolution.Rules),
		}
	}
	var unavailable []string
	if len(resolution.Missing) > 0 {
		unavailable = append(unavailable, "missing PolicySets: "+strings.Join(resolution.Missing, ", "))
	}
	for _, refusal := range resolution.Refused {
		unavailable = append(unavailable, fmt.Sprintf("PolicySet %s refused: %s", refusal.Set, refusal.Reason))
	}
	message := strings.Join(unavailable, "; ")
	return metav1.Condition{
		Type: EgressReady, Status: metav1.ConditionFalse, Reason: policyapi.PolicySetUnavailable,
		Message: message[:min(len(message), messageLimit)],
	}
}

// reportReadiness keeps EgressReady on the queued bindings until ctx is done. A binding is judged
// again whenever it or any PolicySet changes, and a write that fails is retried with backoff.
func (i *Informer) reportReadiness(ctx context.Context) {
	for {
		name, shutdown := i.queue.Get()
		if shutdown {
			return
		}
		if err := i.applyEgressReady(ctx, name); err != nil {
			slog.Warn("EgressReady write failed", "binding", name, "error", err)
			i.queue.AddRateLimited(name)
		} else {
			i.queue.Forget(name)
		}
		i.queue.Done(name)
	}
}

// applyEgressReady writes the binding's EgressReady only when what the index decides differs from
// what the binding carries. A binding that is gone, or deleted before the write lands, needs none.
func (i *Informer) applyEgressReady(ctx context.Context, name string) error {
	binding, err := i.bindings.Get(name)
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	configuration := policyapply.PrepareConditionApply(binding, egressReady(i.index.Resolve(binding.Spec.PolicySets)))
	if configuration == nil {
		return nil
	}
	_, err = i.bindingsClient.ApplyStatus(ctx, configuration, metav1.ApplyOptions{FieldManager: FieldManager, Force: true})
	if apierrors.IsNotFound(err) {
		return nil
	}
	return err
}
