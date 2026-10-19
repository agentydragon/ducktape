// Package postgres provides the real PostgreSQL fixture for Agentplane Go integration tests.
package postgres

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
	_ "github.com/lib/pq"
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
)

// Database owns a PostgreSQL container and the connection migrated by the service's production Goose code.
type Database struct {
	URL       string
	container testcontainers.Container
}

// Start loads the repository-pinned PostgreSQL and Ryuk images, starts PostgreSQL, and applies migrate.
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
	return terminate(db.container)
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
