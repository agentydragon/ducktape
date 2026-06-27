# Financial anomalies & leaks

- **Financial anomalies & leaks.** Over a recent window of transactions (Plaid), look for
  duplicate charges (same merchant/amount, close dates), **new recurring merchants** (a
  subscription you may not know you have), recurring charges whose amount changed, **fees**
  (overdraft, FX, card — usually killable), and charges unusually large for a merchant's
  history. For a recurring charge with no matching evidence anywhere (no receipt, no signup,
  never used), research the merchant and, if it's a zombie subscription, file a
  `prepared_prompt` to cancel it. One item per finding, evidence in `body` (date, merchant,
  amount, account); **skip expected regulars** (rent, known subscriptions noted in
  `memory/`).
