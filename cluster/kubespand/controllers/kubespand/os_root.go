// OSRootController produces a secrets.OSRoot COSI resource from the agent config.
//
// This is the kubespand equivalent of Talos's RootOSController, which reads
// machine.ca from MachineConfig. kubespand reads the CA certificate and token
// from its YAML config instead.
//
// The OSRoot resource is consumed by the upstream APIController to generate
// API certificates via the trustd CSR flow (worker mode — no CA private key).
//
// Ref: internal/app/machined/pkg/controllers/secrets/root.go
package kubespandctrl

import (
	"context"
	"fmt"
	"net/netip"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/crypto/x509"
	"github.com/siderolabs/talos/pkg/machinery/resources/secrets"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
)

// OSRootController produces secrets.OSRoot from agent config (CA cert + token).
type OSRootController struct{}

// Name implements controller.Controller.
func (ctrl *OSRootController) Name() string {
	return "kubespand.OSRootController"
}

// Inputs implements controller.Controller.
func (ctrl *OSRootController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*agentconfig.Resource](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *OSRootController) Outputs() []controller.Output {
	return []controller.Output{
		{Type: secrets.OSRootType, Kind: controller.OutputExclusive},
	}
}

// Run implements controller.Controller.
func (ctrl *OSRootController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		acfg, err := safe.ReaderGetByID[*agentconfig.Resource](ctx, r, agentconfig.ResourceID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}

			return fmt.Errorf("getting agent config: %w", err)
		}

		spec := acfg.TypedSpec()

		if spec.CACrt == "" || spec.Token == "" {
			continue
		}

		caCert := &x509.PEMEncodedCertificate{Crt: []byte(spec.CACrt)}

		if err := safe.WriterModify(ctx, r,
			secrets.NewOSRoot(secrets.OSRootID),
			func(res *secrets.OSRoot) error {
				rootSpec := res.TypedSpec()
				// Worker mode: CA cert only, no private key.
				rootSpec.IssuingCA = &x509.PEMEncodedCertificateAndKey{
					Crt: []byte(spec.CACrt),
				}
				rootSpec.AcceptedCAs = []*x509.PEMEncodedCertificate{caCert}
				rootSpec.Token = spec.Token
				// Parse CertSANs into IPs and DNS names (matches Talos machine.certSANs).
				rootSpec.CertSANIPs = nil
				rootSpec.CertSANDNSNames = nil
				for _, san := range spec.CertSANs {
					if addr, err := netip.ParseAddr(san); err == nil {
						rootSpec.CertSANIPs = append(rootSpec.CertSANIPs, addr)
					} else {
						rootSpec.CertSANDNSNames = append(rootSpec.CertSANDNSNames, san)
					}
				}

				return nil
			},
		); err != nil {
			return fmt.Errorf("writing OS root secrets: %w", err)
		}

		logger.Info("OS root secrets produced from config")
		r.ResetRestartBackoff()
	}
}
