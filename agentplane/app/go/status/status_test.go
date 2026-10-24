package status

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestCheckAllPreservesProbeOrderAndFormatsFailures(t *testing.T) {
	checker, err := New([]Probe{
		{Name: "healthy", Check: func(context.Context) error { return nil }},
		{Name: "failed", Check: func(context.Context) error { return errors.New("connection refused") }},
	}, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	results := checker.CheckAll(t.Context())
	if len(results) != 2 || results[0].Name != "healthy" || results[0].State != "ok" || results[0].Detail != nil {
		t.Fatalf("healthy result = %#v", results)
	}
	if results[1].Name != "failed" || results[1].State != "down" || results[1].Detail == nil || !strings.Contains(*results[1].Detail, "connection refused") {
		t.Fatalf("failed result = %#v", results[1])
	}
	if results[0].CheckedAt.Location() != time.UTC {
		t.Fatalf("checked_at location = %v, want UTC", results[0].CheckedAt.Location())
	}
}

func TestCheckAllEnforcesIndependentTimeout(t *testing.T) {
	checker, err := New([]Probe{{Name: "wedged", Check: func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	}}}, 10*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	result := checker.CheckAll(t.Context())[0]
	if result.State != "down" || result.Detail == nil || *result.Detail != "no answer within 0.01s" {
		t.Fatalf("result = %#v", result)
	}
}
