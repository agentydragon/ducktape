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
	"strconv"
	"strings"
	"time"

	"golang.org/x/sync/singleflight"
	"k8s.io/apimachinery/pkg/util/cache"
)

var (
	// ErrAddressForbidden means the host has an address the proxy may not dial.
	ErrAddressForbidden = errors.New("address forbidden")
	// ErrHostUnresolved means the host has no address to dial.
	ErrHostUnresolved = errors.New("host unresolved")
)

// DefaultTTL is how long a pin answers admissions without a new lookup.
const DefaultTTL = 30 * time.Second

// maxPins bounds the pins held; the least recently used goes first.
const maxPins = 10000

// Pin is the address a host was resolved to, valid for the dials made while it is fresh.
type Pin struct {
	Host    string
	Port    int
	Address netip.Addr
}

type pinKey struct {
	host string
	port int
}

// flight names the lookup that admissions of the same host and port, under the same `internal`,
// wait on together.
func (k pinKey) flight(internal bool) string {
	return net.JoinHostPort(k.host, strconv.Itoa(k.port)) + "/" + strconv.FormatBool(internal)
}

type clock func() time.Time

func (c clock) Now() time.Time { return c() }

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

	pins    *cache.LRUExpireCache
	flights singleflight.Group
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
	r := &Resolver{exempt: exempt, ttl: DefaultTTL, lookup: systemLookup, now: time.Now}
	for _, option := range options {
		option(r)
	}
	r.pins = cache.NewLRUExpireCacheWithClock(maxPins, clock(r.now))
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
	if pin, found, err := r.held(key, internal); found || err != nil {
		return pin, err
	}
	for {
		lookup := r.flights.DoChan(key.flight(internal), func() (any, error) {
			// An admission that arrived while the flight before it was finishing finds its pin.
			if pin, found, err := r.held(key, internal); found || err != nil {
				return pin, err
			}
			return r.resolveAndPin(ctx, key, internal)
		})
		select {
		case result := <-lookup:
			// The admission that led the lookup was cancelled; this one was not.
			if errors.Is(result.Err, context.Canceled) && ctx.Err() == nil {
				continue
			}
			if result.Err != nil {
				return Pin{}, result.Err
			}
			return result.Val.(Pin), nil
		case <-ctx.Done():
			return Pin{}, ctx.Err()
		}
	}
}

// held returns the pin held for the key, if it is still fresh and reachable for `internal`.
func (r *Resolver) held(key pinKey, internal bool) (Pin, bool, error) {
	value, found := r.pins.Get(key)
	if !found {
		return Pin{}, false, nil
	}
	pin := value.(Pin)
	// The cache is keyed by host and port, not by who admitted it, so a private address pinned for
	// a rule that declared its host internal must not be served to one that did not: the check
	// runs again against the address already held.
	if !Reachable(pin.Address, r.exempt, internal) {
		return Pin{}, true, fmt.Errorf("%w: %s is pinned to %s", ErrAddressForbidden, key.host, pin.Address)
	}
	return pin, true, nil
}

// resolveAndPin resolves the host once for every admission waiting on it, and holds the pin only
// when every address is reachable.
func (r *Resolver) resolveAndPin(ctx context.Context, key pinKey, internal bool) (Pin, error) {
	addresses, err := r.resolve(ctx, key.host)
	if err != nil {
		return Pin{}, err
	}
	for _, address := range addresses {
		if !Reachable(address, r.exempt, internal) {
			return Pin{}, fmt.Errorf("%w: %s resolves to %s", ErrAddressForbidden, key.host, address)
		}
	}
	chosen := addresses[0]
	for _, address := range addresses {
		if address.Is4() {
			chosen = address
			break
		}
	}
	pin := Pin{Host: key.host, Port: key.port, Address: chosen}
	r.pins.Add(key, pin, r.ttl)
	return pin, nil
}

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
