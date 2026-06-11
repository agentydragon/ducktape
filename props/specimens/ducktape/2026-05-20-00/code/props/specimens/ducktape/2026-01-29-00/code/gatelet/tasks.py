"""Development tasks using invoke."""

import time
from pathlib import Path

from invoke import task

COMPOSE_FILE = "docker/docker-compose.yml"


def docker_compose(c, command, pty=False):
    """Run a docker-compose command with the project's compose file."""
    c.run(f"docker-compose -f {COMPOSE_FILE} {command}", pty=pty)


def docker_compose_exec(c, service, command, pty=True):
    """Run a command inside a running container."""
    docker_compose(c, f"exec {service} {command}", pty=pty)


@task
def up(c):
    """Start development environment with live reload."""
    docker_compose(c, "up -d")
    print("Gatelet is running at http://localhost:8000")
    docker_compose(c, "logs -f gatelet", pty=True)


@task
def down(c):
    """Stop development environment."""
    docker_compose(c, "down")


@task
def build(c):
    """Build Docker image."""
    docker_compose(c, "build")


@task
def logs(c, service="gatelet"):
    """Show logs (optionally specify service)."""
    docker_compose(c, f"logs -f {service}", pty=True)


@task
def shell(c):
    """Open shell in Gatelet container."""
    docker_compose_exec(c, "gatelet", "bash")


@task
def db(c):
    """Connect to PostgreSQL database."""
    docker_compose_exec(c, "db", "psql -U postgres -d gatelet")


@task
def migrate(c):
    """Run database migrations."""
    docker_compose_exec(c, "gatelet", "alembic upgrade head")


@task
def reset_db(c):
    """Reset database (WARNING: destroys all data)."""
    docker_compose(c, "down -v")
    docker_compose(c, "up -d db")
    time.sleep(5)
    docker_compose(c, "up -d gatelet")


@task
def run_tests(c, args=""):
    """Run tests."""
    docker_compose_exec(c, "gatelet", f"pytest {args}")


@task
def migration(c, name):
    """Create new migration."""
    docker_compose_exec(c, "gatelet", f'alembic revision --autogenerate -m "{name}"')


@task
def lint(c):
    """Run linting and formatting checks."""
    docker_compose_exec(c, "gatelet", 'bash -c "isort --check-only . && black --check ."')


@task
def format(c):
    """Auto-format code."""
    docker_compose_exec(c, "gatelet", 'bash -c "isort . && black ."')


@task
def check_config(c):
    """Check if config exists, create from example if not."""
    if not Path("gatelet.toml").exists():
        print("Creating gatelet.toml from example...")
        c.run("cp gatelet.example.toml gatelet.toml")
        print("Please edit gatelet.toml with your API keys")


@task(pre=[check_config, build])
def setup(c):
    """Full setup and start."""
    up(c)


@task
def run(c, cmd):
    """Run a command in the Gatelet container."""
    docker_compose_exec(c, "gatelet", cmd)


@task
def ps(c):
    """View container status."""
    docker_compose(c, "ps")


@task
def clean(c):
    """Clean up everything (containers, volumes, images)."""
    docker_compose(c, "down -v --rmi local")
