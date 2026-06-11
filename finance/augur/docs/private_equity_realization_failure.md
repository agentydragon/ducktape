# Private-Company Equity Realization Failure

This note is a forecasting memo, not an implementation plan. It asks:

> Conditional on the holder being alive and the ordinary financial/legal world still
> functioning well enough that a net-worth graph is meaningful, what is the chance
> that a nominally large private-company equity stake becomes tiny or unusable?

The motivating event is broader than "the shares go to zero." It includes any path
where the current paper value does not become usable wealth for the holder.

For the public/private evidence boundary used by this memo, see
`augur/README.md`. This public note may include sourced public issuer-specific facts,
including OpenAI facts, because those can be part of the forecasting reference class.
Private account data, share counts, security numbers, screenshots, security
documents, and holder-specific eligibility facts belong downstream.

## Forecast Target

Use an absolute catastrophic threshold plus relative impairment thresholds.

```text
cash_realized_by_horizon + conservative_terminal_usable_value < absolute_floor_usd
```

For a multimillion-dollar private-company stake, `$100,000` is a useful example
floor: below that, the outcome is planning-equivalent to a wipeout even if the legal
claim is not literally worth zero.

Also ask:

```text
usable_value < 1% of starting paper value
usable_value < 10% of starting paper value
usable_value < 25% of starting paper value
```

`usable_value` means realized cash plus value that could realistically be converted
to cash, accounting for transfer restrictions, tender eligibility, sale caps, private
secondary haircuts, legal status, and capital-structure effects. It is not the same
as the company-reported mark.

## Generic Realization Model

Model realization risk as a company-value path plus a separate holder-realization
path. A private-company mark can be economically real and still fail to become usable
wealth on the planning horizon.

```text
usable_value(t)
  = eligible_fraction(t)
  * liquidity_fraction_available(t)
  * company_value_per_share(t)
  * realization_haircut(t)
  * holder_claim_survival(t)
```

This is a forecasting decomposition, not an Augur implementation contract.

- `company_value_per_share`: issuer outcome, dilution, capital stack, distress, and
  upside/downside repricing.
- `eligible_fraction`: fraction of the position that can legally and practically
  participate in a sale at each date.
- `liquidity_fraction_available`: tender, IPO, acquisition, structured secondary, or
  negotiated-sale capacity available to the holder by date.
- `realization_haircut`: private-secondary discount, sale price versus paper mark,
  fees, withholding, and execution friction.
- `holder_claim_survival`: legal/title/admin/plan-status hazards that can impair the
  holder's claim even if the issuer survives.

The decomposition should be lot-aware only where lot state affects the forecast.
For example, if a small tail of shares is still inside a holding-period or tender
eligibility window, represent that as `eligible_fraction(t)` for short-horizon cash
planning. Do not let lot-level bookkeeping dominate a 5- or 10-year forecast when the
holder has no plausible need to sell those lots before they season.

Use public law and generic securities references here, and keep holder-specific
eligibility evidence downstream. The SEC's Rule 144 investor guidance is a generic
public reference for restricted and control securities; it describes the different
resale conditions that can apply by issuer reporting status, affiliate status, and
holding period. <https://www.sec.gov/about/reports-publications/investorpubsrule144>

### Evidence Shape

Private downstream notes should condition this model with source-backed facts, but
the generic model should not need personal account data. A useful evidence ledger
has these fields:

- `source`: document, platform record, public article, legal filing, or user
  statement;
- `fact`: the smallest factual claim that affects the forecast;
- `applies_to`: issuer, security class, holder category, lot, or tender process;
- `model_effect`: which term in the decomposition moves and in what direction;
- `confidence`: whether the source is direct, inferred, stale, ambiguous, or only
  partially applicable.

For private documents, downstream ledgers should cite the specific document title
used for each fact, not only a folder, archive, platform, or screenshot batch.

Keep private source ledgers downstream when the facts reveal account state, exact
holdings, security identifiers, platform-only values, personal eligibility, or
nonpublic tender/plan terms.

## Conditional Framing

Exclude:

- the holder dies before the horizon;
- society, the dollar system, or normal property-rights enforcement breaks so badly
  that ordinary net-worth accounting is no longer the relevant frame;
- tax policy or inflation alone changes the meaning of dollars while the stake is
  otherwise monetized.

Include:

- a broad technology or financing crash where the world is still functioning;
- issuer-specific regulatory, legal, governance, or technical failure;
- a private-market liquidity freeze;
- capital-structure actions that leave common holders with little;
- holder-specific loss of practical ability to use or sell the shares.

This conditional framing matters because the question is not "what bad things can
happen to the world?" It is "in the world where the rest of the financial plan still
matters, how often does this concentrated private stake fail to count?"

## Decomposition

Think in overlapping mechanisms. The total probability is not the sum of these rows,
because several can happen together.

The probabilities are judgment estimates, not directly measured frequencies. The
source column gives the reference anchors that should inform the estimate.

| Mechanism                         | What has to be true                                                                                                 | Typical severity                                                                                | Source anchors                                                                                                                                                                                                                                                                                                                                                                                                         |            First-cut 10-year probability for a top-tier private issuer |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------: |
| Business collapse                 | The issuer fails, sells for little, or survives only after value-destructive restructuring.                         | Can produce `<$100k`.                                                                           | Convoy and Olive show multi-billion-dollar private issuers can shut down or wind down. [CB Insights Convoy](https://www.cbinsights.com/research/convoy-logistics-unicorn-shuts-down/), [CB Insights Olive profile](https://www.cbinsights.com/company/crosschx/financials)                                                                                                                                             |                                                              `1% - 4%` |
| Severe repricing without collapse | The company survives but the fair value falls by `90%+`.                                                            | Usually not `<$100k` by itself for a very large stake, but can combine with dilution/liquidity. | PitchBook data summarized by Axios says more than a quarter of VC-backed unicorns had lost unicorn status; Stanford work on unicorn valuations finds reported post-money marks often exceed fair value. [Axios/PitchBook](https://www.axios.com/2026/02/13/vc-unicorn-companies), [Stanford GSB](https://www.gsb.stanford.edu/faculty-research/publications/squaring-venture-capital-valuations-reality)               |                                                             `3% - 12%` |
| Capital-structure impairment      | New capital, preferences, debt, anti-dilution, carve-outs, or recap mechanics push common far down the waterfall.   | Can produce `<$100k` if distressed.                                                             | Cooley describes down-round terms and anti-dilution effects; Stanford valuation work documents investor protections such as IPO guarantees, down-IPO vetoes, and seniority. [Cooley](https://www.cooleygo.com/down-round-financings/), [Stanford GSB](https://www.gsb.stanford.edu/faculty-research/publications/squaring-venture-capital-valuations-reality)                                                          |                                                            `0.5% - 3%` |
| Liquidity failure                 | The stake has paper/economic value but cannot be sold in meaningful size by the horizon.                            | Can make usable value near-zero on a planning date even if terminal value is positive.          | Stanford Venture Capital Initiative data summarized by SaaStr reports IPOs became a much smaller share of unicorn exits; WEF/Stanford note companies staying private longer and traditional exits slowing. [SaaStr/Stanford VCI](https://www.saastr.com/only-11-of-unicorn-exits-are-ipos-now-down-from/), [WEF/Stanford](https://www.weforum.org/stories/2026/05/venture-capital-innovation-investment-startups/)     | `5% - 20%` for "no material liquidity"; lower for permanent near-zero. |
| Holder-status exclusion           | Tenders or sale programs exist but exclude former employees, a security class, or small/large holders.              | Usually partial, but can dominate usable value.                                                 | Direct public base-rate data is sparse; source through private-company transfer restrictions and liquidity-market structure, plus holder-rights documents when available.                                                                                                                                                                                                                                              |                                                              `1% - 8%` |
| Legal/title/plan failure          | Vesting, issuance, transfer, repurchase, or plan mechanics unexpectedly invalidate or neuter the holder's claim.    | Can produce `<$100k`, but rare if records are clean.                                            | Direct public base-rate data is sparse; treat as a legal-operational tail whose estimate should be driven by the holder's plan documents, issuer records, and transfer-agent/custodian records.                                                                                                                                                                                                                        |                                                            `0.1% - 1%` |
| Governance or mission override    | Control structure, settlement, public-interest duty, or regulator-driven action changes economics for some holders. | Low base rate but hard to rule out in unusual issuers.                                          | Source through issuer governance documents, regulatory settlements, charter duties, and public legal actions; generic public base-rate data is weak.                                                                                                                                                                                                                                                                   |                                                            `0.2% - 2%` |
| Fraud/accounting/security shock   | The stated business or assets are much weaker than believed, or a catastrophic internal failure appears.            | Can produce collapse or severe recap.                                                           | FTX and Theranos show high-valuation private companies can collapse from fraud/control failures in an otherwise functioning world. [CNBC FTX](https://www.cnbc.com/2022/11/11/ftx-ceo-sam-bankman-fried-lost-billionaire-status-filed-bankruptcy.html), [Forbes Theranos](https://www.forbes.com/sites/roomykhan/2017/02/17/theranos-9-billion-evaporatedstanford-expert-whose-questions-ignited-the-unicorn-trouble/) |                                                            `0.5% - 3%` |

For the combined event `usable_value < $100,000`, a defensible first-cut 10-year
range for a top-tier, repeatedly financed private issuer is a judgment estimate
anchored to the reference classes above:

```text
low:       0.5% - 1%
central:  2% - 5%
high:     8% - 15%
```

The low end requires strong evidence of clean title, repeated broad secondary
liquidity, deep strategic backing, modest preference/debt risk, and no obvious
regulatory or governance overhang. The high end applies if the company is extremely
capital hungry, has opaque financing terms, unusual control/governance, restricted
holder liquidity, or a business model exposed to abrupt regulatory or technical
discontinuity.

For `usable_value < 10% of starting paper value`, use a substantially higher first
cut, again as a judgment estimate:

```text
central:  5% - 15% over 10 years
wide:     2% - 30%
```

For "no material usable liquidity by horizon," do not confuse this with zero value.
Anchor this to the slower IPO/exit environment and increasing importance of secondary
markets noted by Stanford VCI/SaaStr and WEF/Stanford:

```text
5-year:   5% - 20%
10-year:  3% - 15%
```

The 10-year number can be lower than the 5-year number because time creates more
chances for a tender, IPO, acquisition, structured secondary, or other exit.

## Reference Classes

Use a hierarchy. No single reference class is adequate.

### All Venture-Backed Startups

This is a loose upper-bound class. Most venture-backed startups never become large
liquid holdings, but that is not the right base rate after a company has already
become a very large private issuer. Use this class only to remember that startup
equity can truly go to zero.

### Late-Stage Private Companies And Unicorns

This is the main cautionary class. It captures:

- private marks that later prove stale or inflated;
- companies staying private for long periods;
- down rounds and structured rounds;
- exits below the last private valuation;
- companies with large funding histories that still shut down.

Reference anchors:

- PitchBook data summarized by Axios suggested that more than a quarter of VC-backed
  unicorns had lost unicorn status, with many valuations probably meaningfully below
  book marks: <https://www.axios.com/2026/02/13/vc-unicorn-companies>
- Stanford Venture Capital Initiative data summarized by SaaStr reported a sharp
  decline in IPO share of unicorn exits, with secondaries becoming more important:
  <https://www.saastr.com/only-11-of-unicorn-exits-are-ipos-now-down-from/>
- Stanford GSB research on unicorn share-class terms found reported post-money
  valuations can exceed fair value when investor protections are included:
  <https://www.gsb.stanford.edu/faculty-research/publications/squaring-venture-capital-valuations-reality>

### Large Private-Company Collapses

This is a tail-risk anchor rather than a base rate. The point is not that every large
private issuer resembles these examples; it is that a multi-billion-dollar private
mark can still become worthless or nearly worthless in an intact world.

Examples:

- Convoy was last valued around `$3.8B`, raised more than `$800M`, and shut down:
  <https://www.cbinsights.com/research/convoy-logistics-unicorn-shuts-down/>
- Olive was valued around `$4B` before being sold off in pieces / wound down:
  <https://www.cbinsights.com/company/crosschx/financials>
- FTX raised at a roughly `$32B` valuation and entered bankruptcy months later:
  <https://www.cnbc.com/2022/11/11/ftx-ceo-sam-bankman-fried-lost-billionaire-status-filed-bankruptcy.html>
- Theranos reached a roughly `$9B` valuation before the equity value evaporated:
  <https://www.forbes.com/sites/roomykhan/2017/02/17/theranos-9-billion-evaporatedstanford-expert-whose-questions-ignited-the-unicorn-trouble/>
- WeWork reached a roughly `$47B` private valuation and later filed for bankruptcy;
  this is a public-market and debt-restructuring case by the end, but it is a useful
  anchor for extreme private-mark collapse:
  <https://techcrunch.com/2023/11/06/wework-once-worth-47-billion-files-for-bankruptcy/>

### Top-Tier Mega-Private Issuers

This class pushes the estimate down. Repeated large financings, repeated secondary
liquidity, strategic partners, major customers, and high public visibility all reduce
the probability of a true wipeout. They do not eliminate the risk that the mark is
not usable cash, or that common holders are treated worse than enterprise-value
headlines imply.

This is the best class for the lower bound. A `<0.1%` 10-year catastrophic-realization
probability would require treating the stake as almost public-large-cap-like, which
is usually not warranted for concentrated private common stock.

### Employee Or Former-Employee Common Stock

This class pushes the estimate up relative to company-level enterprise value.
Common stock can be impaired even when the company survives, because preferred
investors, lenders, strategic investors, and active employees can have different
rights or bargaining leverage.

Cooley's down-round financing guide is a concise reference for the mechanism:
anti-dilution and harsher financing terms can magnify dilution to common holders.
<https://www.cooleygo.com/down-round-financings/>

Stanford GSB's unicorn valuation work is another reference for why company-level marks
are not the same thing as common-holder value: it explicitly discusses share-class
protections such as IPO return guarantees, down-IPO vetoes, and seniority.
<https://www.gsb.stanford.edu/faculty-research/publications/squaring-venture-capital-valuations-reality>

## Public OpenAI Anchors

When applying this forecast to an OpenAI holding, Ducktape may use public OpenAI
facts as issuer-specific evidence. These facts are not private account data.

Public anchors that push catastrophic-realization risk down:

- Reuters reported that current and former employees sold roughly `$6.6B` of shares
  in a secondary transaction valuing OpenAI at about `$500B`, which is strong evidence
  that large-scale liquidity has existed for employee holders:
  <https://www.investing.com/news/stock-market-news/openai-hits-500-billion-valuation-after-share-sale-source-says-4267760>
- OpenAI's public Microsoft partnership update says Microsoft held an approximately
  `$135B`, roughly `27%` investment in OpenAI Group PBC after recapitalization, which
  is evidence of major strategic validation and capital-market support:
  <https://openai.com/index/next-chapter-of-microsoft-openai-partnership/>

Public anchors that keep the tail risk nontrivial:

- OpenAI's public structure note says the for-profit entity would transition to a
  Public Benefit Corporation and that the nonprofit would retain control, which makes
  governance and mission/control mechanics relevant to shareholder economics:
  <https://openai.com/index/evolving-our-structure/>
- Reuters reported very large planned compute spending through 2030 alongside rapidly
  growing revenue, which is bullish on scale but also relevant to financing need,
  dilution, and dependence on continued capital access:
  <https://www.investing.com/news/stock-market-news/openai-sees-compute-spend-of-around-600-billion-by-2030-cnbc-reports-4517341>

Private anchors that must stay downstream include exact holdings, account screenshots,
security numbers, Shareworks-only values, holder eligibility, and nonpublic tender or
plan-document terms.

## Bounding Logic

### Why The Lower Bound Is Not Zero

Even very large private companies can fail or recapitalize badly. The existence of
past secondary liquidity proves demand existed at that time; it does not prove future
liquidity, future eligibility, clean future terms, or absence of holder-specific
restrictions.

A practical lower bound for a large private common-stock stake is around `0.5% - 1%`
over 10 years for `usable_value < $100,000`. Going below that requires unusually strong
evidence:

- multiple independent liquidity paths;
- clear current and former-holder eligibility;
- clean title and custody records;
- low debt/preference overhang;
- durable cash generation or capital access;
- no special governance/regulatory risk;
- no obvious single-point technical or reputation failure mode.

### Why The Upper Bound Is Not 50%

The upper bound should be far below generic startup failure rates if the issuer is
already one of the most valuable private companies, has repeated external validation,
and has demonstrated large secondary liquidity. A broad `50%` catastrophic wipeout
forecast would usually imply evidence of fraud, imminent financing failure, legal
invalidity, or an existential business model break.

For a high-quality mega-private issuer, `8% - 15%` over 10 years is already an
aggressive high case for `usable_value < $100,000`. For severe impairment, not
catastrophic failure, wider ranges such as `10% - 30%` can be reasonable.

## Evidence That Should Move The Forecast

Move down if:

- there have been repeated large tenders including the holder's status class;
- sale caps allow a meaningful fraction of the position to be sold;
- independent secondary bids are close to the paper mark;
- the company has durable positive cashflow or unusually secure financing;
- security documents clearly protect the holder's class;
- the capital stack is simple and not debt-heavy;
- there is a credible near-term public listing or broad liquidity event.

Move up if:

- financing needs are enormous relative to current revenue/cashflow;
- recent liquidity is narrow, delayed, cancelled, or excludes former holders;
- transfer restrictions are strict and discretionary;
- security documents include surprising repurchase or conversion rights;
- preferred/debt terms are opaque or senior-heavy;
- the issuer has unusual governance that could prioritize non-shareholder objectives;
- regulatory, safety, national-security, or litigation risk could force a restructuring;
- internal controls, accounting, or custody are hard to verify.

## Forecasting Checklist

Before assigning a number, answer these:

1. What is the exact security class, and where is that documented?
2. Is the holder current employee, former employee, founder, investor, contractor, or
   another category?
3. What fraction of the position has actually been sellable in recent tenders?
4. Were tenders company-run, investor-run, platform-run, or informal secondary bids?
5. Were former holders eligible on equal terms?
6. Are there transfer restrictions, company consent rights, ROFR, or lockups?
7. What is known about liquidation preferences, senior securities, debt, or strategic
   financing?
8. What happens to this security class in a down round, acquisition, recap, or PBC-like
   restructuring?
9. Is the latest mark based on an actual transaction involving this class, a 409A-style
   valuation, a platform mark, or an internal display?
10. What is the fastest path to realizing enough cash that the catastrophic event
    becomes impossible?

## Source Discipline

Every factual claim in this memo should be tied to one of:

- an inline public source link;
- the holder's private security documents, tender documents, issuer notices, or
  custodian/transfer-agent records;
- an explicit "direct public base-rate data is sparse" note.

The probability ranges above are not themselves sourced facts. They are forecasting
judgments that combine the cited reference classes with case-specific evidence. If a
later version has issuer-specific evidence, record the evidence next to the estimate
and say which direction it moved the prior.
