// Package postgres provides the real PostgreSQL fixture for Agentplane Go integration tests.
package postgres

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"sync"
	"testing"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/lib/pq"
	"github.com/moby/moby/client"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/wait"
)

const (
	postgresImage = "postgres:18"
	postgresUser  = "postgres"
	postgresPass  = "postgres"
	postgresDB    = "postgres"
	ryukImage     = "testcontainers/ryuk:0.14.0"
	// templateDB is the copy of the migrated database that Clone copies.
	templateDB = "migrated_template"
)

// Database owns a PostgreSQL container and the connection migrated by the service's production Goose code.
type Database struct {
	URL       string
	container testcontainers.Container

	mu sync.Mutex
	// admin creates and drops the clones; nil until the first Clone.
	admin  *sql.DB
	clones int
}

// RunIntegrationTests starts the PostgreSQL fixture, then runs the package tests and closes the fixture.
// Startup and migrations share a three-minute bound; tests run without that startup deadline.
func RunIntegrationTests(migrate func(context.Context, *sql.DB) error, run func(*Database) int) int {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	database, err := Start(ctx, migrate)
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	code := run(database)
	if err := database.Close(); err != nil {
		fmt.Fprintln(os.Stderr, "close PostgreSQL fixture:", err)
		if code == 0 {
			code = 1
		}
	}
	return code
}

// Start ensures the PostgreSQL and Ryuk image tags are available, starts PostgreSQL, and applies migrate.
func Start(ctx context.Context, migrate func(context.Context, *sql.DB) error) (*Database, error) {
	if err := loadImage(ctx, postgresImage, "ducktape/third_party/containers/postgres_18_load.sh"); err != nil {
		return nil, err
	}
	if err := loadImage(ctx, ryukImage, "ducktape/third_party/containers/ryuk_014_load.sh"); err != nil {
		return nil, err
	}

	container, err := testcontainers.Run(
		ctx, postgresImage,
		testcontainers.WithEnv(map[string]string{
			"POSTGRES_USER":     postgresUser,
			"POSTGRES_PASSWORD": postgresPass,
			"POSTGRES_DB":       postgresDB,
		}),
		testcontainers.WithExposedPorts("5432/tcp"),
		testcontainers.WithWaitStrategy(wait.ForAll(
			wait.ForLog("database system is ready to accept connections").WithOccurrence(2).WithStartupTimeout(2*time.Minute),
			wait.ForListeningPort("5432/tcp").WithStartupTimeout(2*time.Minute),
		)),
	)
	if err != nil {
		return nil, fmt.Errorf("start PostgreSQL container: %w", err)
	}

	url, db, err := openDatabase(ctx, container)
	if err != nil {
		return nil, errors.Join(err, terminate(container))
	}
	if err := migrate(ctx, db); err != nil {
		return nil, errors.Join(fmt.Errorf("apply production Goose migrations: %w", err), db.Close(), terminate(container))
	}
	if err := db.Close(); err != nil {
		return nil, errors.Join(fmt.Errorf("close migration connection: %w", err), terminate(container))
	}
	return &Database{URL: url, container: container}, nil
}

// Close terminates PostgreSQL. Testcontainers' Ryuk remains responsible for cleanup if the Go test
// process exits before this explicit shutdown runs.
func (db *Database) Close() error {
	var closeAdmin error
	if db.admin != nil {
		closeAdmin = db.admin.Close()
	}
	return errors.Join(closeAdmin, terminate(db.container))
}

// Clone returns the URL of a database of the test's own, copied from the migrated one, which the test's cleanup
// drops: it starts with the schema and no rows, so queries that span every row see only the test's, and its NOTIFY
// channels reach only its own connections. A test package that clones connects to the migrated database only
// through clones, since a copy needs it idle.
func (db *Database) Clone(t testing.TB) string {
	t.Helper()
	db.mu.Lock()
	defer db.mu.Unlock()
	if db.admin == nil {
		admin, err := db.createTemplate(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		db.admin = admin
	}
	db.clones++
	name := fmt.Sprintf("test_%d", db.clones)
	if _, err := db.admin.ExecContext(t.Context(), `CREATE DATABASE `+name+` TEMPLATE `+templateDB); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := db.admin.Exec(`DROP DATABASE ` + name + ` WITH (FORCE)`); err != nil {
			t.Error(err)
		}
	})
	return db.databaseURL(name)
}

func (db *Database) databaseURL(name string) string {
	parsed, err := url.Parse(db.URL)
	if err != nil {
		panic(err)
	}
	parsed.Path = "/" + name
	return parsed.String()
}

// createTemplate copies the migrated database into templateDB, from a connection to another database.
func (db *Database) createTemplate(ctx context.Context) (*sql.DB, error) {
	admin, err := sql.Open("postgres", db.databaseURL("template1"))
	if err != nil {
		return nil, err
	}
	// The migrator's connection may still be closing on the server.
	for {
		_, err = admin.ExecContext(ctx, `CREATE DATABASE `+templateDB+` TEMPLATE `+postgresDB)
		var failure *pq.Error
		if !errors.As(err, &failure) || failure.Code != "55006" {
			break
		}
		select {
		case <-ctx.Done():
			return nil, errors.Join(err, admin.Close())
		case <-time.After(50 * time.Millisecond):
		}
	}
	if err != nil {
		return nil, errors.Join(err, admin.Close())
	}
	return admin, nil
}

func openDatabase(ctx context.Context, container testcontainers.Container) (string, *sql.DB, error) {
	endpoint, err := container.PortEndpoint(ctx, "5432/tcp", "")
	if err != nil {
		return "", nil, fmt.Errorf("get PostgreSQL endpoint: %w", err)
	}
	url := fmt.Sprintf("postgres://%s:%s@%s/%s?sslmode=disable", postgresUser, postgresPass, endpoint, postgresDB)
	db, err := sql.Open("postgres", url)
	if err != nil {
		return "", nil, fmt.Errorf("open PostgreSQL connection: %w", err)
	}
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return "", nil, fmt.Errorf("ping PostgreSQL: %w", err)
	}
	return url, db, nil
}

func loadImage(ctx context.Context, image, loaderRlocation string) error {
	dockerClient, err := client.New(client.FromEnv)
	if err != nil {
		return fmt.Errorf("create Docker client to inspect %s: %w", image, err)
	}
	_, inspectErr := dockerClient.ImageInspect(ctx, image)
	closeErr := dockerClient.Close()
	// TODO: Verify cached images against the Bazel pin; consider sharing
	// the image-loading path with Python.
	if inspectErr == nil {
		return closeErr
	}
	if closeErr != nil {
		return fmt.Errorf("close Docker client after inspecting %s: %w", image, closeErr)
	}

	loader, err := runfiles.Rlocation(loaderRlocation)
	if err != nil {
		return fmt.Errorf("locate Bazel image loader for %s: %w", image, err)
	}
	command := exec.CommandContext(ctx, loader)
	command.Stdout, command.Stderr = os.Stdout, os.Stderr
	if err := command.Run(); err != nil {
		return fmt.Errorf("load Bazel image %s: %w", image, err)
	}
	return nil
}

func terminate(container testcontainers.Container) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := testcontainers.TerminateContainer(container, testcontainers.StopContext(ctx), testcontainers.StopTimeout(30*time.Second)); err != nil {
		return fmt.Errorf("terminate PostgreSQL container: %w", err)
	}
	return nil
}
