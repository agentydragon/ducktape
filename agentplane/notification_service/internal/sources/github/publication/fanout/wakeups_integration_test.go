package fanout

import (
	"testing"
	"time"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
)

const awaitBound = 30 * time.Second

func observeFanoutWakeups(t *testing.T, g githubFixture) func(name string, want bool, step func()) {
	t.Helper()
	const sentinel = "notification_fanout_test_sentinel"
	hub, err := wakeups.Listen(t.Context(), g.databaseURL, wakeup.WorkChannel, sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	work, stopWork, err := hub.Subscribe(wakeup.WorkChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stopWork)
	settled, stopSettled, err := hub.Subscribe(sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stopSettled)

	return func(name string, want bool, step func()) {
		t.Helper()
		step()
		g.exec(t, "SELECT pg_notify($1, '')", sentinel)
		select {
		case <-settled:
		case <-time.After(awaitBound):
			t.Fatal("no wakeup from the sentinel")
		}
		select {
		case <-work:
			if !want {
				t.Errorf("%s woke workers, want no wakeup", name)
			}
		default:
			if want {
				t.Errorf("%s did not wake workers", name)
			}
		}
	}
}

func TestSharedGitHubChangesWakeOnlyAffectedSessionWatches(t *testing.T) {
	first := newGitHubFixtureWithStoreFixture(t, newSharedFixture(t, defaultQuotas()))
	other := newGitHubFixtureWithStoreFixture(t, newSharedFixture(t, defaultQuotas()))
	sub := first.follow(t, first.source(githubmodel.PullRequestSubject{Number: 7}), "first")
	other.follow(t, other.source(githubmodel.PullRequestSubject{Number: 7}), "other")
	const barrier = "notification_session_watch_barrier"
	hub, err := wakeups.Listen(t.Context(), first.databaseURL, wakeup.InboxChannel, barrier)
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	firstSignal, stopFirst, err := hub.SubscribeKey(wakeup.InboxChannel, first.sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer stopFirst()
	otherSignal, stopOther, err := hub.SubscribeKey(wakeup.InboxChannel, other.sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer stopOther()
	settled, stopSettled, err := hub.Subscribe(barrier)
	if err != nil {
		t.Fatal(err)
	}
	defer stopSettled()
	for name, update := range map[string]func(){
		"repository access": func() {
			first.exec(t, `UPDATE github_repository_access SET checked_at=now() WHERE app_id=$1 AND installation_id=$2 AND repository_id=$3`, first.app, first.installation, first.repository)
		},
		"subject": func() {
			first.exec(t, `UPDATE github_subject SET last_success_at=now() WHERE repository_id=$1 AND kind=$2 AND subject_key=$3`, sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key)
		},
	} {
		update()
		first.exec(t, `SELECT pg_notify($1, '')`, barrier)
		select {
		case <-settled:
		case <-time.After(awaitBound):
			t.Fatal("no barrier")
		}
		select {
		case <-firstSignal:
		default:
			t.Errorf("%s did not wake the affected Session", name)
		}
		select {
		case <-otherSignal:
			t.Errorf("%s woke an unrelated Session", name)
		default:
		}
	}
}
