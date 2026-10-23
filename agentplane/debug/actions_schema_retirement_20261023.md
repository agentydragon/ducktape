# Actions schema retirement

## Final column gate

Read-only verification after the Flux rollout on 2026-10-23: staging Actions is Ready 2/2 and testing
Ready 1/1. Every Actions pod runs `devel-20261023111932-6e7b459`, digest
`sha256:92fdfb7ff5be2963f5d8120af6b9aad24c9343e2dceb212f7aeffd81daa39485`; no old reader pods remain.
Flux selected the published image, committed its tag and reconciled both Deployments.
The image includes the grant reader/API retirement from #11696 and account-token admission from #11700.

Staging Goose migration 00004 is applied. The database retains 4,597 Action requests, one Connection
and three grants; `activation_deadline` still exists. No credential values were read. The last
runtime reader of this column is gone, so migration 00005 can remove it. It removes stored legacy
deadlines without changing grant identities, statuses, current OAuth state or Action history.

## Remaining verification

After migration 00005 deploys, verify its Goose version, column absence, readiness and retained
identity/history counts. Keep published rollback images, but images that still read the dropped
column cannot run against the new schema. Do not edit landed Goose SQL or drop/recreate databases.
External-client reauthorization remains a manual follow-up; these checks do not establish it.
The operator waived restoration testing.
