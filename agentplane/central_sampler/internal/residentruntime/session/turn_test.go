package session

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
)

func TestJoinCanceledAdvanceRetainsSettledResult(t *testing.T) {
	turnCtx, cancelTurn := context.WithCancel(context.Background())
	defer cancelTurn()
	advanceCtx, cancelAdvance := context.WithCancel(turnCtx)
	defer cancelAdvance()

	completed := make(chan controlledAdvanceResult, 1)
	responseSettled := make(chan struct{})
	releaseResult := make(chan struct{})
	go func() {
		<-responseSettled
		<-advanceCtx.Done()
		<-releaseResult
		completed <- controlledAdvanceResult{result: sessionstore.TurnSample}
	}()

	// A provider response has settled before the turn is canceled, but its
	// advance result is published only after cancellation. Channels control this
	// ordering without relying on a timer.
	close(responseSettled)
	cancelTurn()
	close(releaseResult)
	result, err := joinCanceledAdvance(turnCtx, cancelAdvance, completed)
	if err != context.Canceled || result != sessionstore.TurnSample {
		t.Fatalf("joined advance=(%q, %v), want (%q, %v)", result, err, sessionstore.TurnSample, context.Canceled)
	}
}
