// The Policy Controller's entry point.
package main

import (
	"context"
	"log/slog"
	"net"
	"os"
	"slices"

	"github.com/go-logr/logr"
	"google.golang.org/grpc"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
	policyservice "github.com/agentydragon/ducktape/agentplane/policy_controller/service"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

func main() {
	ctrl.SetLogger(logr.FromSlogHandler(slog.NewJSONHandler(os.Stderr, nil)))
	log := ctrl.Log.WithName("policy-controller")
	c, err := loadConfig()
	if err == nil {
		var shutdown func()
		shutdown, err = observability.Init(context.Background(), "policy-controller")
		if err == nil {
			err = run(c)
			shutdown()
		}
	}
	if err != nil {
		log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run(c config) error {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return err
	}
	ours := labels.SelectorFromSet(labels.Set{
		policycontroller.ManagedByLabel:     policycontroller.FieldManager,
		policycontroller.PolicyNamespaceLbl: c.PolicyNamespace,
	})
	only := func(namespaces ...string) map[string]cache.Config {
		out := map[string]cache.Config{}
		for _, ns := range namespaces {
			out[ns] = cache.Config{}
		}
		return out
	}
	// The cache holds only what the controller reads: its own namespace's policy objects, the
	// subject and accounts namespaces' ServiceAccounts, and the native bindings carrying its label.
	config := ctrl.GetConfigOrDie()
	config.WrapTransport = observability.WrapHTTPTransport(config.WrapTransport)
	mgr, err := ctrl.NewManager(config, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: c.MetricsBindAddress},
		HealthProbeBindAddress: c.HealthProbeBindAddress,
		// Replicas take turns: only the Lease holder reconciles.
		LeaderElection:          true,
		LeaderElectionID:        "agentplane-policy-controller",
		LeaderElectionNamespace: c.PolicyNamespace,
		Cache: cache.Options{ByObject: map[client.Object]cache.ByObject{
			&policyapi.PolicySet{}:             {Namespaces: only(c.PolicyNamespace)},
			&policyapi.PolicyBinding{}:         {Namespaces: only(c.PolicyNamespace)},
			&policyapi.KubernetesRoleSet{}:     {Namespaces: only(c.PolicyNamespace)},
			&policyapi.ServiceAccountGrant{}:   {Namespaces: only(c.PolicyNamespace)},
			&policyapi.ManagedServiceAccount{}: {Namespaces: only(c.PolicyNamespace)},
			&corev1.ServiceAccount{}:           {Namespaces: only(append(slices.Clone(c.SubjectNamespaces), c.AccountsNamespace)...)},
			&rbacv1.RoleBinding{}:              {Label: ours},
			&rbacv1.ClusterRoleBinding{}:       {Label: ours},
		}},
	})
	if err != nil {
		return err
	}
	readers := make([]auth.Account, 0, len(c.ReaderAccounts))
	namespaces := slices.Clone(c.SubjectNamespaces)
	for _, name := range c.ReaderAccounts {
		var account auth.Account
		if err := account.UnmarshalText([]byte(name)); err != nil {
			return err
		}
		readers = append(readers, account)
		if !slices.Contains(namespaces, account.Namespace) {
			namespaces = append(namespaces, account.Namespace)
		}
	}
	writers := make([]auth.Account, 0, len(c.WriterAccounts))
	writerNamespaces := make([]string, 0, len(c.WriterAccounts))
	for _, name := range c.WriterAccounts {
		var account auth.Account
		if err := account.UnmarshalText([]byte(name)); err != nil {
			return err
		}
		writers = append(writers, account)
		if !slices.Contains(writerNamespaces, account.Namespace) {
			writerNamespaces = append(writerNamespaces, account.Namespace)
		}
	}
	kube, err := kubernetes.NewForConfig(config)
	if err != nil {
		return err
	}
	readResolver, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.GRPCAudience, namespaces)
	if err != nil {
		return err
	}
	writeResolver := readResolver
	if len(writerNamespaces) > 0 {
		writeResolver, err = auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.GRPCAudience, writerNamespaces)
		if err != nil {
			return err
		}
	}
	objects, err := client.NewWithWatch(config, client.Options{Scheme: scheme})
	if err != nil {
		return err
	}
	api, err := policyservice.New(c.PolicyNamespace, objects, readers, writers)
	if err != nil {
		return err
	}
	server := grpc.NewServer(api.Methods(readResolver, writeResolver).ServerOptions(0)...)
	policypb.RegisterPolicyServiceServer(server, api)
	listener, err := net.Listen("tcp", c.GRPCBindAddress)
	if err != nil {
		return err
	}
	if err := mgr.Add(policyListener{server: server, listener: listener}); err != nil {
		listener.Close()
		return err
	}
	reconciler := &policycontroller.Reconciler{
		Client:            mgr.GetClient(),
		APIReader:         mgr.GetAPIReader(),
		PolicyNamespace:   c.PolicyNamespace,
		SubjectNamespaces: c.SubjectNamespaces,
		AccountsNamespace: c.AccountsNamespace,
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		return err
	}
	if err := mgr.AddHealthzCheck("ping", healthz.Ping); err != nil {
		return err
	}
	if err := mgr.AddReadyzCheck("ping", healthz.Ping); err != nil {
		return err
	}
	return mgr.Start(ctrl.SetupSignalHandler())
}

// Read watches run on every replica; only the reconciler participates in leader election.
type policyListener struct {
	server   *grpc.Server
	listener net.Listener
}

func (policyListener) NeedLeaderElection() bool { return false }
func (p policyListener) Start(ctx context.Context) error {
	go func() { <-ctx.Done(); p.server.Stop() }()
	err := p.server.Serve(p.listener)
	if ctx.Err() != nil {
		return nil
	}
	return err
}
