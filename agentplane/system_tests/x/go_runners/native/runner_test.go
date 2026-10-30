package native_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/system_tests/x/go_runners/driver"
)

func TestSharedStack(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	host, err := driver.Start(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// A parent cleanup runs after every parallel child and its cleanup have completed.
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
		defer cancel()
		if err := host.Verify(ctx); err != nil {
			t.Error(err)
		}
		if err := host.Stop(ctx); err != nil {
			t.Error(err)
		}
	})
	t.Run("scenarios", func(t *testing.T) {
		for _, name := range driver.Cases() {
			t.Run(name, func(t *testing.T) {
				t.Parallel()
				if err := host.Begin(t.Context(), name); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() {
					// t.Context is cancelled before Cleanup; teardown needs its own deadline.
					ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
					defer cancel()
					if err := host.Cleanup(ctx, name); err != nil {
						t.Error(err)
					}
				})
				ctx, cancel := context.WithTimeout(t.Context(), driver.CaseTimeout(name))
				defer cancel()
				if err := host.Run(ctx, name); err != nil {
					t.Fatal(err)
				}
			})
		}
	})
	// This phase must have exclusive access after the entire parallel group has drained.
	t.Run("exclusive", func(t *testing.T) {
		if err := host.Verify(t.Context()); err != nil {
			t.Fatal(err)
		}
	})
}
