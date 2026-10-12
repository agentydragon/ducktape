// The Kubernetes Policy Controller spike's entry point.
package main

import (
	"flag"
	"os"
	"strings"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	kubernetespolicy "github.com/agentydragon/ducktape/agentplane/x/kubernetes_policy_go"
)

func main() {
	policyNamespace := flag.String("policy-namespace", "", "where the PolicySets and PolicyBindings live")
	subjectNamespaces := flag.String("subject-namespaces", "", "comma-separated namespaces whose ServiceAccounts may be bound")
	flag.Parse()
	ctrl.SetLogger(zap.New())
	log := ctrl.Log.WithName("kubernetes-policy")
	if err := run(*policyNamespace, strings.Split(*subjectNamespaces, ",")); err != nil {
		log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run(policyNamespace string, subjectNamespaces []string) error {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := kubernetespolicy.AddToScheme(scheme); err != nil {
		return err
	}
	ours := labels.SelectorFromSet(labels.Set{
		kubernetespolicy.ManagedByLabel:     kubernetespolicy.FieldManager,
		kubernetespolicy.PolicyNamespaceLbl: policyNamespace,
	})
	only := func(namespaces ...string) map[string]cache.Config {
		out := map[string]cache.Config{}
		for _, ns := range namespaces {
			out[ns] = cache.Config{}
		}
		return out
	}
	// The cache holds only what the controller reads: its own namespace's policy objects, the
	// subject namespaces' accounts, and the native bindings carrying its label.
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{ByObject: map[client.Object]cache.ByObject{
			&kubernetespolicy.PolicySet{}:     {Namespaces: only(policyNamespace)},
			&kubernetespolicy.PolicyBinding{}: {Namespaces: only(policyNamespace)},
			&corev1.ServiceAccount{}:          {Namespaces: only(subjectNamespaces...)},
			&rbacv1.RoleBinding{}:             {Label: ours},
			&rbacv1.ClusterRoleBinding{}:      {Label: ours},
		}},
	})
	if err != nil {
		return err
	}
	reconciler := &kubernetespolicy.Reconciler{
		Client:            mgr.GetClient(),
		Roles:             mgr.GetAPIReader(),
		PolicyNamespace:   policyNamespace,
		SubjectNamespaces: subjectNamespaces,
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		return err
	}
	return mgr.Start(ctrl.SetupSignalHandler())
}
