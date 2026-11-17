package main

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

// awaitBound only fails a test whose worker never reports.
const awaitBound = 30 * time.Second

const privateMessage = "never-print-this delivery payload"

func receive(t *testing.T, failed <-chan error) error {
	t.Helper()
	select {
	case err := <-failed:
		return err
	case <-time.After(awaitBound):
		t.Fatal("no worker reported")
		return nil
	}
}

func TestAWorkerThatFailsIsFatalAndNamesItsErrorsTypesNotItsMessage(t *testing.T) {
	failed := make(chan error, 2)
	running := runWorkers(t.Context(), 2, failed, func(ctx context.Context, index int) error {
		if index == 1 {
			<-ctx.Done()
			return nil
		}
		return fmt.Errorf("delivery: %w", errors.New(privateMessage))
	})
	err := receive(t, failed)
	var stopped workerStopped
	if !errors.As(err, &stopped) || stopped.worker != 0 {
		t.Fatalf("failure = %v, want worker 0 stopped", err)
	}
	if strings.Contains(err.Error(), "never-print-this") || !strings.Contains(err.Error(), "*fmt.wrapError, *errors.errorString") {
		t.Errorf("failure %q does not name the types alone", err)
	}
	t.Cleanup(running.Wait)
}

func TestAWorkerThatReturnsWithoutAnErrorWhileServingIsFatal(t *testing.T) {
	failed := make(chan error, 1)
	running := runWorkers(t.Context(), 1, failed, func(context.Context, int) error { return nil })
	if err := receive(t, failed); !strings.Contains(err.Error(), "returned without an error") {
		t.Errorf("failure = %v", err)
	}
	running.Wait()
}

func TestWorkersThatReturnOnceStoppedReportNothing(t *testing.T) {
	ctx, stop := context.WithCancel(t.Context())
	failed := make(chan error, 2)
	running := runWorkers(ctx, 2, failed, func(ctx context.Context, _ int) error {
		<-ctx.Done()
		return ctx.Err()
	})
	stop()
	running.Wait()
	select {
	case err := <-failed:
		t.Errorf("a stopped worker reported %v", err)
	default:
	}
}
