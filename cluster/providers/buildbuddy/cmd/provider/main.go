package main

import (
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/agentydragon/ducktape/cluster/providers/buildbuddy/apis/v1alpha1"
	provider "github.com/agentydragon/ducktape/cluster/providers/buildbuddy/internal/controller"
	"github.com/crossplane/crossplane-runtime/v2/pkg/controller"
	"github.com/crossplane/crossplane-runtime/v2/pkg/logging"
	"github.com/crossplane/crossplane-runtime/v2/pkg/ratelimiter"
	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	ctrl.SetLogger(logr.FromSlogHandler(log.Handler()))
	if err := run(); err != nil {
		log.Error("provider stopped", "error", err)
		os.Exit(1)
	}
}

func run() error {
	namespace := os.Getenv("WATCH_NAMESPACE")
	if namespace == "" {
		return fmt.Errorf("WATCH_NAMESPACE is required")
	}
	watchSecret := os.Getenv("WATCH_SECRET")
	if watchSecret == "" {
		return fmt.Errorf("WATCH_SECRET is required")
	}
	credentialSecret := os.Getenv("CREDENTIAL_SECRET")
	if credentialSecret == "" {
		return fmt.Errorf("CREDENTIAL_SECRET is required")
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := v1alpha1.SchemeBuilder.AddToScheme(scheme); err != nil {
		return err
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{namespace: {}},
			ByObject:          map[client.Object]cache.ByObject{&corev1.Secret{}: {Field: fields.OneTermEqualSelector("metadata.name", watchSecret)}},
		},
		Client:                 client.Options{Cache: &client.CacheOptions{DisableFor: []client.Object{&corev1.Secret{}}}},
		Metrics:                metricsserver.Options{BindAddress: ":8080"},
		HealthProbeBindAddress: ":8081",
	})
	if err != nil {
		return err
	}
	opts := controller.Options{Logger: logging.NewLogrLogger(ctrl.Log), GlobalRateLimiter: ratelimiter.NewGlobal(10), PollInterval: time.Minute, MaxConcurrentReconciles: 1}
	if err := provider.Setup(mgr, opts); err != nil {
		return err
	}
	if err := provider.SetupSecret(mgr, watchSecret, credentialSecret); err != nil {
		return err
	}
	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return err
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		return err
	}
	return mgr.Start(ctrl.SetupSignalHandler())
}
