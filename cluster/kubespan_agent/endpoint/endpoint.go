// Package endpoint provides the EndpointController that harvests WireGuard
// endpoints from connected peers for re-announcement via discovery.
//
// Port of talos/internal/app/machined/pkg/controllers/kubespan/endpoint.go.
// Simplified: maps peer public key directly as resource ID (no Affiliate lookup).
package endpoint

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"
)

// Controller watches Config and PeerStatus resources and produces Endpoint
// resources for peers that are connected (State == Up) with a valid endpoint.
//
// This enables endpoint harvesting: when HarvestExtraEndpoints is enabled,
// discovered endpoint addresses of connected peers are recorded as Endpoint
// resources which can then be re-announced via the discovery service.
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

				// TODO: integrate harvested endpoints with discovery re-announcement (pbOtherEndpoints)
				if err := safe.WriterModify(ctx, r,
					kubespan.NewEndpoint(kubespan.NamespaceName, ps.Metadata().ID()),
					func(res *kubespan.Endpoint) error {
						res.TypedSpec().AffiliateID = ps.Metadata().ID()
						res.TypedSpec().Endpoint = spec.Endpoint
						return nil
					},
				); err != nil {
					return fmt.Errorf("writing endpoint for %s: %w", ps.Metadata().ID(), err)
				}
			}
		}

		if err := safe.CleanupOutputs[*kubespan.Endpoint](ctx, r); err != nil {
			return fmt.Errorf("cleaning up endpoints: %w", err)
		}

		r.ResetRestartBackoff()
	}
}
