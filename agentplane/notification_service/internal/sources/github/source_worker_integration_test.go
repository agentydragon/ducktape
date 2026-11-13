package github

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources"
)

func TestSourceJobsPublishWithoutADestinationAndStayIdleAfterCacheExpiry(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	w := sources.Worker{Store: h.store, Providers: map[string]sources.Provider{"github": h.provider}}
	if worked, err := w.ProcessOne(t.Context()); err != nil || !worked {
		t.Fatalf("bootstrap = %v, %v; want one processed source job", worked, err)
	}
	// Cached access and observations becoming stale does not create new source work.
	h.exec(`UPDATE github_repository_access SET valid_until = now() - interval '1 second'`)
	h.exec(`UPDATE github_subject SET last_success_at = now() - interval '1 day'`)
	requests := h.fake.Total()
	if worked, err := w.ProcessOne(t.Context()); err != nil || worked {
		t.Fatalf("idle = %v, %v; want no source job", worked, err)
	}
	if got := h.fake.Total(); got != requests {
		t.Fatalf("idle source made %d GitHub requests", got-requests)
	}

	// No Sandbox, Dispatcher, History or delivery worker exists in this fixture. A webhook still
	// publishes its durable inbox entry, after renewing stale access for that actual pending work.
	h.ingest("issue_comment", comment(7))
	if worked, err := w.ProcessOne(t.Context()); err != nil || !worked {
		t.Fatalf("webhook = %v, %v; want one processed source job", worked, err)
	}
	page := h.page(subscription.InboxID)
	if len(page.Entries) != 1 || page.Inbox.LastCursor != 1 || page.Notice != nil {
		t.Fatalf("webhook inbox = %+v; want one durable event before delivery", page)
	}
	if worked, err := w.ProcessOne(t.Context()); err != nil || worked {
		t.Fatalf("after webhook = %v, %v; want source idle", worked, err)
	}
}
