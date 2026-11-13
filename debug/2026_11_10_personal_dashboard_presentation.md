# Personal dashboard presentation audit

Reviewed the GNOME captures published from AIQuota PR [#15433](https://github.com/agentydragon/ducktape/pull/15433)
and Plaid Spend PR [#15435](https://github.com/agentydragon/ducktape/pull/15435), against source at
`f46b7bc166`. The images are fixture states from those PRs, not proof of the currently deployed UI.
The shared target policy is [personal dashboard presentation](../docs/personal_dashboard_presentation.md).

## Suggested follow-ups

1. **AIQuota: remove repeated exhausted/paid status.** In the
   [paid credits capture](https://s3.allegedly.works/pr-visuals/assets/def27404a81b75ff1d9621dfd43ec1bab9f1b2cf95e36b449956cdaf208ac227.png),
   the paid balance, a `100% ... exhausted` window and a separate `Included quota exhausted · paid credits available`
   line repeat the same displayed facts. The [exhausted capture](https://s3.allegedly.works/pr-visuals/assets/1a3e545aaee272ae8adc083f10eea7c7d5b947d5e1dffaca7d950d0476a6c6b9.png)
   repeats the exhausted window too. Combine or suppress redundant status when its evidence is visible; retain a useful
   compact signal where its context is hidden. An enabled extra-usage feature and historical monthly spend are distinct:
   positive historical spend alone does not establish that extra usage is enabled now. Preserve reset-pending and stale
   behavior in [`quotaActivity`](../aiquota/shared/presentation.ts) and the Python view model.
2. **AIQuota: reduce grant-detail repetition and wrapping.** The
   [observed account](https://s3.allegedly.works/pr-visuals/assets/0b7de003a4dccf126a5ed410adec081a1be507adf5d46659400ec0ca160cd070.png)
   wraps the Anthropic organization/balance header; its [expanded details](https://s3.allegedly.works/pr-visuals/assets/85ebb289c7eb4ce2c932bf19d710928bd0afd97e3c6ddbfe3ddfdf193c048236.png)
   repeat the entire grant summary to add original amount, precise expiry and scope. Explore grouping or adding only
   incremental details, with readable labels and without losing organization, grant restriction or expiry attribution.
3. **AIQuota: align terminology and formatting.** Claude subscription says `Prepaid balance` while both API providers
   say `Available balance` in the observed capture. Choose one name when the underlying concepts are equivalent, retaining
   distinctions for separate pools. Grant summary dates use a compact form while expanded details switch to a full ISO
   date, time and zone. The same `formatDeadline` handles both Anthropic and OpenAI grants, so the code does not establish
   an Anthropic-specific formatting path; compare the reported live case before attributing it. Align normal and explicit
   exact presentation across GNOME, browser and Python, including the proposed local-zone and precision policy.
4. **Plaid Spend: consolidate sign-in and offline messages.** The
   [sign-in capture](https://s3.allegedly.works/pr-visuals/assets/f753634a9a8981920c31fe8e496aad5285b07915a8343c657c6d45079b513396.png)
   repeats the sign-in requirement in the header, action, attempt status, disconnected explanation and error details.
   Keep the action and useful retry/error information, suppress restatements. In the
   [offline capture](https://s3.allegedly.works/pr-visuals/assets/83bb0f4850b2cd6c1cdc73a23291516250dd544a166de2e593631f6cc8307729.png),
   distinguish no cached data from stale cached data instead of implying stale figures when none are displayed.
5. **Plaid Spend: clarify selected pace versus overall warning.** In the
   [warning capture](https://s3.allegedly.works/pr-visuals/assets/380afaaf67a433b4de3e0c1701c6598d12f9c02a9f34a1825d0210538ffb26f4.png),
   the green `7d pace at or below leash` sits above `Forecast or 30d pace: warning`; clarify that one describes the
   selected window while the other can reflect a different basis. The two unmatched summaries can contain overlapping
   purchases; equal counts and totals do not prove the underlying sets are identical, so do not deduplicate on totals alone.
6. **Both: assess compactness and legibility together.** The captures have substantial vertical padding and dim text for
   many primary readings. Review spacing, wrapping and text contrast in actual GNOME themes and at realistic scaling before
   changing styles; preserve visual separation, readable state indicators and accessible meter descriptions.

## Implementation boundaries

AIQuota already shares JS date formatting between GNOME and web and has a separate Python formatter. Plaid Spend has
separate JS and Python formatting paths. A shared implementation may help where runtimes permit it; otherwise use the
same example vectors and edge-case tests rather than claiming a single cross-language helper exists. These recommendations
do not change application rendering; screenshot fixtures and cross-surface behavior need validation with each follow-up.
