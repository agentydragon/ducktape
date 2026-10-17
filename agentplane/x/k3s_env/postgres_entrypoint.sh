#!/usr/bin/env bash
# Standalone startup for the published CNPG minimal image in this disposable fixture.
set -euo pipefail

if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  initdb -D "$PGDATA" --username="$POSTGRES_USER" \
    --pwfile=<(printf '%s\n' "$POSTGRES_PASSWORD") \
    --auth-local=trust --auth-host=scram-sha-256 --encoding=UTF8 --locale=C.UTF-8

  # Bootstrap over a private Unix socket before exposing the database to services.
  pg_ctl -D "$PGDATA" -o "-c listen_addresses='' -c unix_socket_directories=/tmp" -w start
  trap 'pg_ctl -D "$PGDATA" -m fast -w stop' EXIT
  psql --host=/tmp --username="$POSTGRES_USER" --dbname=postgres --no-password \
    --set=ON_ERROR_STOP=1 --file=/fixture/bootstrap.sql
  pg_ctl -D "$PGDATA" -m fast -w stop
  trap - EXIT
  printf '\nhost all all all scram-sha-256\n' >>"$PGDATA/pg_hba.conf"
fi

exec postgres -D "$PGDATA" -c "listen_addresses=*" -c unix_socket_directories=/tmp "$@"
