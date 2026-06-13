# x/bsc

Experimental: pull personal claims / EOBs / reimbursements from **Blue Shield of
California** via the CMS-mandated Patient Access FHIR API (CARIN BB / SMART-on-FHIR).
Feeds `augur` and finance tooling, reconciling out-of-network reimbursements against
Plaid bank transactions.

**Status (2026-05):** sandbox app registered and airlock OAuth (PKCE) wired, but
blocked — the sandbox member-login step needs a synthetic test member BSC's portal
won't self-service (emailed `interoperabilitysupp@blueshieldca.com`). Flexpa works
technically but is enterprise-priced; Gmail/PDF EOB parsing is the fallback. No code
in this package yet.

Full registration log, API endpoints, sandbox-credential investigation, and aggregator
survey: <NOTES.md>.
