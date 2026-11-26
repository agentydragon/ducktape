// Package wake executes Dispatcher automatic wake with a shared reservation budget.
package wake

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Options grant automatic wake only; explicit-resume roles and policy belong to Environments.
type Options struct {
	Namespace string
	Objects   client.Client
	// Identities resolves the Identities Sandboxes in Namespace name.
	Identities         auth.IdentityUIDReader
	WakeBudget         WakeBudget
	WakeServiceCallers []string
}

type Manager struct {
	namespace string
	policy    *lifecycle.Policy
	budget    WakeBudget
}

func NewManager(options Options) (*Manager, error) {
	if options.WakeBudget == nil {
		return nil, errors.New("Sandbox namespace, uncached Kubernetes client, and shared wake budget are required")
	}
	policy, err := lifecycle.NewPolicy(lifecycle.Options{
		Namespace: options.Namespace, Objects: options.Objects, Identities: options.Identities,
		WakeServiceCallers: options.WakeServiceCallers,
	})
	if err != nil {
		return nil, err
	}
	return &Manager{namespace: options.Namespace, policy: policy, budget: options.WakeBudget}, nil
}

// RequestWake evaluates an admitted caller, reserves automatic budget and applies a guarded write.
func (s *Manager) RequestWake(ctx context.Context, target *lifecycle.Target, caller lifecycle.WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	return s.policy.ApplyAutomaticWake(ctx, target, caller, func(ctx context.Context, nextGeneration int64) (time.Duration, error) {
		return s.budget.ReserveWakeAttempt(ctx, s.budgetKey(caller), target.Uid, nextGeneration)
	})
}

func (s *Manager) budgetKey(caller lifecycle.WakeCaller) WakeBudgetKey {
	switch caller := caller.(type) {
	case lifecycle.IdentityWakeCaller:
		return WakeBudgetKey{Namespace: s.namespace, Name: caller.Identity.GetName(), UID: caller.Identity.GetUid()}
	case lifecycle.ServiceWakeCaller:
		return WakeBudgetKey{ServiceSPIFFEID: caller.SPIFFEID}
	default:
		panic(fmt.Sprintf("unknown wake caller %T", caller))
	}
}
