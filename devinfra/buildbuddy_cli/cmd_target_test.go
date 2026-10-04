package main

import (
	"strings"
	"testing"
)

func TestChooseLogsPrefersTheResultsThatFailed(t *testing.T) {
	logs := logsOf(parseOrFail(t, shardedStream), "//pkg:visual")
	chosen, why := chooseLogs(logs)
	if len(chosen) != 1 || chosen[0].Shard != 2 {
		t.Errorf("chose %v", describeAll(chosen))
	}
	if !strings.Contains(why, "1 of 3") {
		t.Errorf("why = %q", why)
	}
}

func TestChooseLogsFallsBackToTheFirstWhenNothingFailed(t *testing.T) {
	logs := logsOf(parseOrFail(t, shardedStream), "//pkg:visual")
	logs[1].Status = "PASSED"
	chosen, why := chooseLogs(logs)
	if len(chosen) != 1 || chosen[0].Shard != 1 {
		t.Errorf("chose %v", describeAll(chosen))
	}
	if !strings.Contains(why, "None failed") {
		t.Errorf("why = %q", why)
	}
}
