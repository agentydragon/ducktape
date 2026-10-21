package informer

import (
	"reflect"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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

	f.eventually("the index to sync", func() bool { return f.index.Freshness.Available(time.Now()) })
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

// Credentials and Secrets are watched beside the rules, through clients that record their cycles.
func TestEveryWatchedKindCompletesCycles(t *testing.T) {
	f := newFixture(t)
	f.start()
	f.eventually("every kind to complete a watch period", func() bool {
		status := f.index.Freshness.Status(time.Now())
		for _, kind := range []reader.Kind{reader.PolicySets, reader.PolicyBindings, policy.Credentials, policy.Secrets} {
			// A completed cycle is at least a period old by the time it is recorded; the seed is not.
			if age, recorded := status.Ages[kind]; !recorded || age > time.Since(f.started)-watchPeriod/2 {
				return false
			}
		}
		return status.Synced && status.Available
	})
}

// The CRD schema rejects every credential this parser refuses, so only one stored under an older
// schema can reach it.
func TestACredentialThatDoesNotParseIsRemoved(t *testing.T) {
	index := policy.NewIndex(time.Hour)
	index.Rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "test-egress"},
		Spec:       policyapi.PolicySetSpec{Rules: []policyapi.Rule{egressRule("test-cred")}},
	})
	index.Rules.PutBinding(&policyapi.PolicyBinding{
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
