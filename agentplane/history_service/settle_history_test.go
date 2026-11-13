package main

import "testing"

func TestSettlementRequiresAnExplicitScopeAndWritePolicy(t *testing.T) {
	const session = "00000000-0000-4000-8000-000000000001"
	for _, args := range [][]string{
		nil,
		{"--all"},
		{"--dry-run"},
		{"--all", "--apply", "--dry-run"},
		{"--all", "--session", session, "--apply"},
		{"--session", "bad", "--apply"},
		{"--session", "00000000-0000-0000-0000-000000000000", "--apply"},
		{"--all", "--apply", "extra"},
	} {
		if _, err := parseSettlementOptions(args); err == nil {
			t.Fatalf("accepted %v", args)
		}
	}
	for _, apply := range []bool{false, true} {
		mode := "--dry-run"
		if apply {
			mode = "--apply"
		}
		one, err := parseSettlementOptions([]string{"--session", session, mode})
		if err != nil || one.all || one.session.String() != session || one.apply != apply {
			t.Fatalf("one Session: %+v %v", one, err)
		}
		all, err := parseSettlementOptions([]string{"--all", mode})
		if err != nil || !all.all || all.apply != apply {
			t.Fatalf("all Sessions: %+v %v", all, err)
		}
	}
}
