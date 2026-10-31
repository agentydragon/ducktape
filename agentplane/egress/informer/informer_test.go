package informer

import (
	"errors"
	"net/http"
	"path"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"
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

	f.eventually("the index to sync", f.index.Health.Available)
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

// Credentials and Secrets are watched beside the rules, through clients that record their health: a
// kind whose requests fail keeps the index unavailable and is named as failing.
func TestAFailingKindKeepsTheIndexUnavailable(t *testing.T) {
	for _, kind := range []watchhealth.Kind{policy.Credentials, policy.Secrets} {
		t.Run(string(kind), func(t *testing.T) {
			f := newFixture(t)
			base := rest.CopyConfig(apiConfig)
			base.Wrap(func(next http.RoundTripper) http.RoundTripper {
				return roundTripFunc(func(request *http.Request) (*http.Response, error) {
					if watchhealth.Kind(path.Base(request.URL.Path)) == kind {
						return nil, errors.New("test fault: the API server is unreachable")
					}
					return next.RoundTrip(request)
				})
			})
			f.startThrough(base)
			f.eventually(string(kind)+" to be reported failing", func() bool {
				_, failing := f.index.Health.Status().Failing[kind]
				return failing
			})
			if f.index.Health.Available() {
				t.Errorf("the index is available while %s fails", kind)
			}
		})
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

// The CRD schema rejects every credential this parser refuses, so only one stored under an older
// schema can reach it.
func TestACredentialThatDoesNotParseIsRemoved(t *testing.T) {
	index := policy.NewIndex()
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
