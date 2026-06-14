# x/bsc

Experimental: pull personal claims / EOBs / reimbursements from **Blue Shield of
California** via the CMS-mandated Patient Access FHIR API (CARIN BB / SMART-on-FHIR).
Feeds `augur` and finance tooling, reconciling out-of-network reimbursements against
Plaid bank transactions.

**Status (2026-06):** sandbox app registered and airlock OAuth (PKCE) wired, but
blocked — the sandbox member-login step needs a synthetic test member BSC's portal
won't self-service. Emailed `interoperabilitysupp@blueshieldca.com` (2026-05-30); no
reply in 2 weeks, so escalated (2026-06-13) with a follow-up CC'ing the dev-portal
team. Flexpa works
technically but is enterprise-priced; Gmail/PDF EOB parsing is the fallback. No code
in this package yet.

Full registration log, API endpoints, sandbox-credential investigation, and aggregator
survey: <NOTES.md>.
