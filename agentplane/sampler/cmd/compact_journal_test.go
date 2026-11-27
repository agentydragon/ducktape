package main

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
)

func TestRetryBusyJournals(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		start := time.Now()
		var visits []string
		var times []time.Duration
		permanent := errors.New("invalid journal")
		err := retryBusyJournals(t.Context(), []string{"hot", "cold", "broken"}, func(id string) error {
			visits = append(visits, id)
			if id == "hot" {
				times = append(times, time.Since(start))
				if len(times) < 9 {
					return fmt.Errorf("compact: %w", sessionstore.ErrJournalBusy)
				}
			}
			if id == "broken" {
				return permanent
			}
			return nil
		})
		if !errors.Is(err, permanent) || len(visits) != 11 || !slices.Equal(visits[:3], []string{"hot", "cold", "broken"}) {
			t.Fatalf("visits=%v, error=%v", visits, err)
		}
		if times[0] != 5*time.Second || times[1]-times[0] != 10*time.Second || times[8]-times[7] != 5*time.Minute {
			t.Errorf("retry times: %v", times)
		}
	})
}

func TestCancelBusyJournalBackoff(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(t.Context(), time.Second)
		defer cancel()
		err := retryBusyJournals(ctx, []string{"busy"}, func(string) error { t.Fatal("retried after cancellation"); return nil })
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("retry returned %v", err)
		}
	})
}
