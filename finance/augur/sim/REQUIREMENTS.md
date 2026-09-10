# Financial simulation requirements

These are target capabilities and acceptance criteria, not a claim that every
case is implemented. <../SPEC.md> describes current guarantees and limitations.
Tests and runnable experiments establish coverage; this document does not
prescribe a language, tensor layout, policy registry or implementation sequence.

## Experiment contract

An experiment must be able to compose an initial financial situation, supplied
or sampled market paths, executable spending/allocation rules and explicit
financial/tax assumptions. It must compare distributions and inspect a selected
financial history without going through the house-buying app.

Use one batch policy interface and a Python-controlled outer loop. A policy may
keep path-local state and choose different actions on different paths. Its
observations contain actor-available facts; sampled future information must not
leak into those observations. Policies request economic operations; canonical
settlement owns consequences and existing contracts.

Shared financial mechanics must support standalone library callers and executable
study examples. A fixed-width action grid, mandatory dense full-horizon capture
or an extra native policy implementation is not a requirement. Large populations
are a later performance concern, not a prerequisite for domain/API correctness.

## Financial and accounting acceptance

Each modeled path must carry its actual state forward: cash, holdings, remaining
tax lots/basis, liabilities, contracts and assessed/paid tax facts. Reports derive
from that state and its recorded transactions, not a competing financial model.

- Monetary entries balance, with named sources, destinations and counterparties.
  Opening balances, assets and liabilities must reconcile to opening equity;
  account statements and captured journals must agree where supplied.
- Transfers conserve cash across the relevant accounts. Scripted external
  counterparties must remain explicit; their simplifying assumptions cannot
  grant actors unmodeled credit.
- Trades reconcile units, cash proceeds/cost, disposed/remaining basis and gain
  character. Partial and full disposals preserve exact residual amounts.
- Purchases, loans, interest, principal repayment and payoff reconcile across
  parties. Market revaluation is not cash income.
- Tax accrual, annual liability, payment claims, actual payments and remaining
  liabilities are distinct. Rounding and product-specific treatment are explicit.
- A rejected action leaves that action's books unchanged and stops only its
  rollout. Successful prefix actions survive. There is no same-month retry or
  whole-batch rollback.
- Failure records identify the action or unpaid claims, month and actual closing
  book. A low balance is not itself an unpaid bill; a successful funding sale
  is not ruin. Other paths remain unaffected.

Broader balance-sheet and attribution requirements need independent financial
controls; merely comparing two implementations is insufficient.

## Tax fidelity and scope

The initial household use case requires realistic US federal/California treatment
for its selected products, filing status and residency. Required cases include
ordinary income, capital gains/losses and carryovers, applicable deductions,
investment surtaxes, rental/depreciation/disposal treatment, and payment timing.
A study must identify its actual supported subset and ruleset; omission is not a
zero tax rate.

Year selection, opening year-to-date facts, prior-year liabilities and carryovers
must be supplied or explicitly excluded. Estimated-payment machinery alone does
not establish safe-harbor, withholding, penalty or refund coverage. No missing
input may silently become fabricated history. Independent liability calculations
and integrated spending-funding examples must test the selected coverage.

A new filing status or jurisdiction may require new rules and mechanics.
Separate single-filer returns are not an approximation to a joint return unless
a study explicitly chooses and labels that approximation. Cross-border relocation
requires its own declared FX, residency, tax and contract scope.

## Scenario families

| Family                  | Required behavior to exercise                                                                                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transfers and indexing  | One-off/recurring windows, explicit income character, indexed amounts, observed zero and exact rounding; conserve both sides.                                       |
| Public holdings         | Opening positions, changing marks, multiple lots, mixed holding periods, exact lot selection, partial/full sale and reinvestment.                                   |
| Spending and allocation | Fixed real spending, bounded or stateful cuts/raises, allocation/glide rules, cash reserves and rebalancing as executable policy code.                              |
| Taxes and funding       | Sell to fund non-tax outflows, incur and pay the resulting taxes, preserve year crossing, exemptions and loss/basis consequences.                                   |
| Dated bonds and funds   | Distinguish a tradable position, a hold-to-maturity strategy and a rolling fund; coupon, redemption, valuation and tax assumptions reconcile.                       |
| Managed portfolios      | Household contribution/withdrawal decisions interact with a separately modeled investment service; losses cannot appear without basis/gain consequences.            |
| Housing and mortgages   | Acquiring/disposing of property and financing creates/settles contracts; ongoing claims survive a policy change, and failed purchases do not half-originate a loan. |
| Rental lifecycle        | Rent, occupancy changes, expenses, depreciation, improvements and disposal use consistent ownership and basis.                                                      |
| Private assets          | Voluntary sale opportunities are distinct from compulsory events; forced recovery cannot require a policy opt-in.                                                   |
| Multiple actors         | Common world observations, scoped private state, accountable transfers and explicitly ordered decisions; bookkeeping counterparties need not optimize.              |
| Failure and replay      | Preserve successful prefixes and stopped marks; chunking, reordering and selected replay retain original path identity and equivalent decisions.                    |

These are capabilities, not an instruction to build every family before useful
public-portfolio studies. Product approximations must be labeled and tested;
neither a model's complexity nor a shared implementation certifies its fidelity.

## Results and study evidence

Compact population outcomes and selected financial traces must agree. Preserve
intended spending, attempted requests, actual non-tax consumption, cuts, sale
proceeds, taxes, unpaid claims and termination separately. Report the population
and time basis of each statistic; stopped books are not completed-horizon wealth.

Studies identify policy conventions, path source/model and fit window, product
construction, taxes, fees and uncertainty. Reproductions pin paper-specific rules
and data substitutions; synthetic tests are not published-study reproductions.
Overlapping historical windows are not independent draws. Equal seeds under
different models do not imply paired economic paths.

Runnable examples should exercise real policies and settlement in CI with
generated placeholder financial data when needed. Source-backed larger studies
remain separately runnable. Preserve the financial assertions as interfaces move;
do not strip difficult holdings or claims merely to retire a runner.

## Boundary

The simulator is not a tax filing service, a forecast-quality certificate or a
general optimizer. Experiments own model selection, research comparisons and
objective functions. They need not model strategic lenders, landlords, issuers
or tax authorities to obtain accountable contracts and cashflows. Unsupported
mechanics must remain explicit, not silently approximated by the product UI.
