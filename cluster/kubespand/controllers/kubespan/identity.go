// IdentityController watches Config, AgentConfig, and HardwareAddr, and produces
// the node's KubeSpan Identity.
//
// It loads or creates a WireGuard keypair and derives the KubeSpan ULA address
// from the cluster ID and the machine's MAC address (read from the upstream
// HardwareAddrController's network.HardwareAddr resource).
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/identity.go
package kubespanctrl

import (
	"context"
	"fmt"
	"net"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
	"github.com/agentydragon/ducktape/cluster/kubespand/identity"
	kubespanadapter "github.com/siderolabs/talos/internal/app/machined/pkg/adapters/kubespan"
)

// IdentityController watches Config, AgentConfig, and HardwareAddr, producing Identity.
type IdentityController struct {
	cachedID *kubespan.IdentitySpec
}

// Name implements controller.Controller.
func (ctrl *IdentityController) Name() string {
	return "kubespan.IdentityController"
}

// Inputs implements controller.Controller.
func (ctrl *IdentityController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
		safe.Input[*agentconfig.Resource](controller.InputWeak),
		safe.Input[*network.HardwareAddr](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *IdentityController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: kubespan.IdentityType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *IdentityController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
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

		acfg, err := safe.ReaderGetByID[*agentconfig.Resource](ctx, r, agentconfig.ResourceID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting agent config: %w", err)
		}

		hwAddr, err := safe.ReaderGetByID[*network.HardwareAddr](ctx, r, network.FirstHardwareAddr)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting hardware addr: %w", err)
		}

		cfgSpec := cfg.TypedSpec()
		agentSpec := acfg.TypedSpec()

		if ctrl.cachedID == nil {
			mac := net.HardwareAddr(hwAddr.TypedSpec().HardwareAddr)

			id, err := identity.LoadOrCreate(agentSpec.IdentityFile, cfgSpec.ClusterID)
			if err != nil {
				return fmt.Errorf("loading identity: %w", err)
			}

			if err := kubespanadapter.IdentitySpec(id).UpdateAddress(cfgSpec.ClusterID, mac); err != nil {
				return fmt.Errorf("computing address: %w", err)
			}

			ctrl.cachedID = id

			logger.Info("identity ready",
				zap.String("public_key", id.PublicKey),
				zap.Stringer("subnet", id.Subnet),
				zap.Stringer("address", id.Address),
			)
		}

		if err := safe.WriterModify(ctx, r,
			kubespan.NewIdentity(kubespan.NamespaceName, kubespan.LocalIdentity),
			func(res *kubespan.Identity) error {
				*res.TypedSpec() = *ctrl.cachedID
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing identity: %w", err)
		}

		r.ResetRestartBackoff()
	}
}
