import shlex
import subprocess
from pathlib import Path

from inventree.label import LabelTemplate
from inventree.part import Part
from inventree.stock import StockItem
from invoke import task

from inventree_utils.beautifier.config import api_from_config

SERVER = "root@agentydragon.com"


def _run(c, cmd):
    """Run a shell command, printing it when desired."""
    print(f"> {subprocess.list2cmdline(cmd)}")
    c.run(shlex.join(cmd))


def _ssh(c, cmd):
    """Run a remote shell command."""
    _run(c, ["ssh", SERVER, cmd])


@task
def logs(c, since="24h", hide_http_logs=True):
    """Show server logs."""
    remote_command = f"docker logs -f --since {since} inventree-server"
    if hide_http_logs:
        remote_command += " | grep -Ev 'KHTML|HTTP'"
    _ssh(c, remote_command)


@task
def restart(c):
    """Restart the server."""
    _ssh(c, "docker restart inventree-server")


@task
def deploy_server_hook(c):
    """Deploy the post-receive hook to the remote repository."""
    _run(c, ["rsync", "server-post-receive", f"{SERVER}:/srv/git/inventree-utils.git/hooks/post-receive"])


here = Path(__file__).parent


def get_api():
    return api_from_config(timeout=1000)


def get_template(api):
    template_name = "Rai stock item - 1x1 SMD snap box"
    templates = LabelTemplate.list(api)
    try:
        return next(t for t in templates if t.name == template_name)
    except StopIteration:
        raise ValueError(f"No label template named '{template_name}' found")


def all_part_ids(api):
    # Fetch all parts
    parts = Part.list(api)
    if not parts:
        print("No parts found")
        return None

    print(f"Found {len(parts)} parts altogether")

    # get list of part IDs whose stock is not in one of the SMD books.
    # SMD books are stock locations with IDs 1, 2, 3, 4.
    stock_items = StockItem.list(api)
    print(f"Found {len(stock_items)} stock items altogether")

    def stock_items_for_part(part_pk):
        return [si for si in stock_items if si.part == part_pk]

    parts = [part for part in parts if not all(si.location in (1, 2, 3, 4) for si in stock_items_for_part(part.pk))]
    print(f"Found {len(parts)} parts not in SMD books")
    return [part.pk for part in parts]  # [:10]


@task
def render_template(c, api, template_pk):
    output_pdf = here / "all_parts_labels.pdf"

    all_parts = False
    if all_parts:  # noqa: SIM108  # Keep if-else for readability with commented part list
        part_ids = all_part_ids(api)
    else:
        part_ids = [
            109,  # 1N5819
            128,  # 1N4007
            122,  # crystal oscillator
            864,  # Hall switch
            76,  # AMS1117-3.3 LDO
            126,  # LM317 SOIC-8 LDO
            4,  # 74HC164 SOIC-14 shift register
            843,  # RN 0402X4 100K
            835,  # AO3400A SOT-23 N-ch E-MOSFET
            841,  # BCM857BS 2x PNP
            118,  # S8050M-D NPN
            ############################
            # OK: all rendered
            123,  # CJ7809 LDO
        ]

    response = api.post(
        url="/label/print/", data={"plugin": "InvenTreeLabelSheet", "template": template_pk, "items": part_ids}
    )
    print(response)

    # {'pk': 160, 'created': '2025-03-16', 'user': 1, 'user_detail': {'pk': 1, 'username': 'root', 'first_name': '', 'last_name': '', 'email': 'agentydragon@gmail.com'}, 'model_type': 'stockitem', 'items': 181, 'complete': True, 'progress': 100, 'output': '/media/label/output/output_p5YHr9R.pdf', 'template': 7, 'plugin': 'inventreelabel'}
    api.downloadFile(url=response["output"], destination=output_pdf, overwrite=True)
    print(f"Generated PDF label sheet with {len(part_ids)} parts -> {output_pdf}")
    # > /media/label/output/output_p5YHr9R.pdf


@task
def deploy_template(c):
    api = get_api()
    template = get_template(api)

    template_path = here / "labels/smd_1x1_stockitem_wip.html"
    template.save(label=template_path)  # Upload the new template file
    print("Template updated")

    render_template(c, api=api, template_pk=template.pk)


@task
def deploy(c):
    deploy_remote = "deploy"
    branch = "main"
    remote_branch = f"{deploy_remote}/{branch}"

    def _diff(stat: bool):
        opts = [
            "git",
            "diff",
            "--find-renames",
            "--color=always",
            "--src-prefix=REMOTE/",
            "--dst-prefix=LOCAL/",
            remote_branch,
            branch,
        ]
        if stat:
            opts.append("--stat")
        _run(c, opts)

    # 1) Sync local knowledge of remote
    _run(c, ["git", "fetch", deploy_remote])
    _diff(stat=True)

    while True:
        match input("\n[d]iff / [p]ush / [q]uit: ").strip().lower():
            case "d":
                _diff(stat=False)
            case "p":
                _run(c, ["git", "push", deploy_remote, branch])
                print("\n\nTo restart InvenTree server, run:\n\n    inv restart")
                break
            case "q":
                print("Aborting.")
                break
            case _:
                print("Invalid choice.")
