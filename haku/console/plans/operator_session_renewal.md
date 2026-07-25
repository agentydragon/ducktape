# Renewing the operator session instead of bouncing it hourly

**Status: proposal, undecided.** Written after five separate mechanisms were added in one day to
make an hourly forced logout tolerable. It argues the policy is the thing to revisit, not the
mechanisms. Nothing here is implemented.

## The observation

`OPERATOR_SESSION_MAX_AGE_SECONDS` is one hour, absolute and non-sliding. Everything below exists
only to absorb what happens when it elapses:

| Mechanism                                 | Added because                                                      |
| ----------------------------------------- | ------------------------------------------------------------------ |
| `operator_login_flows` + per-flow binding | Every tab bounces at once and the attempts evicted each other (F1) |
| `return_to` on `/auth/login`              | The bounce dropped the operator at `/` (F2)                        |
| Pre-expiry warning in the rail            | The bounce arrived with no notice (F3)                             |
| Auto-restart once on a stale callback     | A bounce that raced or sat too long dead-ended (F1/F1b)            |
| WS close code 4001                        | The bounce looked like a network outage (F4)                       |
| `operatorLoginRedirectStarted()`          | The bounce painted an error on its way out (F7)                    |

That is a lot of machinery in service of one policy line. The premortem question: in three months,
is the most likely regret "we should have kept the hard deadline" or "we should have stopped paving
around it"?

## The argument for renewal

The deadline's stated purpose (code comment on the session payload) is that an active browser must
not turn the cookie into a sliding authorization that **outlives Authentik reauthentication**.

In practice the hourly bounce does not achieve that in any meaningful sense. Authentik's own SSO
session is longer than an hour, so the bounce almost always completes without the operator seeing a
login form: redirect out, redirect back, new cookie. Authority is not re-derived from a fresh
authentication — it is re-derived from a session Authentik already held. The operator pays the full
cost (tab navigates, iframe state lost, machinery above) for something very close to a no-op.

A silent renewal would do the same check, honestly and cheaply: before the deadline, re-run the
authorization-code flow with `prompt=none`. Authentik either confirms the still-valid session and
issues a fresh identity, or returns `login_required` — at which point the operator genuinely does
need to reauthenticate and the existing bounce is the correct behavior. That satisfies the stated
intent **better** than today's redirect, because a failure now means something.

## Open questions to settle before designing anything

1. **Does Authentik honour `prompt=none` for the `haku-console` client?** Standard OIDC, but the
   provider's configuration (and its session/consent policy) decides. This is the gating question —
   check it first, on the live provider, before writing any code.
2. **Where does the renewal run?** A hidden iframe to `/auth/login?prompt=none` is the usual shape,
   but the console sets `frame-ancestors 'none'` on itself and frames Authentik only for haku-ui's
   in-frame SSO; the CSP and the enrollment-cookie paths both need a look. A same-tab renewal at a
   quiet moment may be simpler and is worth costing out.
3. **What is the renewed session's deadline?** Sliding-until-Authentik-says-no is the point, but an
   outer cap (a working day?) may still be wanted. Decide deliberately rather than by omission.
4. **What does this let us delete?** Plausibly the warning UI and much of the bounce-recovery path.
   The `operator_login_flows` table stays regardless — concurrent tabs still log in independently
   after a real reauthentication, and it is the right shape either way.
5. **Does the security model object?** `haku/docs/security.md` does not itself mandate the hourly
   deadline; the rationale lives in the code comment quoted above. Confirm with the operator that
   re-deriving from Authentik's session (rather than from a fresh credential presentation) is the
   intended bar.

## Next step

Answer (1) against the live Authentik, then either write this up as a design with a decision on
(2)–(5), or record here that renewal was rejected and why — so the next person paving around the
bounce finds the reasoning instead of repeating it.
