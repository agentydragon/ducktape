# budget

Plaid transaction classifier answering "what does my monthly spending actually
look like, and how much can I afford to change it?" Reads live data from the
Plaid mirror DB, classifies transactions into named buckets, groups related
buckets into families (e.g. medical: charges + insurance reimbursements) that
show inflows and outflows side by side instead of force-netting, and surfaces
lumpy one-offs separately.

Consumers: augur's API server serves it at `/api/budget/*` for the frontend's
`Budget` tab (<../augur/api/server.py>), and the Beancount exporter
(<../beancount_export/>) renders the same classification as a ledger.

## Architecture

| Module              | What it does                                                                                                                                                                               |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema.py`         | `BudgetConfig` Pydantic: bucket taxonomy (kinds: expense / inflow / transfer / income) + the condition DSL and rule kinds, loaded from the `budget:` block of the augur config YAML        |
| `sql_read_model.py` | Reads Plaid transactions from Postgres, compiles the rules to a first-match-wins SQL CASE, classifies (direction-gated) + applies overrides, aggregates monthly totals, returns drilldowns |
| `service.py`        | Orchestrates request windows, database session reuse, CSV export, and wire types                                                                                                           |
| `wire.py`           | HTTP wire schemas (drive augur's frontend Zod codegen via its `export_schema`)                                                                                                             |
| `csv_export.py`     | CSV exports: the bucket-by-month summary (plus planning columns when adjustments are sent) and one bucket's transactions                                                                   |

## What lives in ducktape vs gaffer-private

**ducktape (public):** The framework — this package (schemas, the condition DSL
and rule kinds, the SQL read model), plus augur's `/api/budget/*` endpoints and
frontend tab. No rule _content_ ships in the framework; every rule lives in the
deployment's config.

**gaffer-private (private):** The actual `budget:` config block in the
deployment's `Config` YAML, listing the user's specific merchants (medical
providers, therapist, landlord), Plaid account IDs to include, and bucket
overrides. Augur and the Beancount exporter both load it from that YAML; the
framework knows nothing about it until the YAML is read.

## Adding a `budget:` section to your augur config

```yaml
budget:
  source:
    # ENV var that holds the postgres URL for the plaid mirror DB. In-cluster, the
    # augur API Deployment's namespace needs its own ESO copy of the secret
    # `plaid-mcp-db-readonly` (cluster/cdk8s/plaid_mcp/db.py); mount its
    # DATABASE_URL key as this env var.
    database_url_env: AUGUR_PLAID_DATABASE_URL
    # Optional: subset of Plaid mirror accounts.account_id values to include.
    # Empty = every account the connection sees.
    plaid_account_ids: []

  buckets:
    - { id: rent, label: Rent, kind: expense, direction: outflow }
    - { id: utilities, label: Utilities, kind: expense, direction: outflow }
    - { id: groceries, label: Groceries, kind: expense, direction: outflow }
    - { id: doordash, label: DoorDash, kind: expense, direction: outflow }
    - { id: restaurants_in_person, label: Restaurants (in person), kind: expense, direction: outflow }
    - { id: ai_subscription, label: AI subscriptions, kind: expense, direction: outflow }
    - { id: transportation, label: Transportation, kind: expense, direction: outflow }
    - { id: insurance, label: Health insurance, kind: expense, direction: outflow }
    - { id: taxes, label: Taxes, kind: expense, direction: outflow }
    - { id: travel, label: Travel, kind: expense, direction: outflow }
    - { id: general_merchandise, label: General merchandise, kind: expense, direction: outflow }
    - { id: electronics, label: Electronics, kind: expense, direction: outflow }
    - { id: entertainment, label: Entertainment, kind: expense, direction: outflow }
    - { id: personal_care, label: Personal care, kind: expense, direction: outflow }
    - { id: bank_fees, label: Bank fees, kind: expense, direction: outflow }
    - { id: government, label: Government, kind: expense, direction: outflow }
    # Related buckets share a `family`; the UI renders them in one panel showing inflow and
    # outflow side by side (no auto-netting -- reimbursement timing is too lumpy to net safely).
    - { id: medical_reimbursement, label: Insurance reimbursements, kind: inflow, direction: inflow, family: medical }
    - { id: prescriptions, label: Prescriptions, kind: expense, direction: outflow, family: medical }
    - { id: therapy, label: Therapy, kind: expense, direction: outflow, family: medical }
    - { id: medical_other, label: Other medical, kind: expense, direction: outflow, family: medical }
    # Transfers are split by direction so each bucket stays single-sided.
    - { id: transfers_out, label: Transfers out (internal), kind: transfer, direction: outflow }
    - { id: transfers_in, label: Transfers in (internal), kind: transfer, direction: inflow }
    - { id: income, label: Income, kind: income, direction: inflow }
    - { id: other, label: Uncategorized, kind: expense, direction: outflow }
    - { id: other_in, label: Uncategorized inflow, kind: inflow, direction: inflow }

  default_outflow_bucket_id: other
  default_inflow_bucket_id: other_in

  # The full ordered rule list. First match wins; a rule fires only on the leg whose
  # sign matches its target bucket's direction. Put specific rules before broad PFC
  # fallbacks. Flat kinds (merchant_substring / name_substring / pfc) plus the richer
  # `match` kind (a condition tree: amount / account / regex / all_of / any_of / not).
  rules:
    # Example shapes (replace patterns with your actual merchants):
    # - { kind: merchant_substring, pattern: <your landlord>, bucket_id: rent }
    # - { kind: pfc, primary: FOOD_AND_DRINK, detailed: FOOD_AND_DRINK_GROCERIES, bucket_id: groceries }
    # - { kind: pfc, primary: TRANSFER_OUT, bucket_id: transfers_out }   # PFC fallback
    # - kind: match                                                       # rich/compound rule
    #   bucket_id: <some_bucket>
    #   condition: { kind: all_of, conditions: [
    #     { kind: name_substring, pattern: <descriptor> },
    #     { kind: amount, min: 5000 } ] }

  lumpy_threshold_usd: 500
```

## Running the dev server against your live cluster data

The dev script lives in **gaffer-private**, not ducktape, because the real
budget config and trained model artifacts live there. From the gaffer-private
repo root (inside the nix devshell):

```bash
./gaffer_augur/dev_against_prod.sh
```

The script:

1. Reads creds from Secret `plaid-mcp/plaid-mcp-db-readonly`
2. Port-forwards `svc/plaid-mcp-db-rw` → `localhost:15432`
3. Exports `AUGUR_PLAID_DATABASE_URL` pointing at the forward
4. Runs `bazelisk run //gaffer_augur:backend_dev` (which has the trained PE
   artifacts and the private `config.yaml` already wired as data deps)

When the active `Config` has no `budget:` section, `/api/budget/*` returns
400 and the Budget tab shows that error.
