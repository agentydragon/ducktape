# FIRE dashboard

A page a household opens on any day. It shows spending read fresh from the household's accounts,
and beside it what that spending means for financial independence: the distribution of outcomes
under named economy models, with sampling error. It is an application over Augur
(<../augur/README.md>). Augur stays a library and gains only the generic capabilities listed under
[What Augur provides](#what-augur-provides).

## Questions it answers

- **Is recent spending sustainable?** Is the spending of the last N months sustainable, and by how
  much is it over or under?
- **How healthy is the plan this year?** The probability of staying on the current spending tier
  at 10, 20 and 30 years, of stepping down to a cheaper fallback tier, and of exhausting the
  fallback.
- **What does a purchase cost?**
  - It is answered in runway days and in the change to sustainable annual spend.
  - A one-off of $1k–$10k moves a ruin probability by roughly 0.01–0.1 percentage points, which is
    below what a Monte Carlo run resolves. So a probability delta is never the headline.

**Every figure says which model produced it and why to trust that model.** Nothing is hidden. Each
figure carries:

- its model card: standing, status, and known biases with their direction;
- the declared scope of the book: what was left out, and which way the omission biases the answer;
- the plausibility-gate verdicts;
- its standard error.

**Not goals:**

- static category budgets;
- forecasting next month's bills inside Augur;
- presentation inside Augur.

## Boundary

| Layer                                                                  | Owner          | Where                                          |
| ---------------------------------------------------------------------- | -------------- | ---------------------------------------------- |
| Account data: balances, holdings, transactions                         | Plaid sync     | <../plaid/>; the data stays with the household |
| Transaction classification                                             | the classifier | <../budget/>                                   |
| Cash-flow forecast: recurring costs, dated changes such as a lease end | the dashboard  | the app                                        |
| Simulation, taxes, strategies, gates, model cards                      | Augur's facade | <../augur/>                                    |
| Household configuration, pages carrying its numbers, deployment        | the household  | a private downstream repository                |

Generic application code may live in this repository. Anything carrying a household's numbers or
identity does not.

## What Augur provides

1. **One versioned door.** It returns results carrying an estimate, its standard error, the model
   card, the declared scope, the assumption ledger and the gate verdicts.
2. **Any start month,** with year-to-date tax facts, not only January.
3. **Sustainable spend as a curve over targets.** For example, the spend at which P(still on tier at
   year 30) is 80, 90 or 95%. It is found by bisection on common random numbers.
4. **The cost of a one-off,** as a paired difference on common random numbers. It is reported in
   runway days and sustainable-spend dollars, with its standard error.
5. **Precomputed bundles** that answer a marginal question in under a second without running a
   simulation. Each is stamped with the book's as-of date and refused once stale.
6. **A "what changed" diff between two results:** the book, the spend basis, the model identity, and
   the contract version.
7. **Caller-supplied cash flows,** so the dashboard's own forecast enters the simulation.

## Stages

1. **Notebook.** A notebook over the facade plots the result data: fans, per-path tiers, and one
   drilled-down path.
2. **Daily report.** A scheduled job refreshes the account data, classifies it, re-reads the latest
   bundle and publishes a static page.
3. **Interactive page.** The report plus a purchase box, answered from bundles.

## Open questions

- **Host:** a tab in an existing internal UI, or its own app.
- **Sustainability reading:** which probability, at which horizon, counts as sustainable. The
  sustainable-spend curve defers the choice to the reader, but the headline needs one.
- **Rebuild policy.** A full grid is hours of CPU time, so bundles are rebuilt on triggers rather
  than daily. The triggers:
  - liquid wealth or the spend basis moving past a threshold;
  - a model, card or contract change;
  - a bundle's age.
- **Spending variance:** fat-tailed categories are either modelled as a distribution or bracketed
  with fixed levels.
