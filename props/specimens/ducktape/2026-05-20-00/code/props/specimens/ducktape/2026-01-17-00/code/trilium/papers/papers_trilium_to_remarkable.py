"""
On first run:
    docker run -v $HOME/.config/rmapi/:/home/app/.config/rmapi/ -it rmapi

This will pair the device to Remarkable API so that we can upload later.
"""

import dataclasses
import re
import subprocess
import sys

import click
import platformdirs
import requests
from absl import app, flags
from tqdm.auto import tqdm

_ETAPI_ROOT_URL = flags.DEFINE_string("etapi_root_url", "http://localhost:37840", "ETAPI root URL")
_TOKEN = flags.DEFINE_string("token", None, "ETAPI token")
_PURGE = flags.DEFINE_bool("purge", False, "Purge RM side?")
SYNCED_DIR_PATH = platformdirs.user_cache_path("papers_trilium_to_remarkable") / "synced_dir"
REMARKABLE_SIDE_PATH = "/papers_trilium_to_remarkable"
FILE_PREFIX = "[f]\t"


def find_attribute_value_in_result(result, attribute_name):
    for attribute in result["attributes"]:
        if attribute["name"] == attribute_name:
            return attribute["value"]
    raise KeyError


@dataclasses.dataclass
class PaperInTrilium:
    arxiv_id: str | None
    note_id: str
    title: str
    pdf_note_id: str | None
    priority: int | None
    finished_reading: bool


def get_result_priority(result):
    try:
        priority = find_attribute_value_in_result(result, attribute_name="readingPriority")
    except KeyError:
        return None  # unprioritized go last

    if priority == "":
        return None

    try:
        priority = int(priority)
        assert priority >= 0
        return priority
    except Exception as e:
        raise ValueError(f"failed to process {result['noteId'] = }") from e


def get_trilium_papers():
    token = _TOKEN.value
    root = _ETAPI_ROOT_URL.value
    headers = {"Authorization": token}
    response = requests.get(f"{root}/etapi/notes", params={"search": "~type.title = Paper"}, headers=headers)
    results = response.json()["results"]
    for result in tqdm(results):
        priority = get_result_priority(result)
        note_id = result["noteId"]
        title = result["title"]

        if title == "Paper template":
            # TODO: skip somehow?
            continue

        children = result["childNoteIds"]
        for child_id in children:
            response = requests.get(f"{root}/etapi/notes/{child_id}", headers={"Authorization": token})
            child_note = response.json()
            if child_note["type"] == "file" and child_note["mime"] == "application/pdf":
                pdf_note_id = child_id
                break
        else:
            pdf_note_id = None
            continue

        try:
            arxiv_id = find_attribute_value_in_result(result, attribute_name="arxivId")
        except KeyError:
            arxiv_id = None

        try:
            finished_reading = find_attribute_value_in_result(result, attribute_name="finishedReading")
            if finished_reading == "true":
                finished_reading = True
            elif finished_reading == "false":
                finished_reading = False
            else:
                raise Exception(f"{finished_reading=}")
        except KeyError:
            finished_reading = False

        yield PaperInTrilium(
            arxiv_id=arxiv_id,
            note_id=note_id,
            title=title,
            pdf_note_id=pdf_note_id,
            priority=priority,
            finished_reading=finished_reading,
        )


def make_args(*args):
    return [
        "docker",
        "run",
        "-v",
        "/home/agentydragon/.config/rmapi/:/home/app/.config/rmapi/",
        "-v",
        "/home/agentydragon/.cache/papers_trilium_to_remarkable/synced_dir:/home/app/synced_dir/",
        "rmapi",
        *args,
    ]


def purge_remarkable_synced_dir():
    for p in get_existing_filenames():
        args = make_args(
            "rm",
            f"{REMARKABLE_SIDE_PATH}/{p}",  # .pdf',
        )
        print(args)
        subprocess.check_call(args)


def get_existing_filenames():
    """Yields filenames uploaded in shared folder sans .pdf extension."""
    existing = subprocess.check_output(make_args("ls", REMARKABLE_SIDE_PATH)).decode("utf-8")
    for line in existing.splitlines():
        assert line.startswith(FILE_PREFIX)
        yield line.removeprefix(FILE_PREFIX)


def build_filename(paper):
    filename = f"{paper.priority:03d} "
    if paper.arxiv_id:
        filename += f"{paper.arxiv_id} "
    filename += paper.title
    filename = filename.replace(":", "_")
    return filename.replace("/", "-").replace("?", "-").replace("(", "_").replace(")", "_")
    # filename = filename.replace(' ', '_')


def sync():
    token = _TOKEN.value
    root = _ETAPI_ROOT_URL.value
    # TODO:
    # Look at papers that are uploaded in synced dir. Parse out their format:
    # priority, arXiv ID, name.
    existing_arxiv_id_to_filename = {}
    for filename in get_existing_filenames():
        regex = r"(?P<priority>\d+) (?P<arxiv_id>\d+\.\d+) (?P<name>.+)"
        # TODO: if no arxiv_id -> save ... how? note ID? let's just skip
        if not (match := re.fullmatch(regex, filename)):
            print(f"no match: {filename}")
            continue
        existing_arxiv_id_to_filename[match.group("arxiv_id")] = filename
    # Look at papers that ought to be uploaded.
    should_exist = {}
    no_arxiv_id = []
    for paper in get_trilium_papers():
        if paper.finished_reading:
            print(f"finished reading: {paper.title}")
            continue

        if paper.priority is None:
            paper.priority = 999

        if not paper.arxiv_id:
            no_arxiv_id.append(paper)
            # print('no arxiv id')
            continue
        # TODO: try to download the pdf here
        if not paper.pdf_note_id:
            print("no PDF")
            continue
        should_exist[paper.arxiv_id] = paper
    print(f"no arxiv id: {len(no_arxiv_id)}")
    # TODO: split apart stuff I finished reading / did not finish reading
    # TODO: Existing papers: make sure their name is correct according to priority.
    # (for now leaving at existing priority)

    # Rename existing ones:
    for arxiv_id, paper in (t := tqdm(should_exist.items())):
        filename = build_filename(paper)

        if arxiv_id not in existing_arxiv_id_to_filename:
            continue
        existing_filename = existing_arxiv_id_to_filename[arxiv_id]

        if existing_filename == filename:
            print("{paper.title} already exists, correctly named")
            continue

        before = REMARKABLE_SIDE_PATH + "/" + existing_filename
        after = REMARKABLE_SIDE_PATH + "/" + filename
        args = make_args("mv", before, after)
        subprocess.check_call(args)
        print("{paper.title} renamed {before} -> {after}")

    SYNCED_DIR_PATH.mkdir(exist_ok=True, parents=True)

    new_arxiv_ids = set(should_exist.keys()) - set(existing_arxiv_id_to_filename.keys())
    # Sort by priority.
    new_arxiv_ids = sorted(
        new_arxiv_ids, key=lambda id: (should_exist[id].priority if should_exist[id].priority is not None else 200)
    )
    # TODO: WTF why is it adding new ones?
    for arxiv_id in (t := tqdm(new_arxiv_ids)):
        paper = should_exist[arxiv_id]
        t.set_description(f"{paper.priority} {paper.title}")
        filename = build_filename(paper)
        path = SYNCED_DIR_PATH / (filename + ".pdf")
        headers = {"Authorization": token}
        response = requests.get(f"{root}/etapi/notes/{paper.pdf_note_id}/content", headers=headers)
        assert response.status_code == 200
        with path.open("wb") as f:
            f.write(response.content)

        args = make_args("put", f"/home/app/synced_dir/{filename}.pdf", REMARKABLE_SIDE_PATH)
        # this seems to work:
        # docker run -v /home/agentydragon/.config/rmapi/:/home/app/.config/rmapi/ -v /home/agentydragon/.cache/papers_trilium_to_remarkable/synced_dir:/home/app/synced_dir/ rmapi put /home/app/synced_dir/2205.12910_NaturalProver:_Grounded_Mathematical_Proof_Generation_with_Language_Models.pdf /papers_trilium_to_remarkable
        sp = subprocess.run(args, capture_output=True, check=False)

        if sp.returncode == 0:
            # ok
            continue
        if sp.returncode == 1 and b"entry already exists" in sp.stderr:
            print("already exists")
            continue
        print(f"{paper.title} {paper.note_id} written to {path}")
        print(args)
        print(f"{sp.returncode = }")
        # Print the standard output of the command
        print(f"{sp.stdout = }")
        # Print the standard error of the command
        print(f"{sp.stderr = }")
        raise RuntimeError("unhandled")


def main(_):
    if _PURGE.value:
        if not click.confirm("Purge RM?"):
            sys.exit(1)

        purge_remarkable_synced_dir()
        sys.exit(0)

    sync()


if __name__ == "__main__":
    flags.mark_flag_as_required(_TOKEN.name)
    app.run(main)
