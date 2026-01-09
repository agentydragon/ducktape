{
  pkgs,
  lib,
  config,
  inputs,
  ...
}: let
  # PostgreSQL configuration (single source of truth)
  pgConfig = {
    host = "127.0.0.1";
    port = "5433"; # Host-mapped port
    containerName = "props-postgres";
    containerPort = "5432"; # Internal container port (for Docker network communication)
    adminUser = "postgres";
    # Password stored in .devenv/state/pg_password (generated on first shell entry)
    database = "eval_results";
  };
  passwordFile = ".devenv/state/pg_password";

  # OCI Registry configuration (for agent packages as images)
  registryConfig = {
    host = "127.0.0.1";
    registryPort = "5050"; # Registry direct access (host-mapped)
    proxyPort = "5051"; # Proxy with ACL (host-mapped)
    registryContainerName = "props-registry";
    registryContainerPort = "5000"; # Internal registry port
    proxyContainerName = "props-registry-proxy";
    proxyContainerPort = "5051"; # Internal proxy port
  };
in {
  # Python/uv managed by root devenv.nix - this file only handles props-specific infra

  # Node.js for frontend development
  languages.javascript = {
    enable = true;
    package = pkgs.nodejs_22;
    pnpm.enable = true;
  };

  # PostgreSQL Docker container (managed via processes)
  # Networks: props-internal, props-agents (created in enterShell)
  # Container name: props-postgres (accessible from both networks)
  # Host access: localhost:5433
  processes.postgres.exec = ''
    # Stop and remove existing container if present
    docker rm -f ${pgConfig.containerName} 2>/dev/null || true

    # Read password from state file
    PG_PASSWORD=$(cat ${passwordFile})

    # Run PostgreSQL container
    # max_connections=200: Support higher parallel GEPA evaluation workloads
    # Connected to both networks for registry proxy and agent access
    docker run --rm \
      --name ${pgConfig.containerName} \
      --network props-internal \
      -p ${pgConfig.port}:${pgConfig.containerPort} \
      -e POSTGRES_USER=${pgConfig.adminUser} \
      -e POSTGRES_PASSWORD="$PG_PASSWORD" \
      -e POSTGRES_DB=${pgConfig.database} \
      -v props_eval_results_data:/var/lib/postgresql/data \
      postgres:16 \
      -c max_connections=200 &

    PG_PID=$!

    # Wait for container to start, then attach to props-agents network
    sleep 2
    docker network connect props-agents ${pgConfig.containerName} 2>/dev/null || true

    # Wait for postgres process
    wait $PG_PID
  '';

  # OCI Registry Docker container (for agent packages as images)
  # Network: props-internal only (agents cannot access directly)
  # Container name: props-registry
  # Host access: localhost:5050 (for Bazel push, debugging)
  processes.registry.exec = ''
    # Stop and remove existing container if present
    docker rm -f ${registryConfig.registryContainerName} 2>/dev/null || true

    # Run registry container
    docker run --rm \
      --name ${registryConfig.registryContainerName} \
      --network props-internal \
      -p ${registryConfig.registryPort}:${registryConfig.registryContainerPort} \
      -v props_registry_data:/var/lib/registry \
      registry:2
  '';

  # Registry proxy for ACL enforcement and metadata tracking
  # Networks: props-internal (to reach registry), props-agents (for agent access)
  # Agents connect to the proxy, which forwards to registry with ACL checks
  # Proxy enforces namespace isolation and records image refs in database
  processes.registry_proxy.exec = ''
    echo "Waiting for postgres..."
    until pg_isready -q; do sleep 1; done
    echo "Waiting for registry..."
    until curl -s http://localhost:${registryConfig.registryPort}/v2/ > /dev/null; do sleep 1; done

    # Stop and remove existing container if present
    docker rm -f ${registryConfig.proxyContainerName} 2>/dev/null || true

    # Read password from state file for database connection
    PG_PASSWORD=$(cat ${passwordFile})

    # Check if proxy image exists, build it if not
    if ! docker image inspect props-registry-proxy:latest >/dev/null 2>&1; then
      echo "Proxy image not found, building..."
      cd "$DEVENV_ROOT"
      bazel run //props/registry_proxy:load || {
        echo "ERROR: Failed to build proxy image"
        exit 1
      }
    fi

    # Run proxy container
    docker run --rm --name ${registryConfig.proxyContainerName} \
      --network props-internal \
      -p ${registryConfig.proxyPort}:${registryConfig.proxyContainerPort} \
      -e PROPS_REGISTRY_UPSTREAM_URL=http://${registryConfig.registryContainerName}:${registryConfig.registryContainerPort} \
      -e PGHOST=${pgConfig.containerName} -e PGPORT=${pgConfig.containerPort} \
      -e PGUSER=${pgConfig.adminUser} -e PGPASSWORD="$PG_PASSWORD" \
      -e PGDATABASE=${pgConfig.database} \
      props-registry-proxy:latest &

    PROXY_PID=$!

    # Wait for container to start, then attach to props-agents network
    sleep 2
    docker network connect props-agents ${registryConfig.proxyContainerName} 2>/dev/null || true

    # Wait for proxy process
    wait $PROXY_PID
  '';

  # Periodic database backup (every 6 hours, keeps 7 days)
  # Uses PG* env vars from devenv.env; PGPASSWORD read from state file
  processes.pg_backup.exec = ''
    BACKUP_DIR=".devenv/state/pg_backups"
    mkdir -p "$BACKUP_DIR"
    export PGPASSWORD=$(cat ${passwordFile})

    do_backup() {
      local TIMESTAMP=$(date +%Y%m%d_%H%M%S)
      local BACKUP_FILE="$BACKUP_DIR/props_backup_$TIMESTAMP.sql.gz"
      echo "Creating backup: $BACKUP_FILE"
      pg_dump | gzip > "$BACKUP_FILE"
    }

    echo "Waiting for postgres..."
    until pg_isready -q; do sleep 2; done

    do_backup
    while true; do
      sleep 21600  # 6 hours
      do_backup
      find "$BACKUP_DIR" -name "props_backup_*.sql.gz" -mtime +7 -delete
    done
  '';

  # Enable process logs in TUI.
  # devenv wraps process-compose commands through devenv-tasks to enable task
  # dependencies between processes. However, devenv-tasks captures stdout/stderr
  # into its own activity system and hides logs by default (showOutput=false).
  # This makes the process-compose TUI log panel empty. Override to show logs.
  # See: https://github.com/cachix/devenv/issues/2037
  tasks."devenv:processes:postgres".showOutput = true;

  # Environment variables (database connection parameters - single source of truth)
  env = {
    # Standard PostgreSQL client variables (host-side access)
    # PGPASSWORD is set dynamically in enterShell from .devenv/state/pg_password
    PGHOST = pgConfig.host;
    PGPORT = pgConfig.port;
    PGUSER = pgConfig.adminUser;
    PGDATABASE = pgConfig.database;

    # Project-specific: container routing (for Docker network communication)
    PROPS_DB_CONTAINER_NAME = pgConfig.containerName;
    PROPS_DB_CONTAINER_PORT = pgConfig.containerPort;

    # OCI Registry configuration
    # Host-side access (for bazel push, local development)
    PROPS_REGISTRY_HOST = registryConfig.host;
    PROPS_REGISTRY_PORT = registryConfig.registryPort;
    PROPS_REGISTRY_PROXY_PORT = registryConfig.proxyPort;
    # Container-side access (for agents pulling images from within Docker network)
    PROPS_REGISTRY_CONTAINER_NAME = registryConfig.registryContainerName;
    PROPS_REGISTRY_CONTAINER_PORT = registryConfig.registryContainerPort;
    PROPS_REGISTRY_PROXY_CONTAINER_NAME = registryConfig.proxyContainerName;
    PROPS_REGISTRY_PROXY_CONTAINER_PORT = registryConfig.proxyContainerPort;
  };

  # On shell entry
  enterShell = ''
    set -euo pipefail

    # Generate PostgreSQL password if not exists
    mkdir -p .devenv/state
    if [[ ! -f ${passwordFile} ]]; then
      echo "Generating PostgreSQL password..."
      ${pkgs.openssl}/bin/openssl rand -base64 24 > ${passwordFile}
      chmod 600 ${passwordFile}
    fi
    export PGPASSWORD=$(cat ${passwordFile})

    # Ensure Docker networks exist for proper isolation
    # props-internal: registry, proxy, postgres (not accessible to agents)
    # props-agents: proxy, postgres, agent containers (agents can only reach proxy)
    if command -v docker &> /dev/null; then
      if ! docker network inspect props-internal &> /dev/null; then
        echo "Creating Docker network 'props-internal' for registry + proxy + postgres..."
        docker network create props-internal --internal
      fi
      if ! docker network inspect props-agents &> /dev/null; then
        echo "Creating Docker network 'props-agents' for proxy + postgres + agents..."
        docker network create props-agents
      fi
      # Remove legacy props_default network if it exists
      if docker network inspect props_default &> /dev/null 2>&1; then
        echo "Removing legacy 'props_default' network..."
        docker network rm props_default 2>/dev/null || echo "  (network in use, will remove on next startup)"
      fi
    fi

    echo ""
    echo "Props dev environment ready"
    echo "  devenv up                          → starts postgres, registry, proxy + periodic backup"
    echo "  bazelisk run //props/frontend:dev  → frontend + backend (from direnv shell)"
    echo ""
    echo "Database backup commands:"
    echo "  props db backup        → create manual backup"
    echo "  props db restore FILE  → restore from backup"
    echo "  props db list-backups  → list available backups"
    echo ""
    echo "Registry:"
    echo "  Direct: http://localhost:${registryConfig.registryPort} (for Bazel push)"
    echo "  Proxy:  http://localhost:${registryConfig.proxyPort} (with ACL, for agents)"
    echo ""
  '';
}
