"""
bazel run //finance/reconcile

To auto-add transactions:

--add_to_gnucash=id1,id2,...
"""

# TODO: check that dates are reasonably close in matched transactions

import datetime
import re

import gnucash
import platformdirs
import yaml
from absl import app, flags, logging

from finance import gnucash_util
from finance.reconcile import splitwise_lib

_ADD_TO_GNUCASH = flags.DEFINE_list("add_to_gnucash", None, "External IDs to add to GnuCash")


def print_gnucash_split(split):
    transaction = split.parent
    print("transaction:", transaction.GetDate(), transaction.GetDescription(), "notes=", transaction.GetNotes())
    for s2 in transaction.GetSplitList():
        heading = " "
        if s2.GetGUID().to_string() == split.GetGUID().to_string():
            heading = "→"

        print(heading, gnucash_util.get_split_amount(s2), s2.GetAccount().GetName())
        # s2.GetAccount().GetGUID().to_string()


# TODO: try to automatch
def match_to_account(external_transaction):
    desc = external_transaction.description.strip().lower()
    if any(
        x in desc
        for x in (
            "adli markt",
            "bakerybilla",
            "bäcker",
            "coop",
            "denner",
            "ideas felix",
            "migros",
            "tesco",
            "türke",
            # store by Dharmasala
            "dumbo praha",
        )
    ):
        return ["Expenses", "Groceries"]
    if any(
        x in desc
        for x in (
            # Dharmasala
            "amitaya",
            "cafe",
            "cajovna",
            "kavarna",
            "starbucks",
        )
    ):
        return ["Expenses", "Coffee, tea places"]
    if "uber" in desc:
        return ["Expenses", "Uber"]
    if ("sbb easyride" in desc) or ("operator ict" in desc):
        return ["Expenses", "Public Transportation"]
    if "aws emea" in desc:
        return ["Expenses", "Online Services", "AWS"]
    if "google cloud emea" in desc:
        return ["Expenses", "Online Services", "Google Cloud"]
    if "linode" in desc:
        return ["Expenses", "Online Services", "Linode"]
    if "focusmate" in desc:
        return ["Expenses", "Subscriptions", "Focusmate"]
    if "waking up" in desc:
        return ["Expenses", "Subscriptions", "Waking Up"]
    if "kep - kompetenzzentrum fuer ernaehr" in desc:
        return ["Expenses", "Medical Expenses"]
    if "swica gesundheitsorganisation gener" in desc:
        return ["Expenses", "Insurance", "Health Insurance"]
    if ("surcharge abroad" in desc) or ("balance of service prices" in desc):
        return ["Expenses", "Bank Service Charge", "CHF"]
    return None


def add_external_to_gnucash(external_transaction, book, account_of_interest, external_id):
    logging.info("Creating transaction for txid %s in GnuCash account %s", external_id, account_of_interest.GetName())

    tx = gnucash.Transaction(book)
    tx.BeginEdit()
    dt = datetime.datetime.combine(external_transaction.trade_date, datetime.time(0, 0))
    tx.SetDateEnteredSecs(dt)
    tx.SetDatePostedSecs(dt)
    # TODO: tx.SetDatePostedTS(item.date) ?
    currency = book.get_table().lookup("ISO4217", "CHF")
    tx.SetCurrency(currency)
    tx.SetDescription(external_transaction.description)
    tx.SetNotes(f"Imported at {datetime.datetime.now()}")

    split_in_splitwise = gnucash.Split(book)
    split_in_splitwise.SetParent(tx)
    split_in_splitwise.SetAccount(account_of_interest)
    split_in_splitwise.SetMemo(external_id)

    amount = int(external_transaction.amount * currency.get_fraction())
    print(external_transaction)
    assert amount < 0

    split_in_splitwise.SetValue(gnucash.GncNumeric(amount, currency.get_fraction()))
    split_in_splitwise.SetAmount(gnucash.GncNumeric(amount, currency.get_fraction()))

    target_path = match_to_account(external_transaction)
    if not target_path:
        desc = external_transaction.description.strip()
        logging.warning("Description unmatched: [%s] - adding to Imbalance", desc)
        target_path = ["Imbalance-CHF"]

    imbalance_acct = gnucash_util.account_from_path(book.get_root_account(), target_path)
    s2 = gnucash.Split(book)
    s2.SetParent(tx)
    s2.SetAccount(imbalance_acct)
    s2.SetValue(gnucash.GncNumeric(-amount, currency.get_fraction()))
    s2.SetAmount(gnucash.GncNumeric(-amount, currency.get_fraction()))

    tx.CommitEdit()

    logging.info("Added %s", external_id)
    # TODO: once added, remove from txids to add


def main(_):
    config_dir = platformdirs.user_config_path("ducktape")

    with (config_dir / "config.yaml").open() as f:
        config = yaml.safe_load(f)

    with gnucash_util.gnucash_session(config["reconcile"]["gnucash_book_path"]) as session:
        for reconcile_config in config["reconcile"]["mappings"]:
            if "gnucash_account_path" not in reconcile_config:
                raise ValueError("no gnucash_account_path")

            gnucash_account_path = reconcile_config["gnucash_account_path"]
            print("Reconciling", gnucash_account_path)

            account_of_interest = gnucash_util.account_from_path(session.book.get_root_account(), gnucash_account_path)

            if "splitwise_group_id" in reconcile_config:
                external_transaction_by_external_id = splitwise_lib.load_splitwise_expenses(
                    reconcile_config["splitwise_group_id"]
                )

                prefix = "splitwise"
                id_regex = "([0-9]+)"
            else:
                raise ValueError(f"no way to reconcile: {reconcile_config}")

            # 'start_date' sets date at which mapping starts
            if "start_date" in reconcile_config:
                start_date = datetime.datetime.strptime(reconcile_config["start_date"], "%Y-%m-%d").date()

                external_transaction_by_external_id = {
                    external_id: external_transaction
                    for external_id, external_transaction in external_transaction_by_external_id.items()
                    if external_transaction.trade_date >= start_date
                }

            # External IDs that have been matched.
            matched_external_ids = set()
            gnucash_unmatched_splits = []

            errors = 0
            for split in account_of_interest.GetSplitList():
                transaction_date = split.parent.GetDate().date()
                assert isinstance(transaction_date, datetime.date)
                if transaction_date < start_date:
                    # Skip splits before reconciled date.
                    continue

                memo = split.GetMemo()
                match = re.search(prefix + "=" + id_regex, memo)

                if match:
                    external_id = match.group(1)
                    transaction = external_transaction_by_external_id[external_id]

                    split_amount = gnucash_util.get_split_amount(split)
                    if split_amount != transaction.amount:
                        logging.error(
                            "Error with transaction %s: GnuCash split is %s, external system says %s",
                            external_id,
                            split_amount,
                            transaction.amount,
                        )
                        errors += 1

                    # TODO: also match the dates
                    days = int((transaction_date - transaction.trade_date) / datetime.timedelta(days=1))
                    if abs(days) >= 2:
                        logging.warning("transaction %s has big delta (%s days)", external_id, days)

                    assert external_id not in matched_external_ids, (
                        f"{external_id} matched to 2 transactions in GnuCash"
                    )
                    matched_external_ids.add(external_id)
                    continue

                # transaction is not matched
                gnucash_unmatched_splits.append(split)

                # print("split:", split)
                # split.GetAccount().GetName()
                # >>> t.GetCurrency().get_fullname() --> 'Swiss Franc'

            if errors > 0:
                raise RuntimeError(f"{errors} errors")

            unmatched_ids = set(external_transaction_by_external_id.keys()) - matched_external_ids
            print()
            print("Unmatched in external system:")

            # Sort by descending net
            def get_abs_net(expense_id, external_transaction_by_external_id=external_transaction_by_external_id):
                return abs(external_transaction_by_external_id[expense_id].amount)

            for expense_id in sorted(unmatched_ids, key=get_abs_net, reverse=True):
                external_id = prefix + "=" + expense_id
                expense = external_transaction_by_external_id[expense_id]
                # print(external_transaction_by_external_id[expense_id])
                automatch = match_to_account(expense)
                marker = f" (-> {automatch})" if automatch else ""

                print(f"{external_id} {expense.trade_date.isoformat()} {expense.amount} {expense.description}{marker}")
            print()
            print("Unmatched in GnuCash:")

            # Sort by descending net
            def get_abs_split_net(split):
                return abs(gnucash_util.get_split_amount(split))

            for split in sorted(gnucash_unmatched_splits, key=get_abs_split_net, reverse=True):
                print_gnucash_split(split)

                # add those:

            if _ADD_TO_GNUCASH.value:
                # find _ADD_TO_GNUCASH that's for this system
                prefix_with_equals = prefix + "="
                # TODO: warn if there's some IDs that are not matched in any
                # system
                # TODO: warn on duplicates in _ADD_TO_GNUCASH
                external_ids_for_this_system = {
                    external_id for external_id in _ADD_TO_GNUCASH.value if external_id.startswith(prefix_with_equals)
                }
                # txids are 'system=...'
                # TODO: warn - if txid not in unmatched_ids:
                # TODO: warn -     logging.error("not unmatched: %s", txid)
                # TODO: warn -     continue
                # TODO: make sure IDs are not done multiple times...
                for external_id in external_ids_for_this_system:
                    txid = external_id.removeprefix(prefix_with_equals)
                    if txid not in external_transaction_by_external_id:
                        logging.warning("external system does not have %s", txid)
                        continue
                    external_transaction = external_transaction_by_external_id[txid]
                    add_external_to_gnucash(external_transaction, session.book, account_of_interest, external_id)
        logging.info("Saving the session.")
        session.save()


if __name__ == "__main__":
    app.run(main)
