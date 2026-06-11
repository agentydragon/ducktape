#!/bin/bash
set -e

echo "Waiting for PostgreSQL to be ready..."
until pg_isready -h db -U postgres; do
  sleep 1
done

echo "Running database migrations..."
alembic upgrade head

echo "Starting Gatelet server..."
exec "$@"
