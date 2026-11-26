package reader

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	policyclient "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned"
	policyinformers "github.com/agentydragon/ducktape/agentplane/policy/client/informers/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"k8s.io/apimachinery/pkg/types"
)

// IdentitySource is where an Index learns which incarnation of each Identity in the policy
// namespace is live: KubernetesIdentities or CachedIdentities.
type IdentitySource interface{ identitySource() }

// KubernetesIdentities watches the Identity resources with a shared informer.
type KubernetesIdentities struct{}

// CachedIdentities follows a PolicyService Identity view, which the caller runs. The Index holds
// what the view lists. While the view is stale it holds no Identity and the Identities kind is
// failing, so the Health is unavailable.
type CachedIdentities struct{ View *identitycache.Cache }

func (KubernetesIdentities) identitySource() {}
func (CachedIdentities) identitySource()     {}

func watchIdentities[R any](w *Watches, index *Index[R], policies policyclient.Interface, namespace string, source IdentitySource) error {
	switch source := source.(type) {
	case KubernetesIdentities:
		return Watch(w, Identities, policyinformers.NewTypedFilteredIdentityInformer(policies, namespace, 0, nil, nil), index.PutIdentity, index.DeleteIdentity)
	case CachedIdentities:
		if source.View == nil {
			return errors.New("reader: CachedIdentities needs a View")
		}
		followView(w, index, source.View)
		return nil
	default:
		return fmt.Errorf("reader: unknown IdentitySource %T", source)
	}
}

// followView keeps the index's Identities equal to the view's, and counts as synced once the view
// first answered.
func followView[R any](w *Watches, index *Index[R], view *identitycache.Cache) {
	var answered atomic.Bool
	w.synced = append(w.synced, answered.Load)
	w.feeds = append(w.feeds, func(ctx context.Context) {
		for {
			// Taken before List, so a change after List wakes the next pass.
			changed := view.Changed()
			if identities, err := view.List(); err != nil {
				index.replaceIdentities(nil)
				w.health.RecordFailure(Identities, err)
			} else {
				index.replaceIdentities(identities)
				w.health.RecordSuccess(Identities)
				answered.Store(true)
			}
			select {
			case <-ctx.Done():
				return
			case <-changed:
			}
		}
	})
}

// replaceIdentities makes identities the live Identities.
func (i *Index[R]) replaceIdentities(identities []auth.Identity) {
	live := make(map[string]types.UID, len(identities))
	for _, identity := range identities {
		live[identity.Name] = types.UID(identity.UID)
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	if maps.Equal(i.identities, live) {
		return
	}
	i.identities = live
	i.notifyChanged()
}
