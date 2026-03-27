import contextlib
import decimal
import math

import gnucash


def account_from_path(top_account, account_path, original_path=None):
    if original_path is None:
        original_path = account_path
    account, account_path = account_path[0], account_path[1:]

    account = top_account.lookup_by_name(account)
    if account is None:
        raise Exception("path " + "".join(original_path) + " could not be found")
    if len(account_path) > 0:
        return account_from_path(account, account_path, original_path)
    return account


def gnc_numeric_to_python_decimal(numeric):
    negative = numeric.negative_p()
    sign = 1 if negative else 0
    copy = gnucash.GncNumeric(numeric.num(), numeric.denom())
    result = copy.to_decimal(None)
    if not result:
        raise Exception(f"gnc numeric value {copy.to_string()} can't be converted to decimal")
    digit_tuple = tuple(int(char) for char in str(copy.num()) if char != "-")
    denominator = copy.denom()
    exponent = int(math.log10(denominator))
    assert (10**exponent) == denominator
    return decimal.Decimal((sign, digit_tuple, -exponent))


def get_split_amount(split):
    return gnc_numeric_to_python_decimal(split.GetAmount())


@contextlib.contextmanager
def gnucash_session(path):
    session = gnucash.Session(path)
    try:
        yield session
    finally:
        session.end()
