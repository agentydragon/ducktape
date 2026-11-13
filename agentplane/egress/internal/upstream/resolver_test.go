package upstream

import (
	"context"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"
)

func addresses(texts ...string) []netip.Addr {
	parsed := make([]netip.Addr, len(texts))
	for i, text := range texts {
		parsed[i] = netip.MustParseAddr(text)
	}
	return parsed
}

// answers is a Lookup over fixed names that counts the lookups it served.
type answers struct {
	byHost map[string][]netip.Addr
	err    error
	calls  atomic.Int32
}

func (a *answers) lookup(_ context.Context, host string) ([]netip.Addr, error) {
	a.calls.Add(1)
	return a.byHost[strings.ToLower(host)], a.err
}

func TestLiteralAddressIsCheckedWithoutALookup(t *testing.T) {
	resolver := NewResolver(nil, WithLookup(func(context.Context, string) ([]netip.Addr, error) {
		t.Error("a literal was looked up")
		return nil, nil
	}))
	for host, want := range map[string]string{"140.82.121.4": "140.82.121.4", "[2606:50c0:8000::153]": "2606:50c0:8000::153"} {
		pin, err := resolver.Pin(context.Background(), host, 443, false)
		if err != nil || pin.Address != netip.MustParseAddr(want) {
			t.Errorf("Pin(%s) = %v, %v; want %s", host, pin.Address, err, want)
		}
	}
	if _, err := resolver.Pin(context.Background(), "[::ffff:10.0.0.1]", 443, false); !errors.Is(err, ErrAddressForbidden) {
		t.Errorf("a mapped private literal: err = %v, want ErrAddressForbidden", err)
	}
}

func TestNameResolvingIntoAForbiddenRangeIsRefused(t *testing.T) {
	names := &answers{byHost: map[string][]netip.Addr{"localhost": addresses("127.0.0.1", "::1")}}
	if _, err := NewResolver(nil, WithLookup(names.lookup)).Pin(context.Background(), "localhost", 443, false); !errors.Is(err, ErrAddressForbidden) {
		t.Errorf("err = %v, want ErrAddressForbidden", err)
	}
	pin, err := NewResolver(loopback, WithLookup(names.lookup)).Pin(context.Background(), "LocalHost", 443, false)
	if err != nil || pin.Address != netip.MustParseAddr("127.0.0.1") || pin.Host != "localhost" {
		t.Errorf("exempt Pin = %+v, %v; want 127.0.0.1 for localhost", pin, err)
	}
}

// One forbidden answer refuses the whole host, whichever place it has among the answers.
func TestAHostWithAnyForbiddenAnswerIsRefusedWhole(t *testing.T) {
	for _, order := range [][]string{{"140.82.121.4", "10.0.0.1"}, {"10.0.0.1", "140.82.121.4"}} {
		names := &answers{byHost: map[string][]netip.Addr{"mixed.test": addresses(order...)}}
		if _, err := NewResolver(nil, WithLookup(names.lookup)).Pin(context.Background(), "mixed.test", 443, false); !errors.Is(err, ErrAddressForbidden) {
			t.Errorf("answers %v: err = %v, want ErrAddressForbidden", order, err)
		}
	}
}

func TestAnIPv4AnswerIsPinnedBeforeAnIPv6One(t *testing.T) {
	for name, tc := range map[string]struct {
		answers []string
		want    string
	}{
		"v6 first": {[]string{"2606:50c0:8000::153", "140.82.121.4", "140.82.121.5"}, "140.82.121.4"},
		"v6 only":  {[]string{"2606:50c0:8000::153", "2606:50c0:8000::154"}, "2606:50c0:8000::153"},
		"repeated": {[]string{"140.82.121.5", "140.82.121.5", "140.82.121.4"}, "140.82.121.5"},
	} {
		names := &answers{byHost: map[string][]netip.Addr{"dual.test": addresses(tc.answers...)}}
		pin, err := NewResolver(nil, WithLookup(names.lookup)).Pin(context.Background(), "dual.test", 443, false)
		if err != nil || pin.Address != netip.MustParseAddr(tc.want) {
			t.Errorf("%s: Pin = %v, %v; want %s", name, pin.Address, err, tc.want)
		}
	}
}

// The pin cache is keyed by host and port, not by who admitted it. Without re-checking the address
// it holds, one rule declaring a host internal would lift the guard for every rule that names it
// while the pin stays fresh.
func TestAPrivateAddressPinnedForAnInternalRuleIsNotServedToAnother(t *testing.T) {
	resolver := NewResolver(nil)
	pin, err := resolver.Pin(context.Background(), "10.1.2.3", 443, true)
	if err != nil || pin.Address != netip.MustParseAddr("10.1.2.3") {
		t.Fatalf("internal Pin = %+v, %v", pin, err)
	}
	if _, err := resolver.Pin(context.Background(), "10.1.2.3", 443, false); !errors.Is(err, ErrAddressForbidden) {
		t.Errorf("err = %v, want ErrAddressForbidden", err)
	}
}

func TestAnUnresolvableNameIsRefused(t *testing.T) {
	cause := errors.New("no such host")
	for name, names := range map[string]*answers{
		"lookup error": {err: cause},
		"no addresses": {byHost: map[string][]netip.Addr{"nonexistent.invalid": nil}},
	} {
		_, err := NewResolver(nil, WithLookup(names.lookup)).Pin(context.Background(), "nonexistent.invalid", 443, false)
		if !errors.Is(err, ErrHostUnresolved) || errors.Is(err, ErrAddressForbidden) {
			t.Errorf("%s: err = %v, want ErrHostUnresolved", name, err)
		}
		if names.err != nil && !errors.Is(err, cause) {
			t.Errorf("%s: err = %v does not carry the lookup's error", name, err)
		}
	}
}

func TestACanceledLookupIsNotAnUnresolvedHost(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	resolver := NewResolver(nil, WithLookup(func(ctx context.Context, _ string) ([]netip.Addr, error) {
		cancel()
		<-ctx.Done()
		return nil, ctx.Err()
	}))
	if _, err := resolver.Pin(ctx, "slow.test", 443, false); !errors.Is(err, context.Canceled) || errors.Is(err, ErrHostUnresolved) {
		t.Errorf("err = %v, want context.Canceled", err)
	}
}

func TestAPinIsReusedWhileFreshAndNotAfter(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	names := &answers{byHost: map[string][]netip.Addr{"moving.test": addresses("140.82.121.4")}}
	resolver := NewResolver(nil, WithLookup(names.lookup), WithTTL(30*time.Second), WithClock(func() time.Time { return now }))
	pin := func(host string, port int) Pin {
		t.Helper()
		pinned, err := resolver.Pin(context.Background(), host, port, false)
		if err != nil {
			t.Fatal(err)
		}
		return pinned
	}

	first := pin("moving.test", 443)
	names.byHost["moving.test"] = addresses("140.82.121.9")
	now = now.Add(29 * time.Second)
	if again := pin("MOVING.test", 443); again != first || names.calls.Load() != 1 {
		t.Errorf("within the TTL: pin = %+v after %d lookups, want %+v after 1", again, names.calls.Load(), first)
	}
	if other := pin("moving.test", 80); other.Address != netip.MustParseAddr("140.82.121.9") || names.calls.Load() != 2 {
		t.Errorf("another port: pin = %+v after %d lookups, want a lookup of its own", other, names.calls.Load())
	}
	now = now.Add(2 * time.Second)
	if later := pin("moving.test", 443); later.Address != netip.MustParseAddr("140.82.121.9") || names.calls.Load() != 3 {
		t.Errorf("past the TTL: pin = %+v after %d lookups, want a fresh lookup", later, names.calls.Load())
	}
}

// Pins are served to concurrent admissions while others expire.
func TestConcurrentAdmissionsGetThePinsTheirHostsResolveTo(t *testing.T) {
	const hosts, workers, rounds = 20, 16, 50
	var tick atomic.Int64
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	resolver := NewResolver(
		nil,
		WithTTL(time.Second),
		WithClock(func() time.Time { return base.Add(time.Duration(tick.Add(1)) * 100 * time.Millisecond) }),
		WithLookup(func(_ context.Context, host string) ([]netip.Addr, error) {
			var number int
			if _, err := fmt.Sscanf(host, "host-%d.test", &number); err != nil {
				return nil, err
			}
			return []netip.Addr{netip.AddrFrom4([4]byte{140, 82, 121, byte(number)})}, nil
		}),
	)
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for round := range rounds {
				number := round % hosts
				pin, err := resolver.Pin(context.Background(), fmt.Sprintf("host-%d.test", number), 443, false)
				if want := netip.AddrFrom4([4]byte{140, 82, 121, byte(number)}); err != nil || pin.Address != want {
					t.Errorf("host-%d: pin = %v, %v; want %v", number, pin.Address, err, want)
				}
			}
		}()
	}
	wg.Wait()
}

// Admissions of one host that arrive together wait on a single lookup.
func TestConcurrentFirstAdmissionsOfOneHostShareALookup(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var lookups atomic.Int32
		release := make(chan struct{})
		resolver := NewResolver(nil, WithLookup(func(context.Context, string) ([]netip.Addr, error) {
			lookups.Add(1)
			<-release
			return addresses("140.82.121.4"), nil
		}))
		const admissions = 8
		pins := make(chan Pin, admissions)
		for range admissions {
			go func() {
				pin, err := resolver.Pin(t.Context(), "shared.test", 443, false)
				if err != nil {
					t.Error(err)
				}
				pins <- pin
			}()
		}
		synctest.Wait()

		close(release)

		first := <-pins
		for range admissions - 1 {
			if pin := <-pins; pin != first {
				t.Errorf("pin = %+v, want %+v", pin, first)
			}
		}
		if got := lookups.Load(); got != 1 {
			t.Errorf("%d lookups for %d admissions, want 1", got, admissions)
		}
	})
}

// An admission that leads the lookup and is then cancelled does not fail the ones waiting on it.
func TestAWaitingAdmissionSurvivesTheCancelledOneThatLedTheLookup(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var lookups atomic.Int32
		resolver := NewResolver(nil, WithLookup(func(ctx context.Context, _ string) ([]netip.Addr, error) {
			if lookups.Add(1) == 1 {
				<-ctx.Done()
				return nil, ctx.Err()
			}
			return addresses("140.82.121.4"), nil
		}))
		leader, cancel := context.WithCancel(t.Context())
		go func() { _, _ = resolver.Pin(leader, "shared.test", 443, false) }()
		synctest.Wait()
		waited := make(chan error, 1)
		go func() {
			_, err := resolver.Pin(t.Context(), "shared.test", 443, false)
			waited <- err
		}()
		synctest.Wait()

		cancel()

		if err := <-waited; err != nil {
			t.Errorf("waiting admission: %v", err)
		}
		if got := lookups.Load(); got != 2 {
			t.Errorf("%d lookups, want the cancelled one and the retry", got)
		}
	})
}
