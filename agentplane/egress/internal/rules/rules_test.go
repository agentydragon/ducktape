package rules

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/internal/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	description = "a token for the bot account, which can write to its own repositories"
	secretValue = "the-real-credential"
)

var kube *kubeapiserver.Server

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		kube = server
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

func policySet(namespace, name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func binding(namespace, identity, name string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity}}, PolicySets: sets,
		},
	}
}

func githubIndex(caller *policyapi.Identity) *policy.Index {
	index := policy.NewIndex()
	index.Rules.PutIdentity(caller)
	index.Rules.PutPolicySet(policySet(caller.Namespace, "github", githubRule, openRule))
	index.Rules.PutPolicySet(policySet(caller.Namespace, "other-only", openRule))
	index.Rules.PutBinding(binding(caller.Namespace, "sb", "b-sb", "github"))
	index.Rules.PutBinding(binding(caller.Namespace, "other", "b-other", "other-only"))
	index.PutCredential(githubCredential)
	index.PutSecret(resources.NewSecret("vault-entry", map[string]string{"credential-key": secretValue}))
	return index
}
