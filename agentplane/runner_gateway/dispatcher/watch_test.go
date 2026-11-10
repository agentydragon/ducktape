package dispatcher

import "testing"

func TestTerminalEventsFailOnlyStreamsThatFallBehind(t *testing.T) {
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	changes, _, err := hub.subscribeChanges()
	if err != nil {
		t.Fatal(err)
	}
	stream, _, err := hub.subscribe()
	if err != nil {
		t.Fatal(err)
	}
	// One more than a stream's buffer, none of them read: the delivery worker's case.
	for range cap(stream.terminal) + 1 {
		hub.notifyTerminal(TerminalEvent{})
	}
	select {
	case message := <-changes.failure:
		t.Fatalf("change-only subscriber failed on terminal events: %s", message)
	default:
	}
	select {
	case <-stream.failure:
	default:
		t.Fatal("a stream that fell behind on terminal events was not failed")
	}
}
