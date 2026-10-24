package codex

import (
	"encoding/json"
	"slices"
	"testing"
)

func sendFrame(t *testing.T, router *Router[string], host, frame string) {
	t.Helper()
	if err := router.Sent(host, json.RawMessage(frame)); err != nil {
		t.Fatal(err)
	}
}

func assertRoute(t *testing.T, router *Router[string], frame string, want ...string) {
	t.Helper()
	if got := router.Route(json.RawMessage(frame)); !slices.Equal(got, want) {
		t.Fatalf("Route(%s) = %v, want %v", frame, got, want)
	}
}

func TestRouterAssignsFramesToTheSessionTheyConcern(t *testing.T) {
	router := NewRouter[string]()
	router.Host("first")
	router.Host("second")

	sendFrame(t, router, "first", `{"id":"start-1","method":"thread/start","params":{}}`)
	// The new thread may name itself before the thread/start response does.
	assertRoute(t, router, `{"method":"warning","params":{"threadId":"thread-first","message":"early"}}`, "first")
	assertRoute(t, router, `{"id":"start-1","result":{"thread":{"id":"thread-first"}}}`, "first")
	sendFrame(t, router, "second", `{"id":"resume-2","method":"thread/resume","params":{"threadId":"thread-second"}}`)
	assertRoute(t, router, `{"method":"turn/started","params":{"threadId":"thread-second","turn":{"id":"turn-2"}}}`, "second")
	assertRoute(t, router, `{"id":"resume-2","result":{"thread":{"id":"thread-second"}}}`, "second")

	assertRoute(t, router, `{"method":"item/completed","params":{"threadId":"thread-first","turnId":"turn-1"}}`, "first")
	assertRoute(t, router, `{"id":"approval","method":"execCommandApproval","params":{"conversationId":"thread-second"}}`, "second")
	// A sub-agent belongs to the Session of the root thread it descends from, at any depth.
	assertRoute(t, router, `{"method":"thread/started","params":{"thread":{"id":"child","parentThreadId":"thread-first"}}}`, "first")
	assertRoute(t, router, `{"method":"thread/started","params":{"thread":{"id":"grandchild","parentThreadId":"child"}}}`, "first")
	assertRoute(t, router, `{"method":"item/started","params":{"threadId":"grandchild"}}`, "first")
	// Frames naming no thread, or a thread no Session owns, concern every Session.
	assertRoute(t, router, `{"method":"account/rateLimits/updated","params":{"rateLimits":{}}}`, "first", "second")
	assertRoute(t, router, `{"method":"item/started","params":{"threadId":"unknown"}}`, "first", "second")
	assertRoute(t, router, `not json`, "first", "second")
}

func TestRouterRefusesConcurrentThreadStarts(t *testing.T) {
	router := NewRouter[string]()
	sendFrame(t, router, "first", `{"id":"start-1","method":"thread/start","params":{}}`)
	if err := router.Sent("second", json.RawMessage(`{"id":"start-2","method":"thread/start","params":{}}`)); err == nil {
		t.Fatal("a second unanswered thread/start would make unknown threads ambiguous")
	}
	assertRoute(t, router, `{"id":"start-1","error":{"code":-32600,"message":"refused"}}`)
	sendFrame(t, router, "second", `{"id":"start-2","method":"thread/start","params":{}}`)
}

func TestReleasedSessionReceivesNothingUntilHostedAgain(t *testing.T) {
	router := NewRouter[string]()
	router.Host("first")
	router.Host("second")
	sendFrame(t, router, "first", `{"id":"resume-1","method":"thread/resume","params":{"threadId":"thread-first"}}`)
	sendFrame(t, router, "first", `{"id":"turn-1","method":"turn/start","params":{"threadId":"thread-first"}}`)
	router.Release("first")
	assertRoute(t, router, `{"id":"resume-1","result":{"thread":{"id":"thread-first"}}}`)
	assertRoute(t, router, `{"method":"item/started","params":{"threadId":"thread-first"}}`)
	assertRoute(t, router, `{"method":"account/rateLimits/updated","params":{}}`, "second")
	router.Host("first")
	assertRoute(t, router, `{"id":"turn-1","result":{"turn":{"id":"turn-1"}}}`, "first")
	assertRoute(t, router, `{"method":"item/started","params":{"threadId":"thread-first"}}`, "first")
}
