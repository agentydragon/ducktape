// The Sandbox Controller's entry point.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/go-logr/logr"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/discovery"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/environments/internal/controller"
	"github.com/agentydragon/ducktape/agentplane/environments/internal/runtimecontrol"
	"github.com/agentydragon/ducktape/agentplane/environments/internal/serving"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctrl.SetLogger(logr.FromSlogHandler(slog.Default().Handler()))
	log := ctrl.Log.WithName("sandbox-controller")
	c, err := loadConfig()
	if err == nil {
		var shutdown func()
		shutdown, err = observability.Init(context.Background(), "sandbox-controller")
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
	ctx := ctrl.SetupSignalHandler()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return err
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	if err := kubevirtv1.AddToScheme(scheme); err != nil {
		return err
	}
	if err := workloadapi.AddToScheme(scheme); err != nil {
		return err
	}
	restConfig := ctrl.GetConfigOrDie()
	restConfig.WrapTransport = observability.WrapHTTPTransport(restConfig.WrapTransport)
	kubeVirt, err := servesKubeVirt(restConfig)
	if err != nil {
		return err
	}
	if !kubeVirt {
		ctrl.Log.WithName("sandbox-controller").Info("KubeVirt's API is not served: VM Sandboxes stay unready")
	}
	byObject, err := sandboxcontroller.CacheByObject(kubeVirt)
	if err != nil {
		return err
	}
	mgr, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: c.MetricsBindAddress},
		HealthProbeBindAddress: c.HealthProbeBindAddress,
		// Replicas take turns: only the Lease holder reconciles.
		LeaderElection:          true,
		LeaderElectionID:        "agentplane-sandbox-controller",
		LeaderElectionNamespace: c.Namespace,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{c.Namespace: {}},
			ByObject:          byObject,
		},
	})
	if err != nil {
		return err
	}
	quiescer := runtimecontrol.GatewayQuiescer{Target: c.RunnerGatewayTarget, TokenFile: c.RunnerGatewayTokenFile, TLS: c.RunnerGatewayTLS}
	clientset, err := kubernetes.NewForConfig(restConfig)
	if err != nil {
		return err
	}
	credentials, err := requester.New(mgr.GetAPIReader(), mgr.GetClient())
	if err != nil {
		return err
	}
	states := sandboxcontroller.NewRuntimeStates(runtimecontrol.RuntimeWatcher{Security: c.RunnerSecurity}, time.Now)
	if err := mgr.Add(states); err != nil {
		return err
	}
	reconciler := &sandboxcontroller.Reconciler{
		Client: mgr.GetClient(), Objects: mgr.GetAPIReader(), KubeVirt: kubeVirt,
		Quiescer: quiescer, States: states, Credentials: credentials, Now: time.Now,
		FirecrackerStatus: sandboxcontroller.PodProxyFirecrackerStatusReader{Client: clientset},
		Suspender:         runtimecontrol.GatewaySuspender{Target: c.RunnerGatewayTarget, TokenFile: c.RunnerGatewayTokenFile, TLS: c.RunnerGatewayTLS},
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		return err
	}
	if err := mgr.AddHealthzCheck("ping", healthz.Ping); err != nil {
		return err
	}
	ready := healthz.Ping
	if c.EnvironmentService != nil {
		var identities reader.IdentitySource = reader.KubernetesIdentities{}
		if c.PolicyService != nil {
			cache, stop, err := identitycache.Start(ctx, *c.PolicyService, identitycache.StandardGrace)
			if err != nil {
				return fmt.Errorf("PolicyService Identity view: %w", err)
			}
			defer stop()
			identities = reader.CachedIdentities{View: cache}
		}
		server, err := serving.New(*c.EnvironmentService, c.Namespace, restConfig, scheme, identities)
		if err != nil {
			return err
		}
		if err := mgr.Add(server); err != nil {
			return err
		}
		ready = server.Check
	}
	if err := mgr.AddReadyzCheck("serving", ready); err != nil {
		return err
	}
	return mgr.Start(ctx)
}

// servesKubeVirt is whether the cluster serves the VirtualMachine API this controller watches.
func servesKubeVirt(config *rest.Config) (bool, error) {
	discoveryClient, err := discovery.NewDiscoveryClientForConfig(config)
	if err != nil {
		return false, err
	}
	_, err = discoveryClient.ServerResourcesForGroupVersion(kubevirtv1.GroupVersion.String())
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	return err == nil, err
}
