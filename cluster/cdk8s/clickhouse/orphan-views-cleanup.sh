#!/bin/sh
set -eu
: "${CLICKHOUSE_REPLICA_HOSTS:?at least one replica is required}"

# All connections are replica-specific: materialized view definitions are local.
# Check both hosts before changing either; check again immediately before each drop.
guard="SELECT throwIf(count() != 0, 'unexpected legacy Langfuse dependency or view definition') FROM system.tables WHERE database = 'langfuse' AND (name IN ('traces_null','traces_30d_amt','traces_7d_amt') OR (name = 'traces_30d_amt_mv' AND (engine != 'MaterializedView' OR toString(uuid) != '19f87464-f293-49c6-b60b-c4d852e53676' OR hex(SHA256(create_table_query)) != 'A37AA0BA6C2813E531354BFD8E691FDA1E703A0B09BAAA97AC1F3F894AEFEED0')) OR (name = 'traces_7d_amt_mv' AND (engine != 'MaterializedView' OR toString(uuid) != '857a6070-6bf2-45e4-876c-105e5392862a' OR hex(SHA256(create_table_query)) != 'BB278161BBCDD474313D82005F6C1035C1A00C87B0CD202880D679BE23EBC3DB')))"

for host in $CLICKHOUSE_REPLICA_HOSTS; do
  printf 'Checking %s\n' "$host"
  clickhouse-client --host "$host" --connect_timeout=10 --receive_timeout=60 --query "$guard"
done

for host in $CLICKHOUSE_REPLICA_HOSTS; do
  printf 'Cleaning %s\n' "$host"
  clickhouse-client --host "$host" --connect_timeout=10 --receive_timeout=60 --query "$guard"
  clickhouse-client --host "$host" --connect_timeout=10 --receive_timeout=60 --query 'DROP VIEW IF EXISTS langfuse.traces_30d_amt_mv'
  clickhouse-client --host "$host" --connect_timeout=10 --receive_timeout=60 --query "$guard"
  clickhouse-client --host "$host" --connect_timeout=10 --receive_timeout=60 --query 'DROP VIEW IF EXISTS langfuse.traces_7d_amt_mv'
  clickhouse-client --host "$host" --connect_timeout=10 --receive_timeout=60 --query "$guard"
done
