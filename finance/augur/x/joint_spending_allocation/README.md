# Joint spending and allocation

A `JointHousehold` agent (an `EconomicAgent` subclass) composes the
[bounded annual spending rule](../bounded_spending/README.md) with the
[allocation example's cash-band/glide proposals](../allocation_glide/README.md).
Each selected path is its own `World` tracking one fresh household; the experiment
loops over `world.step()`, which posts the household its statements and dues and then
asks it to act once per month, and
reads what it measures from world state after each step (payments, taxes, assets,
and for replays the month's journal and dispositions). The world keeps no history.
The household reserves due claims plus intended consumption before any purchase and
submits trades → claim payments → consumption. Helpers propose; the world owns exact
lots, taxes, settlement and terminal rejection, and the household's intentions live on
its instance.

```bash
bb run //finance/augur/x/joint_spending_allocation:compare_bin -- \
  --output-dir /tmp/augur-joint-example
```

Use a new output directory. The sweep crosses initial annual spending rates of
4%/8%, fixed-real/20%-cut-5%-raise flexibility, and constant 50/50/annual glide to
70/30. Spending bounds apply around the previous annual amount indexed by CPI;
they are not permanent floor/ceiling lifestyle tiers. `scenario.py:prepare` accepts
caller-supplied paths; the CLI supplies three deterministic stress cases, not
probability samples or a fitted market forecast.

The synthetic situation starts with USD 10,000 cash and 500 units each of
`test-growth` and `test-steady`, priced at USD 100 with USD 80/unit basis acquired
24 months earlier. A separate USD 1,000 nominal bill is due annually. Growth
prices change at months 12/24/36; steady remains 100; CPI rises 5% annually.
Neither security is a bond-fund construction. There are no distributions, fees,
borrowing, housing, relocation or market feedback.

The **synthetic** tax schedule is 20% ordinary/short-term and 10% long-term, with
zero deduction and zero capital-loss ordinary offset. This exercises canonical
assessment and actual payment; it does not demonstrate statutory US/state/foreign,
NIIT/AMT or personalized tax coverage. No horizon-end liquidation/tax settlement
is imposed. This is a composition example, not Guyton–Klinger or personal advice.

`execution-input.json` contains the exact materialized situation, paths and tax
rules. `experiment.json` records the grid and reporting conventions. Each cell
retains its measurements plus selected `[2, 0]` replays with original IDs, the
replays carrying the journal and dispositions the experiment copied month by month.

Measurements keep policy intention separate from attempted consumption requests
and actual payments. A rejected consumption has known zero paid; consumption
prevented by an earlier rejected action has a null request and known zero paid. Post-stop
months are absent. Cuts compare intention with a separately calculated fixed-real
anchor on the same initial wealth. Shortfalls are reported only where actual
consumption is known. Taxes and unpaid claims are separate from consumption.

Terminal assets are cash plus marked public holdings **before unsettled taxes
and unpaid claims**, not liquidated after-tax wealth. Min/median/max describe
completed paths only; stopped paths retain their observed assets and mark month.
No probability estimates, independent-sampling error bars or policy rankings are
inferred from these three cases.

CI runs the documented CLI and verifies both policy dimensions affect the joint
result, tax-free controls, reserved payments, exhaustion and selected replay.
