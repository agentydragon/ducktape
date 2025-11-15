import datetime
import decimal
import http.server
import json
import urllib.parse

from absl import logging
import splitwise
import xdg

from ducktape.finance.reconcile import external_system


def get_splitwise_net(expense, user_id):
    for exp_user in expense.users:
        if exp_user.id == user_id:
            owed = decimal.Decimal(exp_user.getOwedShare())
            paid = decimal.Decimal(exp_user.getPaidShare())
            return paid - owed
    return None


def make_client(splitwise_credentials_path):
    if splitwise_credentials_path.exists():
        with splitwise_credentials_path.open() as f:
            splitwise_credentials = json.load(f)
    else:
        splitwise_credentials = {}

    consumer_key = splitwise_credentials["consumer_key"]
    consumer_secret = splitwise_credentials["consumer_secret"]

    return splitwise.Splitwise(consumer_key, consumer_secret)


def assign_token(client, cache_dir):
    token_path = cache_dir / "splitwise_token.json"
    if token_path.exists():
        with token_path.open() as f:
            access_token = json.load(f)
            logging.info("Access token loaded from %s", access_token)
    else:
        port = 3003
        url, secret = client.getAuthorizeURL()
        print(f"Please go to {url}.")
        params = retrieve_get_params(port=port)
        access_token = client.getAccessToken(params["oauth_token"][0], secret, params["oauth_verifier"][0])
        logging.info("got access token")

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with token_path.open("w") as f:
            json.dump(access_token, f)
            logging.info("Access token saved to %s", access_token)
    client.setAccessToken(access_token)


def retrieve_get_params(port):
    get_params = None

    class RequestHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal get_params
            query = urllib.parse.urlparse(self.path).query
            get_params = urllib.parse.parse_qs(query)
            logging.info("parsed query string: %s", get_params)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Auth handled, you can close this tab.")
            self.server.shutdown()

    server = http.server.ThreadingHTTPServer(("", port), RequestHandler)
    server.serve_forever()
    return get_params


def load_splitwise_expenses(splitwise_group_id) -> dict[str, external_system.ExternalExpense]:
    config_dir = xdg.xdg_config_home() / "gnucash_splitwise_reconciler"
    cache_dir = xdg.xdg_cache_home() / "gnucash_splitwise_reconciler"
    splitwise_credentials_path = config_dir / "splitwise_credentials.json"

    client = make_client(splitwise_credentials_path)

    assign_token(client, cache_dir)

    my_user_id = client.getCurrentUser().getId()
    # group.name
    expenses = {}
    offset = 0
    limit = 100

    while True:
        logging.info("fetching batch of %d items at offset %d", limit, offset)
        batch = client.getExpenses(offset=offset, limit=limit, group_id=splitwise_group_id)
        # exp.repayments[*].fromUser, .toUser
        # can have: exp.deletedAt
        for expense in batch:
            if expense.getDeletedAt():
                continue
            net = get_splitwise_net(expense, my_user_id)
            if net is None:
                # We are not involved.
                continue

            dt = datetime.datetime.strptime(expense.date, "%Y-%m-%dT%H:%M:%SZ").date()
            expenses[str(expense.id)] = external_system.ExternalExpense(
                id=str(expense.id),
                description=((expense.description or "") + (expense.notes or "")),
                amount=net,
                trade_date=dt,
            )
        if len(batch) < limit:
            logging.info("at offset %d, got less than limit %d", offset, limit)
            break
        offset += limit
    logging.info("fetched %d expenses", len(expenses))
    return expenses
