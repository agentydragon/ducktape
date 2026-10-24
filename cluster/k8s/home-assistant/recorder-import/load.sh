#!/bin/sh
# Runs in the pgloader image as the recorder import Job's main container: copies the rows of the
# snapshot prepare.py took into the schema it had Home Assistant create. `truncate` drops what that
# throwaway Home Assistant wrote; `reset sequences` moves the id sequences past the copied rows.
# pgloader's summary prints each table's rows read and imported, to compare with prepare.py's counts.
set -eu
cat >/work/load <<EOF
LOAD DATABASE FROM sqlite:///work/ha.db INTO ${RECORDER_DB_URL}
WITH data only, truncate, reset sequences, batch rows = 10000
EXCLUDING TABLE NAMES LIKE 'sqlite_stat%';
EOF
exec pgloader /work/load
