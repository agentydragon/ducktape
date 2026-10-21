package reader

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyapply "github.com/agentydragon/ducktape/agentplane/policy/client/apply"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	enforced = "Enforced"
	// messageLimit is the CRD's maxLength for a condition message.
	messageLimit = 32768
)

// Condition is the condition a reader keeps on each PolicyBinding: the one write a reader makes to
// the API server.
type Condition struct {
	// Type is the condition type, such as EgressReady.
	Type string
	// FieldManager owns the condition, so server-side apply leaves other readers' conditions on the
	// binding alone.
	FieldManager string
	// Rules names the reader's rules in a ready condition's message, such as "egress rules".
	Rules string
}

// judge is the condition a binding's named sets give this reader: ready when every set exists and
// parses. The sets that do parse grant either way.
func (c Condition) judge(resolution Resolution) metav1.Condition {
	if len(resolution.Missing) == 0 && len(resolution.Refused) == 0 {
		return metav1.Condition{
			Type: c.Type, Status: metav1.ConditionTrue, Reason: enforced,
			Message: fmt.Sprintf("%s: %d", c.Rules, resolution.Rules),
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
		Type: c.Type, Status: metav1.ConditionFalse, Reason: policyapi.PolicySetUnavailable,
		Message: message[:min(len(message), messageLimit)],
	}
}

// reportReadiness keeps the condition on the queued bindings until the queue shuts down. A binding
// is judged again whenever it or any PolicySet changes, and a write that fails is retried with
// backoff.
func (r *rules[R]) reportReadiness(ctx context.Context) {
	for {
		name, shutdown := r.queue.Get()
		if shutdown {
			return
		}
		if err := r.apply(ctx, name); err != nil {
			slog.Warn("PolicyBinding condition write failed", "binding", name, "condition", r.condition.Type, "error", err)
			r.queue.AddRateLimited(name)
		} else {
			r.queue.Forget(name)
		}
		r.queue.Done(name)
	}
}

// apply writes the binding's condition only when what the index decides differs from what the
// binding carries. A binding that is gone, or deleted before the write lands, needs none.
func (r *rules[R]) apply(ctx context.Context, name string) error {
	binding, err := r.bindings.Get(name)
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	configuration := policyapply.PrepareConditionApply(binding, r.condition.judge(r.index.Resolve(binding.Spec.PolicySets)))
	if configuration == nil {
		return nil
	}
	_, err = r.bindingsClient.ApplyStatus(ctx, configuration, metav1.ApplyOptions{FieldManager: r.condition.FieldManager, Force: true})
	if apierrors.IsNotFound(err) {
		return nil
	}
	return err
}
