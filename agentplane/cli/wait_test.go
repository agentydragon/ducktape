package main

import (
	"net/http"
	"strconv"
	"sync"
	"syscall"
	"testing"
	"time"
)

const waitedID = "00000000-0000-0000-0000-0000000000a1"

// holdingService answers a plain read at once with plain, and a read that asks to wait after
// holding it for hold, as the Action Service would, with held.
func holdingService(t *testing.T, plain, held func(requests int) string, hold time.Duration) *fake {
	var mu sync.Mutex
	waits := 0
	return newFake(t, func(w http.ResponseWriter, r *http.Request, s seen) {
		if s.query.Get("wait_seconds") == "" {
			reply(w, 200, plain(0))
			return
		}
		mu.Lock()
		waits++
		n := waits
		mu.Unlock()
		select {
		case <-time.After(hold):
		case <-r.Context().Done():
			return
		}
		reply(w, 200, held(n))
	})
}

func constant(body string) func(int) string { return func(int) string { return body } }

func TestWaitAndResultExitByOutcome(t *testing.T) {
	cases := []struct {
		state    string
		wantExit int
	}{
		{"succeeded", 0}, {"denied", 11}, {"failed", 12}, {"cancelled", 13}, {"execution_unknown", 14},
	}
	for _, tc := range cases {
		f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
			reply(w, 200, receiptJSON(waitedID, tc.state, "k"))
		})
		for _, command := range []string{"wait", "result"} {
			code, stdout, _ := invoke(actionsEnv(f.URL), "actions", command, waitedID, "--json")
			if got := jsonLines(t, stdout); code != tc.wantExit || (command == "wait" || tc.state != "succeeded") && (len(got) != 1 || got[0]["state"] != tc.state) {
				t.Errorf("%s of a %s Action: exit %d, stdout %q; want exit %d and its receipt", command, tc.state, code, stdout, tc.wantExit)
			}
		}
	}
}

func TestWaitUntilDecisionStopsBeforeTheOutcome(t *testing.T) {
	for state, wantExit := range map[string]int{"allowed": 0, "dispatching": 0, "running": 0, "denied": 11, "succeeded": 0, "failed": 12} {
		f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, receiptJSON(waitedID, state, "k")) })
		if code, _, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--until", "decision"); code != wantExit {
			t.Errorf("--until decision on %s: exit %d, want %d", state, code, wantExit)
		}
	}
}

func TestWaitExits10AtTheDeadlineWithTheCurrentReceipt(t *testing.T) {
	for _, state := range []string{"decision_pending", "allowed", "dispatching", "running"} {
		f := holdingService(t, constant(receiptJSON(waitedID, state, "k")), constant(receiptJSON(waitedID, state, "k")), 50*time.Millisecond)
		start := time.Now()
		code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "1", "--json")
		if got := jsonLines(t, stdout); code != exitDeadline || len(got) != 1 || got[0]["state"] != state {
			t.Errorf("%s: exit %d, stdout %q; want exit 10 and the current receipt", state, code, stdout)
		}
		if elapsed := time.Since(start); elapsed > 2*time.Second {
			t.Errorf("%s: took %v for a 1s budget", state, elapsed)
		}
		for _, s := range f.requests("GET") {
			if seconds, _ := strconv.ParseFloat(s.query.Get("wait_seconds"), 64); seconds > 1 {
				t.Errorf("asked the server to wait %v s within a 1s budget", seconds)
			}
		}
	}
}

func TestResultAtTheDeadlineExits10WithTheReceipt(t *testing.T) {
	f := holdingService(t, constant(receiptJSON(waitedID, "running", "k")), constant(receiptJSON(waitedID, "running", "k")), 50*time.Millisecond)
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "result", waitedID, "--timeout", "1", "--json")
	if got := decodeObject(t, stdout); code != exitDeadline || got["state"] != "running" {
		t.Errorf("exit %d, stdout %q; want exit 10 and the receipt", code, stdout)
	}
}

func TestWaitReissuesServerWaitsUntilTheConditionHolds(t *testing.T) {
	f := holdingService(t, constant(receiptJSON(waitedID, "running", "k")), func(n int) string {
		if n < 3 {
			return receiptJSON(waitedID, "running", "k")
		}
		return receiptJSON(waitedID, "succeeded", "k")
	}, 100*time.Millisecond)
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "10")
	waits := 0
	for _, s := range f.requests("GET") {
		if s.query.Get("wait_seconds") != "" {
			waits++
			if s.query.Get("wait_until") != "terminal" {
				t.Errorf("wait_until = %q, want terminal", s.query.Get("wait_until"))
			}
		}
	}
	if code != 0 || waits != 3 {
		t.Errorf("exit %d after %d server waits, want exit 0 after 3", code, waits)
	}
}

func TestWaitBudgetBeyondTheServerCapIsSplitIntoWaits(t *testing.T) {
	f := holdingService(t, constant(receiptJSON(waitedID, "running", "k")), func(n int) string {
		if n < 3 {
			return receiptJSON(waitedID, "running", "k")
		}
		return receiptJSON(waitedID, "succeeded", "k")
	}, 0)
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "600")
	waits := 0
	for _, s := range f.requests("GET") {
		if s.query.Get("wait_seconds") == "" {
			continue
		}
		waits++
		// 30 is the Action Service's default max_wait_seconds.
		if seconds, _ := strconv.ParseFloat(s.query.Get("wait_seconds"), 64); seconds > 30 {
			t.Errorf("asked the server to wait %v s, more than its default cap of 30", seconds)
		}
	}
	if code != 0 || waits != 3 {
		t.Errorf("exit %d after %d server waits, want exit 0 after 3", code, waits)
	}
}

func TestWaitWithTimeoutZeroReadsOnce(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, receiptJSON(waitedID, "running", "k"))
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "0", "--json")
	reads := f.requests("GET")
	if got := decodeObject(t, stdout); code != exitDeadline || got["state"] != "running" || len(reads) != 1 || reads[0].query.Get("wait_seconds") != "" {
		t.Errorf("exit %d, stdout %q, reads %+v; want exit 10 after one plain read", code, stdout, reads)
	}
}

func TestWaitByKeyResolvesTheIDFirst(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, s seen) {
		if r.URL.Path == "/v1/action-requests" {
			reply(w, 200, "["+receiptJSON(waitedID, "running", s.query.Get("idempotency_key"))+"]")
			return
		}
		reply(w, 200, receiptJSON(waitedID, "succeeded", "k"))
	})
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "wait", "--key", "k", "--timeout", "5")
	reads := f.requests("GET")
	if code != 0 || len(reads) != 2 || reads[0].query.Get("idempotency_key") != "k" || reads[1].path != "/v1/action-requests/"+waitedID {
		t.Errorf("exit %d, reads %+v; want the key lookup, then the wait on the ID it found", code, reads)
	}
}

func TestWaitByUnknownKeyExitsNotFound(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, "[]") })
	if code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "wait", "--key", "nobody"); code != 5 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestWaitRetriesDroppedConnectionsWithinTheBudget(t *testing.T) {
	var mu sync.Mutex
	requests := 0
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		mu.Lock()
		requests++
		n := requests
		mu.Unlock()
		if n <= 2 {
			drop(w)
			return
		}
		reply(w, 200, receiptJSON(waitedID, "succeeded", "k"))
	})
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "10")
	if code != 0 || f.count() != 3 {
		t.Errorf("exit %d after %d requests, want exit 0 on the third", code, f.count())
	}
}

func TestWaitThatLosesTheServiceExits3WithTheLastReceipt(t *testing.T) {
	var mu sync.Mutex
	requests := 0
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		mu.Lock()
		requests++
		n := requests
		mu.Unlock()
		if n > 1 {
			drop(w)
			return
		}
		reply(w, 200, receiptJSON(waitedID, "running", "k"))
	})
	start := time.Now()
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "2", "--json")
	got := decodeObject(t, stdout)
	if code != 3 || got["state"] != "running" || errorOf(t, stderr).Code != "UNAVAILABLE" {
		t.Errorf("exit %d, stdout %q, stderr %q; want exit 3 with the running receipt", code, stdout, stderr)
	}
	if f.count() < 3 {
		t.Errorf("%d requests, want it to keep retrying within the budget", f.count())
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Errorf("took %v for a 2s budget", elapsed)
	}
}

func TestWaitThatNeverReachedTheServiceHasNothingToPrint(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { drop(w) })
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "1")
	if code != 3 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 3 and no output", code, stdout)
	}
}

func TestWaitDoesNotRetryARefusal(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 403, `{"detail":"no"}`) })
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "5")
	if code != 1 || f.count() != 1 {
		t.Errorf("exit %d after %d requests, want exit 1 after one", code, f.count())
	}
}

func TestWaitRefusesAStateItDoesNotKnow(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, receiptJSON(waitedID, "teleported", "k"))
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "wait", waitedID, "--timeout", "5")
	if code != 1 || stdout != "" || f.count() != 1 {
		t.Errorf("exit %d, stdout %q after %d requests; want exit 1 at once", code, stdout, f.count())
	}
}

func TestSigtermEndsAWaitAndCancelsNothing(t *testing.T) {
	arrived := make(chan struct{})
	var once sync.Once
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, s seen) {
		if s.query.Get("wait_seconds") == "" {
			reply(w, 200, receiptJSON(waitedID, "running", "k"))
			return
		}
		once.Do(func() { close(arrived) })
		<-r.Context().Done()
	})
	c := newCLI(actionsEnv(f.URL), "")
	exited := make(chan int, 1)
	go func() { exited <- c.run("actions", "wait", waitedID, "--timeout", "60") }()
	select {
	case <-arrived:
	case <-time.After(10 * time.Second):
		t.Fatal("the wait never reached the service")
	}
	if err := syscall.Kill(syscall.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case code := <-exited:
		if code != 128+int(syscall.SIGTERM) || c.stdout.String() != "" {
			t.Errorf("exit %d, stdout %q; want 143 and no output", code, c.stdout.String())
		}
	case <-time.After(10 * time.Second):
		t.Fatal("SIGTERM did not end the wait")
	}
	for _, s := range f.all() {
		if s.method != http.MethodGet {
			t.Errorf("the interrupted wait sent %s %s", s.method, s.path)
		}
	}
}
