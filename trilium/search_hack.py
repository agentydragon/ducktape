import datetime
import json
import os.path

from absl import app, flags
import openai

# needs: pip install plotly sklearn
from openai.embeddings_utils import cosine_similarity
import pandas as pd
import requests

_ROOT = flags.DEFINE_string("root", "http://localhost:37840", "ETAPI root URL")
_TOKEN = flags.DEFINE_string("token", None, "ETAPI token")
EMBEDDINGS_FILE = "embeddings.json"


def fetch_openai_api_key():
    token, root = _TOKEN.value, _ROOT.value
    headers = {"Authorization": token}

    response = requests.get(f"{root}/etapi/notes", params={"search": "#openaiApiKey"}, headers=headers)
    key_note_id = response.json()["results"][0]["noteId"]
    return requests.get(f"{root}/etapi/notes/{key_note_id}/content", headers=headers).text


def get_embedding(string):
    response = openai.Embedding.create(input=string, model="text-embedding-ada-002")
    return response["data"][0]["embedding"]


def get_cached_embedding(embeddings, string):
    if string in embeddings["strings"]:
        return embeddings["strings"][string]

    embedding = get_embedding(string)
    embeddings["strings"][string] = embedding
    return embedding


def index():
    token = _TOKEN.value
    root = _ROOT.value
    headers = {"Authorization": token}

    openai.api_key = fetch_openai_api_key()

    # embedding -> {
    #   'notes': {'noteid': {'string': ..., 'datetime': 'iso8601 string'}}},
    #   'strings': {'string': embedding},
    # }
    if os.path.exists(EMBEDDINGS_FILE):
        with open(EMBEDDINGS_FILE) as f:
            embeddings = json.load(f)
    else:
        embeddings = {"notes": {}, "strings": {}}

    INDEXED_QUERIES = ["#issue", "#dateNote", "~type.title = Paper", "~type.title = Person", "#hotlist"]
    MAX = 100

    for _query in INDEXED_QUERIES:
        response = requests.get(
            f"{root}/etapi/notes",
            # TODO: I want to index *all* notes but the API doesn't allow empty
            # search...
            # params={'search': '~type.title = Paper'},
            params={"search": INDEXED_QUERIES[0]},
            headers=headers,
        )
        results = response.json()
        # print(results)
        import random

        r = results["results"]
        random.shuffle(r)

        for result in r[:MAX]:
            # TODO: include in embedding string:
            #   - branch path(s)
            #   - relations, relation titles
            #   - labels
            # print(result)

            title = result["title"]
            note_id = result["noteId"]

            response = requests.get(f"{root}/etapi/notes/{note_id}/content", headers=headers)
            assert response.status_code == 200
            note_content = response.text

            content = f"Note title: {title}\n"

            for attribute in result["attributes"]:
                # TODO: combine relations/labels of the same type, like:
                # internalLink: A internalLink: B internalLink: C
                if attribute["type"] == "relation" and attribute["name"] in {
                    "runOnAttributeChange",
                    "runOnNoteCreation",
                    "shareCss",
                    "shareJs",
                    "template",
                }:
                    continue
                if attribute["type"] == "relation":
                    tr = requests.get(f"{root}/etapi/notes/{attribute['value']}", headers=headers)
                    value = tr.json()["title"]
                    content += f"{attribute['name']}: {value}\n"
                    continue
                    # attribute['noteId']
                # TODO: also label
                # iconClass, ~template, label: #relation:...
                if attribute["type"] == "label" and attribute["name"].startswith(("relation:", "label:")):
                    # relation/label definition
                    continue

                if attribute["type"] == "label" and attribute["name"] in {
                    "bookmarked",
                    "iconClass",
                    "template",
                    "readingPriority",
                    "readingPriorityDate",
                    "paper",
                    "issue",
                    "shareAlias",
                    "readingStart",
                    "readingEnd",
                    "notOnArxiv",
                    "tag",  # ?!
                    "expanded",  # ?!
                    "viewType",  # TODO: just drop folders
                    "mapType",
                }:
                    continue

                if attribute["type"] == "label" and attribute["name"] in {"finishedReading", "arxivId", "hotlist"}:
                    content += f"{attribute['name']}: {attribute['value']}\n"
                    continue

                print(attribute)
                raise RuntimeError(f"Unsupported attribute: {attribute!r}")

            if note_content:
                content += "\n" + note_content

            print(content)
            print("----")

            embeddings["notes"][note_id] = {"string": content, "datetime": datetime.datetime.now().isoformat()}

    strings = {note["string"] for note in embeddings["notes"].values()}
    not_embedded = strings - set(embeddings["strings"].keys())

    for string in not_embedded:
        # TODO: can use get_cached_embedding here
        try:
            embedding = get_embedding(string)
            # print(string, embedding[:-10])
            embeddings["strings"][string] = embedding
        except Exception as e:
            print(string, e)

    print(len(results["results"]))

    # TODO: use FAISS for the embedding search

    ## Sort results by ascending priority.
    # def get_result_priority(result):
    #    for attribute in result['attributes']:
    #        if attribute['name'] == 'readingPriority':
    with open(EMBEDDINGS_FILE, "w") as f:
        json.dump(embeddings, f)


def search(query):
    openai.api_key = fetch_openai_api_key()

    with open(EMBEDDINGS_FILE) as f:
        embeddings = json.load(f)

    # TODO: lower level caching
    embedding = get_cached_embedding(embeddings, query)

    df = pd.DataFrame.from_records(
        [
            {"note_id": note_id, "embedding": embeddings["strings"].get(note["string"]), "string": note["string"]}
            for note_id, note in embeddings["notes"].items()
        ]
    )

    # return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))
    import numpy as np

    df["similarities"] = df.embedding.apply(lambda x: cosine_similarity(x or np.zeros_like(embedding), embedding))
    df = df.drop(columns=["embedding"])
    res = df.sort_values("similarities", ascending=False).head(10)
    print(query)
    pd.set_option("display.max_rows", None)
    print(res)

    # TODO: use some sort of db, it sucks when we crash before saving
    with open(EMBEDDINGS_FILE, "w") as f:
        json.dump(embeddings, f)


def main(_):
    index()
    for q in (
        "lizards",
        "python",
        "my friends in prague",
        "furries in san francisco",
        "open issues",
        "pytorch",
        "do next",
        "inspiring",
    ):
        search(q)


if __name__ == "__main__":
    flags.mark_flag_as_required(_TOKEN.name)
    app.run(main)
