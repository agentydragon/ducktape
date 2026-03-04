package network

import (
	"net/netip"
	"testing"

	"github.com/google/nftables/expr"
	"github.com/siderolabs/talos/pkg/machinery/nethelpers"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
)

func TestCompileMarkRule(t *testing.T) {
	verdict := nethelpers.VerdictAccept

	rule := network.NfTablesRule{
		MatchMark: &network.NfTablesMark{
			Mask:  0x00000060,
			Xor:   0x00000000,
			Value: 0x00000020,
		},
		Verdict: &verdict,
	}

	compiled, err := NfTablesRule(&rule).Compile()
	if err != nil {
		t.Fatalf("Compile() error: %v", err)
	}

	if len(compiled.Rules) != 1 {
		t.Fatalf("expected 1 rule, got %d", len(compiled.Rules))
	}

	// Should have: meta load mark, bitwise, cmp, verdict
	exprs := compiled.Rules[0]
	if len(exprs) < 4 {
		t.Fatalf("expected >= 4 expressions, got %d", len(exprs))
	}

	if _, ok := exprs[0].(*expr.Meta); !ok {
		t.Errorf("expected Meta expression at index 0, got %T", exprs[0])
	}
	if _, ok := exprs[1].(*expr.Bitwise); !ok {
		t.Errorf("expected Bitwise expression at index 1, got %T", exprs[1])
	}
	if _, ok := exprs[2].(*expr.Cmp); !ok {
		t.Errorf("expected Cmp expression at index 2, got %T", exprs[2])
	}
	if v, ok := exprs[3].(*expr.Verdict); !ok {
		t.Errorf("expected Verdict expression at index 3, got %T", exprs[3])
	} else if v.Kind != expr.VerdictAccept {
		t.Errorf("expected VerdictAccept, got %v", v.Kind)
	}
}

func TestCompileDestinationAddressRule(t *testing.T) {
	verdict := nethelpers.VerdictAccept

	rule := network.NfTablesRule{
		MatchDestinationAddress: &network.NfTablesAddressMatch{
			IncludeSubnets: []netip.Prefix{
				netip.MustParsePrefix("10.244.0.0/16"),
				netip.MustParsePrefix("10.96.0.0/12"),
			},
		},
		SetMark: &network.NfTablesMark{
			Mask: ^uint32(0x00000040),
			Xor:  0x00000040,
		},
		Verdict: &verdict,
	}

	compiled, err := NfTablesRule(&rule).Compile()
	if err != nil {
		t.Fatalf("Compile() error: %v", err)
	}

	// IPv4-only subnets → single rule with matchV4 prefix
	if len(compiled.Rules) != 1 {
		t.Fatalf("expected 1 rule (IPv4 only), got %d", len(compiled.Rules))
	}

	// Should have one IPv4 set
	if len(compiled.Sets) != 1 {
		t.Fatalf("expected 1 set, got %d", len(compiled.Sets))
	}

	if compiled.Sets[0].Kind != SetKindIPv4 {
		t.Errorf("expected SetKindIPv4, got %v", compiled.Sets[0].Kind)
	}
}

func TestCompileDualStackAddressRule(t *testing.T) {
	verdict := nethelpers.VerdictAccept

	rule := network.NfTablesRule{
		MatchDestinationAddress: &network.NfTablesAddressMatch{
			IncludeSubnets: []netip.Prefix{
				netip.MustParsePrefix("10.244.0.0/16"),
				netip.MustParsePrefix("fd00::/64"),
			},
		},
		Verdict: &verdict,
	}

	compiled, err := NfTablesRule(&rule).Compile()
	if err != nil {
		t.Fatalf("Compile() error: %v", err)
	}

	// Dual-stack → two rules (one IPv4, one IPv6)
	if len(compiled.Rules) != 2 {
		t.Fatalf("expected 2 rules (dual-stack), got %d", len(compiled.Rules))
	}

	// Should have two sets (one IPv4, one IPv6)
	if len(compiled.Sets) != 2 {
		t.Fatalf("expected 2 sets, got %d", len(compiled.Sets))
	}
}

func TestCompileMSSClamp(t *testing.T) {
	rule := network.NfTablesRule{
		MatchDestinationAddress: &network.NfTablesAddressMatch{
			IncludeSubnets: []netip.Prefix{
				netip.MustParsePrefix("10.0.0.0/8"),
			},
		},
		ClampMSS: &network.NfTablesClampMSS{
			MTU: 1420,
		},
	}

	compiled, err := NfTablesRule(&rule).Compile()
	if err != nil {
		t.Fatalf("Compile() error: %v", err)
	}

	// ClampMSS only generates rules for address families with matching prefixes.
	// IPv4-only address match → single IPv4 rule with MSS clamp.
	if len(compiled.Rules) != 1 {
		t.Fatalf("expected 1 rule (IPv4-only ClampMSS), got %d", len(compiled.Rules))
	}

	// The rule should contain Exthdr expressions (MSS clamp).
	for i, exprs := range compiled.Rules {
		hasExthdr := false
		for _, e := range exprs {
			if _, ok := e.(*expr.Exthdr); ok {
				hasExthdr = true
				break
			}
		}
		if !hasExthdr {
			t.Errorf("rule %d: MSS clamp rule should contain Exthdr expression", i)
		}
	}
}

func TestCompileEmptyRule(t *testing.T) {
	rule := network.NfTablesRule{}

	compiled, err := NfTablesRule(&rule).Compile()
	if err != nil {
		t.Fatalf("Compile() error: %v", err)
	}

	if len(compiled.Rules) != 0 {
		t.Errorf("expected 0 rules for empty input, got %d", len(compiled.Rules))
	}
}

func TestBuildIPSetAndSplit(t *testing.T) {
	include := []netip.Prefix{
		netip.MustParsePrefix("10.0.0.0/8"),
		netip.MustParsePrefix("fd00::/64"),
	}

	ipSet, err := BuildIPSet(include, nil)
	if err != nil {
		t.Fatalf("BuildIPSet() error: %v", err)
	}

	v4, v6 := SplitIPSet(ipSet)
	if len(v4) == 0 {
		t.Error("expected IPv4 ranges")
	}
	if len(v6) == 0 {
		t.Error("expected IPv6 ranges")
	}
}
