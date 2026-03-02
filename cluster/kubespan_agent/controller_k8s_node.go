package main

import (
	"context"
	"fmt"
	"net/netip"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/k8snet"
)

// KubernetesNodeController watches a K8s Node object via client-go informer
// and produces a KubernetesNetworks COSI resource with PodCIDRs + ServiceCIDRs.
//
// Analogous to Talos's K8sNodeStatusController which feeds into LocalAffiliateController.
type KubernetesNodeController struct {
	factory  informers.SharedInformerFactory
	informer cache.SharedIndexInformer
}

// Name implements controller.Controller.
func (ctrl *KubernetesNodeController) Name() string {
	return "kubespan.KubernetesNodeController"
}

// Inputs implements controller.Controller.
func (ctrl *KubernetesNodeController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *KubernetesNodeController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: k8snet.Type,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *KubernetesNodeController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		cfg, err := safe.ReaderGetByID[*kubespan.Config](ctx, r, kubespan.ConfigID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting config: %w", err)
		}

		if !cfg.TypedSpec().AdvertiseKubernetesNetworks {
			continue
		}

		// Lazy-init the K8s informer on first reconcile when config is available.
		if ctrl.informer == nil {
			if initErr := ctrl.initInformer(ctx, r, logger); initErr != nil {
				logger.Warn("failed to initialize K8s informer, will retry", zap.Error(initErr))
				continue
			}
		}

		// Read node from informer cache and extract PodCIDRs.
		prefixes := ctrl.getPodCIDRs(logger)

		// Merge static ServiceCIDRs from config.
		prefixes = append(prefixes, agentCfg.ServiceCIDRs...)

		// Write KubernetesNetworks resource.
		if err := safe.WriterModify(ctx, r, k8snet.New(), func(res *k8snet.KubernetesNetworks) error {
			res.TypedSpec().Prefixes = prefixes
			return nil
		}); err != nil {
			return fmt.Errorf("writing kubernetes networks: %w", err)
		}

		logger.Debug("kubernetes networks reconciled", zap.Int("prefixes", len(prefixes)))
		r.ResetRestartBackoff()
	}
}

// initInformer creates the K8s clientset and starts the node informer.
func (ctrl *KubernetesNodeController) initInformer(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	var config *rest.Config
	var err error

	if agentCfg.KubeconfigPath != "" {
		config, err = clientcmd.BuildConfigFromFlags("", agentCfg.KubeconfigPath)
		if err != nil {
			return fmt.Errorf("building kubeconfig from %s: %w", agentCfg.KubeconfigPath, err)
		}
	} else {
		config, err = rest.InClusterConfig()
		if err != nil {
			return fmt.Errorf("building in-cluster config: %w", err)
		}
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return fmt.Errorf("creating kubernetes client: %w", err)
	}

	// Create informer factory filtered to the local node only.
	ctrl.factory = informers.NewSharedInformerFactoryWithOptions(
		clientset, 0,
		informers.WithTweakListOptions(func(opts *metav1.ListOptions) {
			opts.FieldSelector = fields.OneTermEqualSelector("metadata.name", agentCfg.NodeName).String()
		}),
	)

	ctrl.informer = ctrl.factory.Core().V1().Nodes().Informer()

	// Bridge K8s informer events to COSI reconcile loop (same pattern as
	// DiscoveryController's dm.NotifyCh() → r.QueueReconcile()).
	ctrl.informer.AddEventHandler(cache.ResourceEventHandlerFuncs{ //nolint:errcheck
		AddFunc:    func(_ interface{}) { r.QueueReconcile() },
		UpdateFunc: func(_, _ interface{}) { r.QueueReconcile() },
		DeleteFunc: func(_ interface{}) { r.QueueReconcile() },
	})

	ctrl.factory.Start(ctx.Done())

	// Wait for initial cache sync.
	if !cache.WaitForCacheSync(ctx.Done(), ctrl.informer.HasSynced) {
		return fmt.Errorf("timed out waiting for K8s node informer to sync")
	}

	logger.Info("K8s node informer started", zap.String("node", agentCfg.NodeName))
	return nil
}

// getPodCIDRs reads PodCIDRs from the informer cache for the local node.
func (ctrl *KubernetesNodeController) getPodCIDRs(logger *zap.Logger) []netip.Prefix {
	items := ctrl.informer.GetStore().List()
	if len(items) == 0 {
		return nil
	}

	node, ok := items[0].(*corev1.Node)
	if !ok {
		return nil
	}

	var prefixes []netip.Prefix
	for _, cidr := range node.Spec.PodCIDRs {
		p, err := netip.ParsePrefix(cidr)
		if err != nil {
			logger.Warn("failed to parse PodCIDR", zap.String("cidr", cidr), zap.Error(err))
			continue
		}
		prefixes = append(prefixes, p)
	}

	return prefixes
}
