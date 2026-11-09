package rules

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	hopAudience = "test-egress"
	namespace   = "test-workloads"
	description = "a token for the bot account, which can write to its own repositories"
	secretValue = "the-real-credential"
)

var (
	kube *kubeapiserver.Server
	// sbToken, otherToken and nobodyToken prove the ServiceAccounts sb, other and nobody in namespace.
	sbToken, otherToken, nobodyToken string
	unreachable                      = kubeapiserver.UnreachableReviews{Err: errors.New("test API server unreachable")}
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		kube = server
		for name, token := range map[string]*string{"sb": &sbToken, "other": &otherToken, "nobody": &nobodyToken} {
			account := auth.Account{Namespace: namespace, Name: name}
			if *token, err = server.WorkloadToken(context.Background(), account, hopAudience); err != nil {
				return err
			}
		}
		return nil
	}))
}

var (
	githubCredential = resources.Credential{
		Name:        "github-pat",
		Description: description,
		Source:      resources.SecretKey{Name: "vault-entry", Key: "credential-key"},
		Targets: []resources.Target{
			resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"},
			resources.BasicPassword{Header: "Authorization"},
		},
	}
	githubRule = policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{"api.github.com"}, Methods: []string{"GET", "POST"},
		Paths: []string{"/repos/**"}, CredentialRef: &policyapi.CredentialRef{Name: githubCredential.Name},
	}
	openRule = policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{"*.example.com"}}
)

func policySet(name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func binding(account, name string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.Subject{Namespace: namespace, Name: account}, PolicySets: sets,
		},
	}
}

func githubIndex() *policy.Index {
	index := policy.NewIndex()
	index.Rules.PutPolicySet(policySet("github", githubRule, openRule))
	index.Rules.PutPolicySet(policySet("other-only", openRule))
	index.Rules.PutBinding(binding("sb", "b-sb", "github"))
	index.Rules.PutBinding(binding("other", "b-other", "other-only"))
	index.PutCredential(githubCredential)
	index.PutSecret(resources.NewSecret("vault-entry", map[string]string{"credential-key": secretValue}))
	return index
}
