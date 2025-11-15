import csv
import datetime
import decimal

from ducktape.finance.reconcile import external_system

_EXPECTED_CSV_COLUMNS = {
    # ?
    "Valuation date",
    # My banking relationship ID (part of IBAN)
    "Banking relationship",
    # Empty.
    "Portfolio",
    # Looks also like part of account's IBAN.
    "Product",
    # Account IBAN (CH...)
    "IBAN",
    # Account currency (CHF)
    "Ccy.",
    # CSV start date - TODO: use this
    "Date from",
    # CSV end date - TODO: use this
    "Date to",
    # Product name ("UBS Personal Account, Credit interest 0%")
    "Description",
    "Trade date",
    "Booking date",
    "Value date",
    "Description 1",
    "Description 2",
    "Description 3",
    "Transaction no.",
    "Exchange rate in the original amount in settlement currency",
    "Individual amount",
    "Debit",
    "Credit",
    "Balance",
}


def load_ubs_csv(path) -> dict[str, external_system.ExternalExpense]:
    # TODO: check IBAN

    # Returns:
    result = {}
    # encoding='utf-8-sig' skips byte order mark
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for line in reader:
            assert set(line.keys()) == _EXPECTED_CSV_COLUMNS, f"unexpected keys: {line.keys()}"
            # Lines that don't have 'Debit' or 'Credit' seem to be just
            # transaction details.
            if not (line["Debit"] or line["Credit"]):
                continue

            transaction_id = str(line["Transaction no."])
            assert transaction_id not in result
            assert (line["Credit"] and not line["Debit"]) or (line["Debit"] and not line["Credit"])
            amount = decimal.Decimal((line["Credit"] or "0").replace("'", "")) - decimal.Decimal(
                (line["Debit"] or "0").replace("'", "")
            )
            trade_date = datetime.datetime.strptime(line["Trade date"], "%d.%m.%Y").date()
            result[transaction_id] = external_system.ExternalExpense(
                amount=amount,
                id=transaction_id,
                # TODO: not sure which date to use...
                trade_date=trade_date,
                description=" ".join([line["Description 1"], line["Description 2"], line["Description 3"]]),
            )
    return result
