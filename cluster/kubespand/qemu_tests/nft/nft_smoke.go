package main

import (
	"errors"
	"fmt"
	"math/rand"
	"os"
	"syscall"
	"time"

	"github.com/google/nftables"
	"github.com/google/nftables/expr"
	"golang.org/x/sys/unix"
)

var ebusyRetry bool

// runNftSmokeLevel runs a single nft-smoke level. Returns 0 on success, 1 on
// failure, 2 on unknown level.
func runNftSmokeLevel(level int) int {
	fmt.Printf("nft-smoke level %d\n", level)

	if err := logExistingState(); err != nil {
		return 1
	}

	var err error
	switch level {
	case 1:
		err = smokeLevel1()
	case 2:
		err = smokeLevel2()
	case 3:
		err = smokeLevel3()
	case 4:
		err = smokeLevel4()
	case 5:
		err = smokeLevel5()
	case 6:
		err = smokeLevel6()
	// Isolation sub-levels: bisect which expression in level 3 causes EBUSY.
	case 10:
		err = smokeMarkRead()
	case 11:
		err = smokeMarkReadBitwise()
	case 12:
		err = smokeMarkWrite()
	case 13:
		err = smokeMarkReadWrite()
	case 14:
		err = smokeSetLookupOnly()
	case 15:
		err = smokeLookupThenMark()
	case 16:
		err = smokeTwoRulesSetAndMark()
	case 17:
		err = smokeSetLookupMarkInOneRule()
	case 18:
		err = smokeFiveRulesOneChain()
	case 19:
		err = smokeTwoChainsTwoRules()
	case 20:
		err = smokeLevel3MinusOneRule()
	case 21:
		err = smokeFiveSimpleRules()
	case 22:
		err = smokeTwoLookupsSameSet()
	default:
		fmt.Fprintf(os.Stderr, "unknown level %d\n", level)
		return 2
	}

	if err != nil {
		fmt.Fprintf(os.Stderr, "FAIL: %v\n", err)
		return 1
	}

	nftCleanup()
	fmt.Printf("nft-smoke level %d: PASS\n", level)
	return 0
}

const smokeTableName = "testprobe_smoke"

func logExistingState() error {
	conn, err := nftables.New()
	if err != nil {
		fmt.Fprintf(os.Stderr, "nftables.New: %v\n", err)
		return err
	}
	tables, _ := conn.ListTables()
	fmt.Printf("existing tables: %d\n", len(tables))
	for _, t := range tables {
		fmt.Printf("  table %s family=%d\n", t.Name, t.Family)
	}
	chains, _ := conn.ListChains()
	fmt.Printf("existing chains: %d\n", len(chains))
	for _, c := range chains {
		tbl := "<nil>"
		if c.Table != nil {
			tbl = c.Table.Name
		}
		fmt.Printf("  chain %s table=%s\n", c.Name, tbl)
	}
	return nil
}

func nftCleanup() {
	conn, _ := nftables.New()
	if conn != nil {
		conn.DelTable(&nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName})
		_ = conn.Flush()
	}
}

// flushRetry wraps conn.Flush() with EBUSY retry when ebusyRetry is set.
// In QEMU TCG emulation, the kernel runs slowly enough that the nf_tables
// commit mutex is often still held by RCU cleanup from the previous Flush.
func flushRetry(conn *nftables.Conn) error {
	if !ebusyRetry {
		return conn.Flush()
	}
	const maxRetries = 10
	for i := 0; i < maxRetries; i++ {
		err := conn.Flush()
		if err == nil {
			return nil
		}
		if !errors.Is(err, syscall.EBUSY) {
			return err
		}
		base := time.Duration(50<<i) * time.Millisecond
		jitter := time.Duration(rand.Intn(int(base)))
		delay := base + jitter
		if delay > 5*time.Second {
			delay = 5 * time.Second
		}
		fmt.Printf("  EBUSY retry %d/%d (waiting %v)\n", i+1, maxRetries, delay)
		time.Sleep(delay)
	}
	return conn.Flush()
}

// smokeLevel1: table + chains with hooks in separate batches. Baseline.
func smokeLevel1() error {
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := conn.AddTable(&nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (AddTable): %w", err)
	}
	fmt.Println("  AddTable OK")

	conn2, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	policy := nftables.ChainPolicyAccept
	conn2.AddChain(&nftables.Chain{
		Name: "test_prerouting", Table: table,
		Type: nftables.ChainTypeFilter, Hooknum: nftables.ChainHookPrerouting,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	conn2.AddChain(&nftables.Chain{
		Name: "test_output", Table: table,
		Type: nftables.ChainTypeRoute, Hooknum: nftables.ChainHookOutput,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	if err := flushRetry(conn2); err != nil {
		return fmt.Errorf("Flush (AddChains): %w", err)
	}
	fmt.Println("  AddChains OK")
	return nil
}

// smokeLevel2: table + chains + anonymous interval set + lookup rule.
func smokeLevel2() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	conn.AddRule(&nftables.Rule{
		Table: table, Chain: chain,
		Exprs: v4LookupAcceptExprs(set),
	})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (set+rule): %w", err)
	}
	fmt.Println("  AddSet + AddRule (anonymous interval set + lookup) OK")
	return nil
}

// smokeLevel3: table + chains + set + multiple rules with mark expressions.
func smokeLevel3() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	prerouteChain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	outputChain := &nftables.Chain{Table: table, Name: "test_output"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipLoopbackExprs()})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (set+rules): %w", err)
	}
	fmt.Println("  AddSet + multiple rules (skip-mark, lookup, mark write) OK")
	return nil
}

// smokeLevel4: everything from level 3 in a SINGLE New()+Flush() batch.
func smokeLevel4() error {
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := conn.AddTable(&nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName})
	policy := nftables.ChainPolicyAccept
	prerouteChain := conn.AddChain(&nftables.Chain{
		Name: "test_prerouting", Table: table,
		Type: nftables.ChainTypeFilter, Hooknum: nftables.ChainHookPrerouting,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	outputChain := conn.AddChain(&nftables.Chain{
		Name: "test_output", Table: table,
		Type: nftables.ChainTypeRoute, Hooknum: nftables.ChainHookOutput,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipLoopbackExprs()})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (single batch): %w", err)
	}
	fmt.Println("  Single batch (table + chains + set + rules) OK")
	return nil
}

// smokeLevel5: install level 4, then re-install with FlushChain in a single batch.
func smokeLevel5() error {
	if err := smokeLevel4(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := conn.AddTable(&nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName})
	conn.FlushChain(&nftables.Chain{Table: table, Name: "test_prerouting"})
	conn.FlushChain(&nftables.Chain{Table: table, Name: "test_output"})
	policy := nftables.ChainPolicyAccept
	prerouteChain := conn.AddChain(&nftables.Chain{
		Name: "test_prerouting", Table: table,
		Type: nftables.ChainTypeFilter, Hooknum: nftables.ChainHookPrerouting,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	outputChain := conn.AddChain(&nftables.Chain{
		Name: "test_output", Table: table,
		Type: nftables.ChainTypeRoute, Hooknum: nftables.ChainHookOutput,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipLoopbackExprs()})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (re-install with FlushChain): %w", err)
	}
	fmt.Println("  Re-install (FlushChain + table + chains + set + rules) OK")
	return nil
}

// smokeLevel6: full kubespand pattern with both IPv4 and IPv6 sets.
func smokeLevel6() error {
	if err := smokeLevel4(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := conn.AddTable(&nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName})
	conn.FlushChain(&nftables.Chain{Table: table, Name: "test_prerouting"})
	conn.FlushChain(&nftables.Chain{Table: table, Name: "test_output"})
	policy := nftables.ChainPolicyAccept
	prerouteChain := conn.AddChain(&nftables.Chain{
		Name: "test_prerouting", Table: table,
		Type: nftables.ChainTypeFilter, Hooknum: nftables.ChainHookPrerouting,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	outputChain := conn.AddChain(&nftables.Chain{
		Name: "test_output", Table: table,
		Type: nftables.ChainTypeRoute, Hooknum: nftables.ChainHookOutput,
		Priority: nftables.ChainPriorityRaw, Policy: &policy,
	})
	v4Set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(v4Set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet v4: %w", err)
	}
	v6Set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIP6Addr}
	v6Start := make([]byte, 16)
	v6Start[0], v6Start[1] = 0xfd, 0x50
	v6Start[2], v6Start[3] = 0xca, 0xfe
	v6End := make([]byte, 16)
	copy(v6End, v6Start)
	v6End[12] = 1
	if err := conn.AddSet(v6Set, []nftables.SetElement{
		{Key: v6Start, IntervalEnd: false},
		{Key: v6End, IntervalEnd: true},
	}); err != nil {
		return fmt.Errorf("AddSet v6: %w", err)
	}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: lookupAndMarkIPv4Exprs(v4Set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: prerouteChain, Exprs: lookupAndMarkIPv6Exprs(v6Set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: skipLoopbackExprs()})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: mssClampIPv4Exprs(v4Set, 1380)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: lookupAndMarkIPv4Exprs(v4Set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: mssClampIPv6Exprs(v6Set, 1360)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: outputChain, Exprs: lookupAndMarkIPv6Exprs(v6Set, fwMark, fwMask)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (full kubespand pattern): %w", err)
	}
	fmt.Println("  Full kubespand pattern (dual-stack sets, mark, MSS clamp) OK")
	return nil
}

// Isolation sub-levels: each tests a single expression type to bisect EBUSY.

func smokeMarkRead() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: markReadExprs()})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (mark read only): %w", err)
	}
	fmt.Println("  mark read only: OK")
	return nil
}

func smokeMarkReadBitwise() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: skipMarkExprs(0x00000060, 0x000000e0)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (mark read+bitwise): %w", err)
	}
	fmt.Println("  mark read+bitwise+cmp: OK")
	return nil
}

func smokeMarkWrite() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: markWriteExprs(0x00000060)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (mark write): %w", err)
	}
	fmt.Println("  mark write (immediate + meta set): OK")
	return nil
}

func smokeMarkReadWrite() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: markReadWriteExprs(0x00000060)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (mark read+write): %w", err)
	}
	fmt.Println("  mark read+bitwise+write: OK")
	return nil
}

func smokeSetLookupOnly() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: v4LookupAcceptExprs(set)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (set lookup only): %w", err)
	}
	fmt.Println("  set lookup only (no mark): OK")
	return nil
}

func smokeLookupThenMark() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: v4LookupAcceptExprs(set)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: markWriteExprs(0x00000060)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (lookup rule + mark rule): %w", err)
	}
	fmt.Println("  set lookup rule + mark write rule (separate): OK")
	return nil
}

func smokeTwoRulesSetAndMark() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: skipMarkExprs(0x00000060, 0x000000e0)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: v4LookupAcceptExprs(set)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (skip-mark + lookup): %w", err)
	}
	fmt.Println("  skip-mark rule + set lookup rule: OK")
	return nil
}

func smokeSetLookupMarkInOneRule() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: lookupAndMarkIPv4Exprs(set, 0x00000060, 0x000000e0)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (lookup+mark in one rule): %w", err)
	}
	fmt.Println("  set lookup + mark write (single rule): OK")
	return nil
}

func smokeFiveRulesOneChain() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: skipLoopbackExprs()})
	conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (5 rules, 1 chain): %w", err)
	}
	fmt.Println("  5 rules on single chain: OK")
	return nil
}

func smokeTwoChainsTwoRules() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	preroute := &nftables.Chain{Table: table, Name: "test_prerouting"}
	output := &nftables.Chain{Table: table, Name: "test_output"}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: preroute, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: output, Exprs: skipMarkExprs(fwMark, fwMask)})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (2 rules, 2 chains): %w", err)
	}
	fmt.Println("  1 rule per chain (2 chains): OK")
	return nil
}

func smokeLevel3MinusOneRule() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	preroute := &nftables.Chain{Table: table, Name: "test_prerouting"}
	output := &nftables.Chain{Table: table, Name: "test_output"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	fwMark := uint32(0x00000060)
	fwMask := uint32(0x000000e0)
	conn.AddRule(&nftables.Rule{Table: table, Chain: preroute, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: preroute, Exprs: lookupAndMarkExprs(set, fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: output, Exprs: skipMarkExprs(fwMark, fwMask)})
	conn.AddRule(&nftables.Rule{Table: table, Chain: output, Exprs: skipLoopbackExprs()})
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (4 rules, 2 chains): %w", err)
	}
	fmt.Println("  level 3 minus last rule (4 rules, 2 chains): OK")
	return nil
}

func smokeFiveSimpleRules() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	for i := 0; i < 5; i++ {
		conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: acceptExprs()})
	}
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (5 simple rules): %w", err)
	}
	fmt.Println("  5 simple accept rules: OK")
	return nil
}

func smokeTwoLookupsSameSet() error {
	if err := smokeLevel1(); err != nil {
		return err
	}
	conn, err := nftables.New()
	if err != nil {
		return fmt.Errorf("nftables.New: %w", err)
	}
	table := &nftables.Table{Family: nftables.TableFamilyINet, Name: smokeTableName}
	chain := &nftables.Chain{Table: table, Name: "test_prerouting"}
	set := &nftables.Set{Table: table, Anonymous: true, Constant: true, Interval: true, KeyType: nftables.TypeIPAddr}
	if err := conn.AddSet(set, testV4Elements()); err != nil {
		return fmt.Errorf("AddSet: %w", err)
	}
	for i := 0; i < 2; i++ {
		conn.AddRule(&nftables.Rule{Table: table, Chain: chain, Exprs: v4LookupAcceptExprs(set)})
	}
	if err := flushRetry(conn); err != nil {
		return fmt.Errorf("Flush (2 lookups same set): %w", err)
	}
	fmt.Println("  2 rules referencing same anonymous set: OK")
	return nil
}

// testV4Elements returns the standard 10.244.0.0/16 test set elements.
func testV4Elements() []nftables.SetElement {
	return []nftables.SetElement{
		{Key: []byte{10, 244, 0, 0}, IntervalEnd: false},
		{Key: []byte{10, 245, 0, 0}, IntervalEnd: true},
	}
}

// v4LookupAcceptExprs returns exprs for: match IPv4 nfproto, lookup dst in set, accept.
func v4LookupAcceptExprs(set *nftables.Set) []expr.Any {
	return []expr.Any{
		&expr.Meta{Key: expr.MetaKeyNFPROTO, Register: 1},
		&expr.Cmp{Op: expr.CmpOpEq, Register: 1, Data: []byte{unix.NFPROTO_IPV4}},
		&expr.Payload{DestRegister: 1, Base: expr.PayloadBaseNetworkHeader, Offset: 16, Len: 4},
		&expr.Lookup{SourceRegister: 1, SetName: set.Name, SetID: set.ID},
		&expr.Verdict{Kind: expr.VerdictAccept},
	}
}
