import base64
import csv
import datetime
import decimal
import hashlib

from absl import logging

from ducktape.finance.reconcile import external_system

# TODO: compute hash from everything

_EXPECTED_CSV_COLUMNS = {
    "Account number",
    "Card number",
    "Account/Cardholder",
    "Purchase date",
    "Booking text",
    "Sector",
    "Amount",
    "Original currency",
    "Rate",
    "Currency",
    "Debit",
    "Credit",
    "Booked",
}

# sep=;
# 3342 5396 0770;;;;Balance brought forward;;;;;CHF;1473.75;;
# 3342 5396 0770;;;28.10.2021;DIRECT DEBIT;;1360.25;CHF;;CHF;;1360.25;29.10.2021
# 3342 5396 0770;510197XXXXXX7156;MICHAEL POKORNY;10.11.2021;Restaurant Markthalle    Zürich       CHE;Restaurants, Bars;65.00;CHF;;CHF;65.00;;11.11.2021
# 3342 5396 0770;510197XXXXXX7156;MICHAEL POKORNY;10.11.2021;Tritt Käse im Viadukt    Zürich       CHE;Grocery stores;44.60;CHF;;CHF;44.60;;11.11.2021


def load_ubs_credit_card_csv(path) -> dict[str, external_system.ExternalExpense]:
    result = {}
    with path.open(encoding="latin1") as f:
        lines = f.readlines()
        assert lines[0] == "sep=;\n"
        for line in csv.DictReader(lines[1:], delimiter=";"):
            assert set(line.keys()) == _EXPECTED_CSV_COLUMNS, f"unexpected keys: {line.keys()}"

            if line["Booking text"] == "Balance brought forward":
                # TODO:
                # {
                #     'Account number': '3342 5396 0770',
                #     'Card number': '',
                #     'Account/Cardholder': '',
                #     'Purchase date': '',
                #     'Booking text': 'Balance brought forward',
                #     'Sector': '',
                #     'Amount': '',
                #     'Original currency': '',
                #     'Rate': '',
                #     'Currency': 'CHF',
                #     'Debit': '1473.75',
                #     'Credit': '',
                #     'Booked': ''
                # }
                continue

            assert line["Account number"] in {"3342 5396 0770", "3073 1430 6738", "3943 8578 0710"}, line[
                "Account number"
            ]
            # logging.info("%s", line)
            purchase_date = datetime.datetime.strptime(line["Purchase date"], "%d.%m.%Y").date()
            # Direct debit:
            # {
            #     'Card number': '',
            #     'Account/Cardholder': '',
            #     'Purchase date': '28.10.2021',
            #     'Booking text': 'DIRECT DEBIT',
            #     'Sector': '',
            #     'Amount': '1360.25',
            #     'Original currency': 'CHF',
            #     'Rate': '',
            #     'Currency': 'CHF',
            #     'Debit': '',
            #     'Credit': '1360.25',
            #     'Booked': '29.10.2021'
            # }

            # Some purchase:
            # TODO: check 'Account number'

            credit, debit = line["Credit"], line["Debit"]
            assert (credit and not debit) or (debit and not credit)
            amount = decimal.Decimal(credit or "0") - decimal.Decimal(debit or "0")

            booking_text = line["Booking text"]
            transaction_hash_text = f"{purchase_date.isoformat()}/{amount}/{booking_text}"
            hash_version = "A"
            transaction_hash = hash_version + base64.b64encode(
                hashlib.sha256(transaction_hash_text.encode("utf-8")).digest()
            )[:16].decode("ascii")
            # logging.info("hash=[%s] %s", transaction_hash_text,
            #              transaction_hash)

            expense = external_system.ExternalExpense(
                amount=amount,
                id=transaction_hash,
                # TODO: not sure which date to use...
                trade_date=purchase_date,
                description=booking_text,
            )
            if transaction_hash in result:
                logging.error("duplicated hash for %s", line)
                raise Exception("duplicated hash: " + transaction_hash)
            result[transaction_hash] = expense
    return result
