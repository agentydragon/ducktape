-- Run with psql -X -v ON_ERROR_STOP=1 -f backfill_metadata.sql in either journal database.
-- Each generated statement commits independently. Locked rows are left for the next pass.
\set AUTOCOMMIT on
SELECT CASE WHEN current_database() = 'central_sampler' THEN 'sampler_event_block'
            ELSE 'session_event_block' END AS tbl,
       CASE WHEN current_database() = 'central_sampler' THEN 'first_sequence'
            ELSE 'first_cursor' END AS pos
\gset
SET lock_timeout = '500ms';
SET statement_timeout = '5min';

WITH numbered AS (
    SELECT session_id, :"pos" AS pos,
           (row_number() OVER (PARTITION BY session_id ORDER BY :"pos") - 1) / 128 AS batch
    FROM :"tbl" WHERE block_format IS NULL
)
SELECT format(
    'WITH selected AS (
         SELECT session_id, %1$I FROM %2$I
         WHERE session_id = %3$L AND %1$I BETWEEN %4$L AND %5$L AND block_format IS NULL
         ORDER BY %1$I LIMIT 128 FOR UPDATE SKIP LOCKED
     )
     UPDATE %2$I AS b SET block_format = NULL FROM selected AS s
     WHERE b.session_id = s.session_id AND b.%1$I = s.%1$I',
    :'pos', :'tbl', session_id, min(pos), max(pos))
FROM numbered GROUP BY session_id, batch ORDER BY session_id, batch
\gexec
SELECT count(*) AS remaining_unclassified FROM :"tbl" WHERE block_format IS NULL;
