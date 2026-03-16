// WireguardLinkController watches network.LinkSpec resources and applies
// WireGuard interface configuration to the kernel.
//
// This is a minimal kubespand-only replacement for upstream Talos's monolithic
// LinkSpecController (which handles bonds, bridges, VLANs, physical links,
// and WireGuard in ~700 lines with Talos-specific udev dependencies).
//
// The WireGuard-specific logic here matches upstream exactly:
//   - Interface create/delete via netlink
//   - Diff-based config via networkadapter.WireguardSpec().Encode()
//   - Config apply via wgctrl
//
// Ref: talos/internal/app/machined/pkg/controllers/network/link_spec.go
package networkctrl

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"github.com/vishvananda/netlink"
	"go.uber.org/zap"
	"golang.zx2c4.com/wireguard/wgctrl"

	networkadapter "github.com/siderolabs/talos/internal/app/machined/pkg/adapters/network"
)

// WgClientFactory creates a wgctrl.Client for WireGuard configuration.
type WgClientFactory func() (*wgctrl.Client, error)

// WireguardLinkController watches LinkSpec resources and applies WireGuard
// interface configuration to the kernel.
type WireguardLinkController struct {
	WgClientFactory WgClientFactory

	wgClient    *wgctrl.Client
	lastApplied network.WireguardSpec
}

// Name implements controller.Controller.
func (ctrl *WireguardLinkController) Name() string {
	return "network.WireguardLinkController"
}

// Inputs implements controller.Controller.
func (ctrl *WireguardLinkController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*network.LinkSpec](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *WireguardLinkController) Outputs() []controller.Output {
	return nil
}

// Run implements controller.Controller.
func (ctrl *WireguardLinkController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	if ctrl.WgClientFactory == nil {
		ctrl.WgClientFactory = wgctrl.New
	}

	defer ctrl.cleanup(logger)

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		linkSpecs, err := safe.ReaderListAll[*network.LinkSpec](ctx, r)
		if err != nil {
			return fmt.Errorf("listing link specs: %w", err)
		}

		// Find the WireGuard LinkSpec.
		var wgSpec *network.LinkSpecSpec
		for ls := range linkSpecs.All() {
			spec := ls.TypedSpec()
			if spec.Kind == network.LinkKindWireguard {
				wgSpec = spec
				break
			}
		}

		if wgSpec == nil {
			// No WireGuard LinkSpec — ensure interface is removed.
			ctrl.deleteInterface(logger)
			ctrl.lastApplied = network.WireguardSpec{}
			continue
		}

		// Ensure the interface exists.
		if err := ctrl.ensureInterface(wgSpec, logger); err != nil {
			return fmt.Errorf("ensuring WireGuard interface: %w", err)
		}

		// Apply WireGuard config diff.
		if err := ctrl.applyWireguardConfig(wgSpec, logger); err != nil {
			return fmt.Errorf("applying WireGuard config: %w", err)
		}

		r.ResetRestartBackoff()
	}
}

// ensureInterface creates the WireGuard interface if it doesn't exist and
// sets MTU + UP state.
func (ctrl *WireguardLinkController) ensureInterface(spec *network.LinkSpecSpec, logger *zap.Logger) error {
	link, err := netlink.LinkByName(spec.Name)
	if err != nil {
		// Interface doesn't exist — create it.
		wgLink := &netlink.Wireguard{
			LinkAttrs: netlink.LinkAttrs{
				Name: spec.Name,
				MTU:  int(spec.MTU),
			},
		}
		if err := netlink.LinkAdd(wgLink); err != nil {
			return fmt.Errorf("creating %s: %w", spec.Name, err)
		}
		link, err = netlink.LinkByName(spec.Name)
		if err != nil {
			return fmt.Errorf("finding created %s: %w", spec.Name, err)
		}
		logger.Info("WireGuard interface created", zap.String("name", spec.Name))

		// Note: rp_filter and src_valid_mark sysctls are managed declaratively
		// by KernelParamSpecController via ConfigController-injected resources.
		// The per-interface rp_filter (conf/<iface>/rp_filter) is set to 0 by
		// KernelParamDefaultsController's default sysctl list.
	}

	// Set MTU if different.
	if link.Attrs().MTU != int(spec.MTU) {
		if err := netlink.LinkSetMTU(link, int(spec.MTU)); err != nil {
			return fmt.Errorf("setting MTU on %s: %w", spec.Name, err)
		}
	}

	// Bring up if requested.
	if spec.Up && link.Attrs().OperState != netlink.OperUp {
		if err := netlink.LinkSetUp(link); err != nil {
			return fmt.Errorf("bringing up %s: %w", spec.Name, err)
		}
	}

	return nil
}

// applyWireguardConfig uses the upstream adapter to compute a diff and apply it.
func (ctrl *WireguardLinkController) applyWireguardConfig(spec *network.LinkSpecSpec, logger *zap.Logger) error {
	if ctrl.wgClient == nil {
		client, err := ctrl.WgClientFactory()
		if err != nil {
			return fmt.Errorf("creating wgctrl client: %w", err)
		}
		ctrl.wgClient = client
	}

	desired := spec.Wireguard
	desired.Sort()

	existing := ctrl.lastApplied
	existing.Sort()

	// Use upstream adapter for diff-based encoding.
	wgConfig, err := networkadapter.WireguardSpec(&desired).Encode(&existing)
	if err != nil {
		return fmt.Errorf("encoding WireGuard config diff: %w", err)
	}

	if wgConfig == nil {
		// No changes needed.
		return nil
	}

	if err := ctrl.wgClient.ConfigureDevice(spec.Name, *wgConfig); err != nil {
		return fmt.Errorf("configuring %s: %w", spec.Name, err)
	}

	ctrl.lastApplied = desired
	logger.Debug("WireGuard config applied",
		zap.String("interface", spec.Name),
		zap.Int("peers", len(desired.Peers)),
	)

	return nil
}

// deleteInterface removes the WireGuard interface if it exists.
func (ctrl *WireguardLinkController) deleteInterface(logger *zap.Logger) {
	link, err := netlink.LinkByName("kubespan")
	if err != nil {
		return // already gone
	}
	if err := netlink.LinkDel(link); err != nil {
		logger.Warn("failed to delete WireGuard interface", zap.Error(err))
	} else {
		logger.Info("WireGuard interface deleted")
	}
}

// cleanup releases resources on shutdown.
func (ctrl *WireguardLinkController) cleanup(logger *zap.Logger) {
	ctrl.deleteInterface(logger)
	if ctrl.wgClient != nil {
		ctrl.wgClient.Close()
		ctrl.wgClient = nil
	}
}
