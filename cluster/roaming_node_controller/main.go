// The roaming node controller's entry point; see README.md.
package main

import (
	"log/slog"
	"os"
	"time"

	"github.com/go-logr/logr"
	coordinationv1 "k8s.io/api/coordination/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

// Must match the Deployment's probe port (cluster/cdk8s/roaming_node_controller.py).
const healthProbeAddress = ":8081"

func main() {
	ctrl.SetLogger(logr.FromSlogHandler(slog.NewJSONHandler(os.Stderr, nil)))
	if err := run(); err != nil {
		ctrl.Log.Error(err, "controller failed")
		os.Exit(1)
	}
}

func run() error {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: healthProbeAddress,
		// One replica with a Recreate strategy; every write is an optimistic-locked patch, so a
		// brief overlap would only conflict and retry.
		LeaderElection: false,
		Cache: cache.Options{
			ByObject: map[client.Object]cache.ByObject{
				&coordinationv1.Lease{}: {Namespaces: map[string]cache.Config{nodeLeaseNamespace: {}}},
			},
		},
	})
	if err != nil {
		return err
	}
	if err := (&Reconciler{Client: mgr.GetClient(), Now: time.Now}).SetupWithManager(mgr); err != nil {
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
