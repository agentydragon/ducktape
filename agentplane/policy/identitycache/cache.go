// Package identitycache keeps a process-local view of Identities from PolicyService.WatchIdentities, so services
// resolve Identities without reading the Identity custom resources themselves.
//
// The view answers only while it is current: a snapshot has completed and the stream is connected, or it broke less
// than the configured grace ago. Otherwise lookups fail with ErrStale and callers refuse, as they would on an API
// server error. Revocation therefore follows watch propagation rather than a read per request.
package identitycache

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// ErrStale means the view cannot vouch for any Identity: it has no complete snapshot, or its stream broke longer
// than the grace ago.
var ErrStale = errors.New("identity view is not current")

type Cache struct {
	policy policypb.PolicyServiceClient
	grace  time.Duration
	now    func() time.Time

	mu         sync.RWMutex
	identities map[string]auth.Identity
	synced     bool
	// brokenAt is when the last stream that had completed a snapshot ended; zero while that stream is live.
	brokenAt time.Time
	// graceEnd signals Changed when the grace after brokenAt runs out and the view turns stale.
	graceEnd *time.Timer
	changed  chan struct{}
}

// New returns a view that is stale until Run completes its first snapshot. grace is how long the view keeps
// answering after its stream breaks, covering a PolicyService replica restart and the reconnect.
func New(policy policypb.PolicyServiceClient, grace time.Duration) (*Cache, error) {
	if policy == nil || grace < 0 {
		return nil, errors.New("identitycache: PolicyService client and a non-negative grace are required")
	}
	return &Cache{policy: policy, grace: grace, now: time.Now, identities: map[string]auth.Identity{}, changed: make(chan struct{})}, nil
}

// Changed closes after the next change to what Lookup and List answer, including the view turning stale when a
// broken stream's grace runs out; it may also close with no visible change. Obtain it before reading to avoid
// missing a change.
func (c *Cache) Changed() <-chan struct{} {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.changed
}

// notifyChangedLocked is called with mu held.
func (c *Cache) notifyChangedLocked() {
	close(c.changed)
	c.changed = make(chan struct{})
}

// Lookup returns the live, not-deleting Identity of a name.
func (c *Cache) Lookup(name string) (auth.Identity, bool, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if !c.currentLocked() {
		return auth.Identity{}, false, ErrStale
	}
	identity, ok := c.identities[name]
	return identity, ok, nil
}

// GetIdentityUID implements auth.IdentityUIDReader; a stale view returns ErrStale. A name the view lacks is read
// through PolicyService.GetIdentity, so an Identity created moments ago resolves before its watch event arrives.
// Hits never leave the process, and the view still decides revocation: a deleted Identity the view holds stays
// resolvable until its removal arrives, as before.
//
// TODO: the read-through exists because Identities live in the API server, whose writes reach WatchIdentities
// asynchronously. If Identity ownership moves into Agentplane's database, make CreateIdentity return only once its
// transaction has committed and readers' views reflect it, then delete the read-through and GetIdentity.
func (c *Cache) GetIdentityUID(ctx context.Context, name string) (string, bool, error) {
	identity, ok, err := c.Lookup(name)
	if err != nil || ok {
		return identity.UID, ok, err
	}
	response, err := c.policy.GetIdentity(ctx, &policypb.GetIdentityRequest{Name: name})
	if status.Code(err) == codes.NotFound {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("identitycache: read Identity %q through PolicyService: %w", name, err)
	}
	fresh := map[string]auth.Identity{}
	put(fresh, response.GetIdentity())
	identity, ok = fresh[name]
	return identity.UID, ok, nil
}

// List returns every live, not-deleting Identity, sorted by name.
func (c *Cache) List() ([]auth.Identity, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if !c.currentLocked() {
		return nil, ErrStale
	}
	return slices.SortedFunc(maps.Values(c.identities), func(a, b auth.Identity) int { return cmp.Compare(a.Name, b.Name) }), nil
}

func (c *Cache) currentLocked() bool {
	return c.synced && (c.brokenAt.IsZero() || c.now().Sub(c.brokenAt) < c.grace)
}

// Run follows WatchIdentities, reconnecting with backoff, until ctx ends.
func (c *Cache) Run(ctx context.Context) error {
	backoff := func() retry.Backoff {
		return retry.WithFullJitter(retry.WithCappedDuration(5*time.Second, retry.NewExponential(100*time.Millisecond)))
	}
	waits := backoff()
	for {
		synced, err := c.follow(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		slog.WarnContext(ctx, "Identity watch ended; reconnecting", "error", err)
		if synced {
			waits = backoff()
		}
		wait, _ := waits.Next()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
	}
}

// follow runs one stream and reports whether it completed a snapshot before ending.
func (c *Cache) follow(ctx context.Context) (bool, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.policy.WatchIdentities(ctx, &policypb.WatchIdentitiesRequest{})
	if err != nil {
		return false, err
	}
	pending := map[string]auth.Identity{}
	synced := false
	defer func() {
		if synced {
			c.mu.Lock()
			c.brokenAt = c.now()
			c.graceEnd = time.AfterFunc(c.grace, func() {
				c.mu.Lock()
				defer c.mu.Unlock()
				c.notifyChangedLocked()
			})
			c.mu.Unlock()
		}
	}()
	for {
		frame, err := stream.Recv()
		if err != nil {
			return synced, err
		}
		switch frame := frame.GetFrame().(type) {
		case *policypb.WatchIdentitiesResponse_Snapshot:
			for _, identity := range frame.Snapshot.GetIdentities() {
				put(pending, identity)
			}
		case *policypb.WatchIdentitiesResponse_SnapshotComplete:
			c.mu.Lock()
			c.identities, c.synced, c.brokenAt = pending, true, time.Time{}
			if c.graceEnd != nil {
				c.graceEnd.Stop()
			}
			c.notifyChangedLocked()
			c.mu.Unlock()
			synced = true
		case *policypb.WatchIdentitiesResponse_Upsert:
			c.apply(synced, pending, func(view map[string]auth.Identity) { put(view, frame.Upsert) })
		case *policypb.WatchIdentitiesResponse_Removed:
			c.apply(synced, pending, func(view map[string]auth.Identity) { remove(view, frame.Removed) })
		default:
			return synced, errors.New("identitycache: unknown WatchIdentities frame")
		}
	}
}

// apply edits the live view once this stream's snapshot completed, else the snapshot being assembled.
func (c *Cache) apply(synced bool, pending map[string]auth.Identity, edit func(map[string]auth.Identity)) {
	if !synced {
		edit(pending)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	defer c.notifyChangedLocked()
	edit(c.identities)
}

func put(view map[string]auth.Identity, identity *policypb.Identity) {
	meta := identity.GetIdentity()
	if meta.GetUid() == "" || identity.GetDeletionTimestamp() != nil {
		delete(view, meta.GetName())
		return
	}
	view[meta.GetName()] = auth.Identity{Name: meta.GetName(), UID: meta.GetUid()}
}

func remove(view map[string]auth.Identity, removed *policypb.PolicyResourceIdentity) {
	// A removal names one incarnation; a successor of the same name may already be in the view.
	if current, ok := view[removed.GetName()]; ok && current.UID == removed.GetUid() {
		delete(view, removed.GetName())
	}
}
