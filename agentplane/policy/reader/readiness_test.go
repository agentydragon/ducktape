package reader

import (
	"strings"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestTheConditionNamesWhatIsUnavailable(t *testing.T) {
	refused := testCondition.judge(Resolution{
		Missing: []string{"test-late"}, Refused: []Refusal{{Set: "test-broken", Reason: "test reason"}}, Rules: 3,
	})
	if refused.Type != testCondition.Type || refused.Status != metav1.ConditionFalse || refused.Reason != policyapi.PolicySetUnavailable ||
		!strings.Contains(refused.Message, "test-late") || !strings.Contains(refused.Message, "test-broken") {
		t.Errorf("a binding with a missing and a refused set: %+v", refused)
	}
	if ready := testCondition.judge(Resolution{Rules: 3}); ready.Type != testCondition.Type || ready.Status != metav1.ConditionTrue || ready.Reason != enforced {
		t.Errorf("a binding whose sets all parse: %+v", ready)
	}
}
