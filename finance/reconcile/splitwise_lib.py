import datetime
import decimal
import json
import threading
import urllib.parse

import platformdirs
import splitwise
import uvicorn
from absl import logging
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from finance.reconcile import external_system


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
    completion_event = threading.Event()
    app = FastAPI()

    @app.get("/")
    async def handle_callback(request: Request):
        nonlocal get_params
        query = request.url.query
        get_params = urllib.parse.parse_qs(query)
        logging.info("parsed query string: %s", get_params)
        completion_event.set()
        return PlainTextResponse("Auth handled, you can close this tab.")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    completion_event.wait()
    server.should_exit = True
    server_thread.join()
    return get_params


def load_splitwise_expenses(splitwise_group_id) -> dict[str, external_system.ExternalExpense]:
    config_dir = platformdirs.user_config_path("gnucash_splitwise_reconciler")
    cache_dir = platformdirs.user_cache_path("gnucash_splitwise_reconciler")
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

            dt = datetime.datetime.strptime(expense.date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC).date()
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
