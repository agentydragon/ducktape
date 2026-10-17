package history

import (
	"context"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestConnectionStringBoundsConnectsUnlessTheURLSaysOtherwise(t *testing.T) {
	for _, test := range []struct{ url, timeout, application string }{
		{"postgresql://test-user:test-password@db.test/history", "2", "test-app"},
		{"postgres://db.test/history?connect_timeout=9&application_name=other", "9", "other"},
	} {
		dsn, err := connectionString(test.url, "test-app")
		if err != nil {
			t.Fatal(err)
		}
		parsed, err := url.Parse(dsn)
		if err != nil {
			t.Fatal(err)
		}
		query := parsed.Query()
		if query.Get("connect_timeout") != test.timeout || query.Get("application_name") != test.application {
			t.Errorf("%s -> %s", test.url, dsn)
		}
	}
}

func TestInvalidDatabaseURLNeverEchoesThePassword(t *testing.T) {
	for _, bad := range []string{
		"postgres://test-user:test-secret@db.test:not-a-port/history",
		"mysql://test-user:test-secret@db.test/history",
	} {
		_, err := connectionString(bad, "test-app")
		if err == nil {
			t.Fatalf("%q accepted", bad)
		}
		if strings.Contains(err.Error(), "test-secret") {
			t.Errorf("error leaks the password: %v", err)
		}
	}
}

func TestFollowFailsWithoutSubscribingWhenTheDatabaseIsDown(t *testing.T) {
	changes, err := NewChanges("postgresql://test-user:test-password@127.0.0.1:1/history?sslmode=disable", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err = changes.Follow(ctx, func() error {
		t.Error("told the reader about changes without a connection")
		return nil
	})
	if err == nil || ctx.Err() != nil {
		t.Fatalf("Follow returned %v after %v", err, ctx.Err())
	}
}
