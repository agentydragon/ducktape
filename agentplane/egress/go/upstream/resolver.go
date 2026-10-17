// Package upstream decides the address a proxied request may be dialed at: the admitted host,
// resolved by the proxy and pinned to the dial.
//
// An egress policy names hosts, but a connection is made to an address, and nothing about a name
// says where it points: an allowed name can resolve into the cluster (a Service, a Pod, a node),
// and it can resolve differently on the second lookup (DNS rebinding). So the proxy resolves the
// host itself, refuses every address that is not globally reachable, and dials exactly the
// address it checked.
package upstream

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"strings"
	"sync"
	"time"
)

var (
	// ErrAddressForbidden means the host has an address the proxy may not dial.
	ErrAddressForbidden = errors.New("address forbidden")
	// ErrHostUnresolved means the host has no address to dial.
	ErrHostUnresolved = errors.New("host unresolved")
)

// DefaultTTL is how long a pin answers admissions without a new lookup.
const DefaultTTL = 30 * time.Second

// Pin is the address a host was resolved to, valid for the dials made while it is fresh.
type Pin struct {
	Host       string
	Port       int
	Address    netip.Addr
	ResolvedAt time.Time
}

type pinKey struct {
	host string
	port int
}

// Lookup returns every address a name resolves to.
type Lookup func(ctx context.Context, host string) ([]netip.Addr, error)

func systemLookup(ctx context.Context, host string) ([]netip.Addr, error) {
	return net.DefaultResolver.LookupNetIP(ctx, "ip", host)
}

// Resolver resolves and checks a host on admission. A pin lives `ttl`: admissions within it reuse
// the address without a lookup, which is what makes the dial go where the check looked.
type Resolver struct {
	exempt []netip.Prefix
	ttl    time.Duration
	lookup Lookup
	now    func() time.Time

	mu   sync.Mutex
	pins map[pinKey]Pin
}

type Option func(*Resolver)

// WithTTL replaces DefaultTTL.
func WithTTL(ttl time.Duration) Option { return func(r *Resolver) { r.ttl = ttl } }

// WithLookup replaces the system resolver.
func WithLookup(lookup Lookup) Option { return func(r *Resolver) { r.lookup = lookup } }

// WithClock replaces the wall clock.
func WithClock(now func() time.Time) Option { return func(r *Resolver) { r.now = now } }

// NewResolver admits, besides globally reachable addresses, the ones inside `exempt`: networks an
// admitted host may resolve into although they are not globally reachable.
func NewResolver(exempt []netip.Prefix, options ...Option) *Resolver {
	r := &Resolver{
		exempt: exempt, ttl: DefaultTTL, lookup: systemLookup, now: time.Now, pins: map[pinKey]Pin{},
	}
	for _, option := range options {
		option(r)
	}
	return r
}

// Pin returns the fresh pin for the host, resolving it when there is none.
//
// A host with any address that is not reachable is refused whole, rather than served from its
// reachable addresses: a name that points into the cluster at all is not one an egress policy
// meant to admit -- unless the rule that admitted it says so, which is what `internal` carries.
// Among the reachable ones an IPv4 address is pinned before an IPv6 one: the Pod network the
// proxy dials from has no IPv6 route.
func (r *Resolver) Pin(ctx context.Context, host string, port int, internal bool) (Pin, error) {
	key := pinKey{host: strings.ToLower(host), port: port}
	if pin, ok := r.fresh(key); ok {
		// The cache is keyed by host and port, not by who admitted it, so a private address
		// pinned for a rule that declared its host internal must not be served to one that did
		// not: the check runs again against the address already held.
		if !Reachable(pin.Address, r.exempt, internal) {
			return Pin{}, fmt.Errorf("%w: %s is pinned to %s", ErrAddressForbidden, host, pin.Address)
		}
		return pin, nil
	}
	addresses, err := r.resolve(ctx, host)
	if err != nil {
		return Pin{}, err
	}
	for _, address := range addresses {
		if !Reachable(address, r.exempt, internal) {
			return Pin{}, fmt.Errorf("%w: %s resolves to %s", ErrAddressForbidden, host, address)
		}
	}
	chosen := addresses[0]
	for _, address := range addresses {
		if address.Is4() {
			chosen = address
			break
		}
	}
	now := r.now()
	pin := Pin{Host: key.host, Port: port, Address: chosen, ResolvedAt: now}
	r.mu.Lock()
	defer r.mu.Unlock()
	for kept, held := range r.pins {
		if !r.alive(held, now) {
			delete(r.pins, kept)
		}
	}
	r.pins[key] = pin
	return pin, nil
}

func (r *Resolver) fresh(key pinKey) (Pin, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	pin, ok := r.pins[key]
	return pin, ok && r.alive(pin, r.now())
}

func (r *Resolver) alive(pin Pin, now time.Time) bool { return now.Before(pin.ResolvedAt.Add(r.ttl)) }

// resolve returns the host's distinct addresses in answer order. A literal is checked, never
// looked up.
func (r *Resolver) resolve(ctx context.Context, host string) ([]netip.Addr, error) {
	if literal, err := netip.ParseAddr(strings.Trim(host, "[]")); err == nil {
		return []netip.Addr{literal}, nil
	}
	found, err := r.lookup(ctx, host)
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil, ctxErr
		}
		return nil, fmt.Errorf("%w: %s: %w", ErrHostUnresolved, host, err)
	}
	seen := make(map[netip.Addr]struct{}, len(found))
	addresses := make([]netip.Addr, 0, len(found))
	for _, address := range found {
		if _, duplicate := seen[address]; !duplicate {
			seen[address] = struct{}{}
			addresses = append(addresses, address)
		}
	}
	if len(addresses) == 0 {
		return nil, fmt.Errorf("%w: %s: no addresses", ErrHostUnresolved, host)
	}
	return addresses, nil
}
