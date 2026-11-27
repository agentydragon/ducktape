---
name: compute-market
description: Maintain an evidence-backed dashboard of affordable compute offers, prioritizing currently orderable OVHcloud servers in Hillsboro and comparing useful alternatives such as Hetzner.
---

# Compute market dashboard

Answer: for up to $200/month of additional spend, what hardware can the owner
actually rent that helps the existing cluster? Maintain the dashboard as a
dated snapshot, not as an implied live inventory. Use the `action_report` skill
to write and inspect it. On a refresh, read the existing dashboard and its
evidence first, then replace stale conclusions with current observations. If a
local report and refresh instructions exist under `cluster/debug/compute-market/`,
read them; do not assume a report is already checked in.

## Start near the existing cluster

The default view is OVHcloud US dedicated servers **available in HIL** at the
price for that exact configuration and location. Inspect current cluster needs
and placement in `cluster/docs/kimsufi_provisioning.md`, `cluster/docs/piraeus.md`
and other relevant sources; distinguish documented configuration from live
state. HIL matters for existing in-zone storage and network paths. Don't assume
an offer outside HIL is a drop-in extension, even if it is cheaper.

Collect both OVH US public catalogs: `eco` (including Kimsufi, So You Start and
Rise) and `baremetalServers`. Query current per-datacenter availability separately.
See `cluster/docs/ovh_hil_availability_queries.md` and run its existing
`bb run //cluster/scripts:ovh_hil_availability -- --max-monthly-usd 201` helper
for an independent plan-level coverage check. The helper shows base plan prices
and default hardware, not a priced, available exact configuration. Apply the
$200 ceiling to results yourself. Reconcile discrepancies between that check and the dashboard, including available plans omitted by the
configuration join; explain any still-unresolved gaps prominently. A successful
query of only one catalog is not a complete OVH refresh.

OVH's public availability endpoint is documented at
<https://developers.us.ovhcloud.com/reference/bare-metal-cloud/dedicated-servers/availability-ordering/>.
Match the plan, memory and disk configuration and HIL to the price, currency,
term and applicable setup charge; a general starting price or stock in another
datacenter does not establish a HIL offer. If a field or the matching proof is
missing, label it unknown and keep that offer out of the verified default list.
Make incomplete source coverage and empty or blocked results visible.

## Compare options

Include OVH's relevant ranges and Hetzner's current dedicated/auction offers;
actively seek other providers that could plausibly improve the decision. Use
cross-provider sources such as Server Hunter and DediScout to discover leads;
check the individual provider's exact configuration and stock before calling a
lead orderable. A provider's general product availability can hide a sold-out
SSD option. Explain location, failure-domain, latency, egress and integration
differences, not just price. A reseller may use another provider's physical
datacenter; verify before counting it as an independent failure domain. Separate
dedicated hardware from shared VMs. Choose a useful short list
and preserve a browsable inventory where possible; avoid claiming a market-wide
optimum without broad enough coverage.

Prioritize SSD/NVMe configurations for this user's current comparison, while
keeping HDD options browsable. Make disk count, per-drive capacity and interface
visible; don't treat an HDD-backed option as an SSD offer. For each exact offer
show CPU model and cores/threads; RAM and ECC if known;
disk count, size and type; public bandwidth, transfer limits and private
network/vRack eligibility and speed when documented; location; recurring price;
setup fee; commitment, tax and currency basis; availability and observation
time. Link directly to the offer/order page and evidence for any separate
availability or price claim. Mark unknowns as unknown. Do not confuse advertised
NIC speed with the contracted bandwidth or assume private networking is included.
Note hardware generation, virtualization/Talos support and usable replicated
capacity when evidence makes them relevant, without inventing benchmarks.

Compare monthly cost and first-month cost separately; a setup fee is not a
monthly fee. Give a USD estimate for non-USD offers with the exchange-rate date,
while retaining the original currency. Keep tax treatment explicit. If a budget
filter is provided, distinguish its recurring-spend limit from any upfront limit.
Explain why each recommendation suits compute, RAM or storage, and its meaningful
tradeoffs. Discounts must identify their duration and renewal price.

## Compare existing machines

Include a current-machine view with actual CPU, RAM, drives, network and vRack
information, plus the latest verifiable recurring invoice charges per machine
including paid options. Use the existing server and billing reader credentials;
match the invoice lines to the server and service period, mark gaps and distinguish
a past invoice from a renewal quote. Reconcile the sum against the invoice when
possible. Keep personal billing figures and raw invoice exports out of version
control: put any dashboard containing them in an ignored local file or outside
the repository. Show a compact current-fleet comparison alongside offer lists,
without requiring a tab switch. Label hardware and invoice observation dates
separately from offer stock.

## Refresh and verify

Record source URLs, query and observation times, coverage, and matching logic so
another run can reproduce the snapshot. Never treat a cached aggregator or a
generic product page as proof of current HIL stock. Availability can change at
any time; direct the reader to recheck before ordering. Do not place orders or
change infrastructure on the authority of a dashboard request.

Deliver the report at the requested location, or use a discoverable local report
under `debug/` when no publication destination is specified. Exercise filters,
inspect the final rendered dashboard and report any missing evidence or blocked
validation. Publishing, scheduling, and purchasing require their own authorization.
