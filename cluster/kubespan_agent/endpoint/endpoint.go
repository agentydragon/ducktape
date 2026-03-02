// Package endpoint provides the EndpointController that harvests WireGuard
// endpoints from connected peers for re-announcement via discovery.
//
// Port of talos/internal/app/machined/pkg/controllers/kubespan/endpoint.go.
package endpoint

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"
)

// Controller watches Config, PeerStatus, and Affiliate resources, and produces
// Endpoint resources for peers that are connected (State == Up) with a valid
// endpoint. Uses the Affiliate mapping to set the correct AffiliateID on
// harvested endpoints for re-announcement via the discovery service.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/endpoint.go
type Controller struct{}

// Name implements controller.Controller.
func (ctrl *Controller) Name() string {
	return "kubespan.EndpointController"
}

// Inputs implements controller.Controller.
func (ctrl *Controller) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
		safe.Input[*kubespan.PeerStatus](controller.InputWeak),
		safe.Input[*cluster.Affiliate](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *Controller) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: kubespan.EndpointType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/endpoint.go (Run)
func (ctrl *Controller) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
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

		cfgSpec := cfg.TypedSpec()

		r.StartTrackingOutputs()

		if cfgSpec.HarvestExtraEndpoints {
			// Build publicKey → affiliateID map from Affiliate resources.
			pubKeyToAffID := ctrl.buildAffiliateMap(ctx, r)

			peerStatuses, listErr := safe.ReaderListAll[*kubespan.PeerStatus](ctx, r)
			if listErr != nil {
				return fmt.Errorf("listing peer statuses: %w", listErr)
			}

			for ps := range peerStatuses.All() {
				spec := ps.TypedSpec()
				if spec.State != kubespan.PeerStateUp {
					continue
				}
				if !spec.Endpoint.IsValid() {
					continue
				}

				pubKey := ps.Metadata().ID()
				affID, ok := pubKeyToAffID[pubKey]
				if !ok {
					affID = pubKey // fallback to public key if no affiliate mapping
				}

				if err := safe.WriterModify(ctx, r,
					kubespan.NewEndpoint(kubespan.NamespaceName, resource.ID(affID)),
					func(res *kubespan.Endpoint) error {
						res.TypedSpec().AffiliateID = affID
						res.TypedSpec().Endpoint = spec.Endpoint
						return nil
					},
				); err != nil {
					return fmt.Errorf("writing endpoint for %s: %w", affID, err)
				}
			}
		}

		if err := safe.CleanupOutputs[*kubespan.Endpoint](ctx, r); err != nil {
			return fmt.Errorf("cleaning up endpoints: %w", err)
		}

		r.ResetRestartBackoff()
	}
}

// buildAffiliateMap reads cluster.Affiliate resources and returns a map
// from KubeSpan public key to affiliate resource ID (NodeID).
func (ctrl *Controller) buildAffiliateMap(ctx context.Context, r controller.Runtime) map[string]string {
	affiliates, err := safe.ReaderListAll[*cluster.Affiliate](ctx, r)
	if err != nil {
		return nil
	}

	result := make(map[string]string)
	for aff := range affiliates.All() {
		ks := aff.TypedSpec().KubeSpan
		if ks.PublicKey != "" {
			result[ks.PublicKey] = aff.Metadata().ID()
		}
	}
	return result
}
