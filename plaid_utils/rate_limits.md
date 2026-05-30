# Plaid rate limits — what you actually get on the free tier

Source: <https://plaid.com/docs/errors/rate-limit-exceeded/> + Plaid billing docs (fetched 2026-05-29).

## Free-tier shape

- **Sandbox**: always free, no caps that matter. Fake banks (e.g. `ins_109508` "First Platypus Bank").
- **Production Trial plan**: real banks. Hard cap of **10 Items** (i.e. 10 linked financial accounts at the institution-pair level). No documented monthly call quota — you're bounded by per-Item rate limits below, not a quota.
- **Development env**: gone (removed 2024).
- New-team eligibility for the Trial plan is US/Canada only and only for teams created on/after 2026-04-15.

## Per-endpoint limits (Production)

Per-Item limits are what'll bite you when polling your own accounts. Per-client limits are huge — they won't matter at 10 Items.

| Endpoint                      | Per-Item              | Per-client          | What it does                                                 |
| ----------------------------- | --------------------- | ------------------- | ------------------------------------------------------------ |
| `/transactions/sync`          | 50/min                | 2,500/min           | Incremental transactions (use this, not `/transactions/get`) |
| `/transactions/get`           | 30/min                | 20,000/min          | Legacy; sync is the modern replacement                       |
| `/transactions/refresh`       | 2/min, 120/h, 2,880/d | 100/min … 432,000/d | Force a fresh pull from the institution                      |
| `/accounts/get`               | 15/min                | 15,000/min          | Account metadata + cached balances                           |
| `/accounts/balance/get`       | 5/min, 30/h           | 1,200/min           | Real-time balance (uncached; hits the bank)                  |
| `/item/get`                   | 15/min                | 5,000/min           | Item status                                                  |
| `/auth/get`                   | 15/min                | 12,000/min          | Routing + account numbers                                    |
| `/identity/get`               | 15/min                | 2,000/min           | Account owner identity                                       |
| `/institutions/get`           | —                     | 50/min              | List institutions                                            |
| `/link/token/create`          | —                     | 20,000/min          |                                                              |
| `/item/public_token/exchange` | —                     | 12,000/min          |                                                              |

## Sandbox limits

Generally 2–5× more permissive per-Item. Don't worry about them.

## What this means for "polling my own accounts"

The big constraint is **`/accounts/balance/get`**: 30/hour/Item = 720/day/Item. With 10 Items that's 7,200 balance refreshes/day total, ~216k/month — way past anything you'd want.

For transactions, `/transactions/sync` at 50/min/Item is essentially unbounded for personal use. Plaid pushes via webhooks too, so you don't need to poll hard.

Realistic personal budget (10 Items, all in one daily run):

| Activity                                 | Calls/Item/day | Total/day |
| ---------------------------------------- | -------------- | --------- |
| 1× `/accounts/get` per Item              | 1              | 10        |
| 1× `/transactions/sync` (initial + page) | 1–5            | 10–50     |
| 4× balance refresh per Item              | 4              | 40        |

Total: ~60–100 calls/day → ~1,800–3,000/month. Nowhere near any limit.

If you want fresh balances every 15 minutes: 4×24=96 → exceeds `/accounts/balance/get` 30/h cap. Stay at 4×/hour max (every 15 min hits 4/h, fine; every 5 min = 12/h, fine; every 2 min = 30/h, at the cap).

Plaid's docs explicitly say: "using the API as designed should typically not cause a rate limit to be encountered."
