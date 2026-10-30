// The Sandbox Controller's entry point.
package main

import (
	"context"
	"log/slog"
	"os"
	"time"

	"github.com/go-logr/logr"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/sandbox_controller"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

func main() {
	ctrl.SetLogger(logr.FromSlogHandler(slog.NewJSONHandler(os.Stderr, nil)))
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
	quiescer := sandboxcontroller.GatewayQuiescer{Target: c.RunnerGatewayTarget, TokenFile: c.RunnerGatewayTokenFile}
	reconciler := &sandboxcontroller.Reconciler{
		Client: mgr.GetClient(), Objects: mgr.GetAPIReader(), KubeVirt: kubeVirt,
		Quiescer: quiescer, Leases: mgr.GetClient(), RunnerCertificateIssuer: c.RunnerCertificateIssuer, Now: time.Now,
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
