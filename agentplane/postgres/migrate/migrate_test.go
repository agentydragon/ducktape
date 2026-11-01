package migrate

import (
	"context"
	"database/sql"
	"io"
	"net"
	"net/url"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

// flakyFront drops the first `drop` connections it accepts, as a PostgreSQL that is still starting does, and
// forwards the rest to upstream.
type flakyFront struct {
	listener net.Listener
	upstream string
	drop     int32
	accepted atomic.Int32
}

func (f *flakyFront) serve() {
	for {
		conn, err := f.listener.Accept()
		if err != nil {
			return
		}
		if f.accepted.Add(1) <= f.drop {
			conn.Close()
			continue
		}
		go forward(conn, f.upstream)
	}
}

func forward(conn net.Conn, upstream string) {
	defer conn.Close()
	server, err := net.Dial("tcp", upstream)
	if err != nil {
		return
	}
	defer server.Close()
	go io.Copy(server, conn)
	io.Copy(conn, server)
}

func TestRunRetriesTheFirstConnection(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	database, err := postgres.Start(ctx, func(context.Context, *sql.DB) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := database.Close(); err != nil {
			t.Error(err)
		}
	})
	target, err := url.Parse(database.URL)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	// More drops than database/sql's own three attempts at a bad connection, so only Run's retry gets through.
	front := &flakyFront{listener: listener, upstream: target.Host, drop: 4}
	go front.serve()
	target.Host = listener.Addr().String()

	migrated := false
	err = Run(ctx, target.String(), func(ctx context.Context, db *sql.DB) error {
		migrated = true
		_, err := db.ExecContext(ctx, "SELECT 1")
		return err
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !migrated {
		t.Fatal("Run returned without applying the migrations")
	}
}
