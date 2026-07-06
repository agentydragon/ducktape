"""ez Share WiFi SD card client (stdlib only).

## Verified API (firmware LZ1801EDPG:1.0.0, tested 2026-04-20)

### Working

`GET /client?command=version`
  `<response><device><version>LZ1801EDPG:1.0.0:...</version></device></response>`

`GET /client?command=devicemac`
  `<response><device><mac>F455951135A2</mac><ssid>...</ssid></device></response>`

`GET /client?command=maxtime`
  `<response><maxtime><time>0</time><name></name></maxtime></response>`

`GET /client?command=GETFILELIST&dir=<url-encoded-path>`
  Path is a Windows path: `A:`, `A:\\DATALOG`, `A:\\DATALOG\\20260418`.
  Returns `<response><files><file type="N">...</file>...</files></response>`.

  Each `<file>` has:
  - `type` attribute: `"3"` = directory, `"4"` = regular file
  - `<name>`: long filename (e.g. `20260419_040533_BRP.edf`); `.` and `..` for self/parent dirs
  - `<createTime>`: unix timestamp
  - `<fileSize>`: bytes (0 for directories)
  - `<imgURL>`: for files — full download URL with 8.3 short filename
    (e.g. `http://192.168.4.1/download?file=DATALOG%5C20260418%5C202604~5.EDF`);
    for dirs — the GETFILELIST URL for that directory (use directly for recursion)

`GET /download?file=<url-encoded-windows-path>`
  Download by 8.3 short path, no `A:` prefix (e.g. `DATALOG\\20260418\\202604~5.EDF`).

### Not implemented on this firmware

- `GET /client?command=Getallfiles` — returns empty `<photos/>`
- `GET /client?command=GetFolders` — returns bare XML declaration
- `GET /pdclient.cgi?command=getcardmode` — redirects to welcome page without login

### Unverified (not tested on this firmware)

- `/pdclient.cgi?command=gconfig` / `sconfig` — card WiFi config read/write
- `/pdclient.cgi?command=Login` — admin login
- `/pdclient.cgi?command=reboot` — reboot card
- `/upload` (POST) — file upload
- `/upload?DEL=` — file delete
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "http://192.168.4.1"


@dataclass(frozen=True)
class FileEntry:
    """A file or directory entry from GETFILELIST."""

    name: str  # long filename, e.g. "20260419_040533_BRP.edf"
    size: int  # bytes (0 for directories)
    create_time: int  # unix timestamp
    img_url: str  # files: download URL; dirs: GETFILELIST URL for recursion
    is_dir: bool

    @classmethod
    def _from_xml(cls, element: ET.Element) -> FileEntry:
        return cls(
            name=element.findtext("name") or "",
            size=int(element.findtext("fileSize") or 0),
            create_time=int(element.findtext("createTime") or 0),
            img_url=element.findtext("imgURL") or "",
            is_dir=element.get("type") == "3",
        )


@dataclass
class DeviceInfo:
    version: str
    mac: str
    ssid: str
    maxtime: str


@dataclass
class CardConfig:
    """Card WiFi AP configuration. Read/write via gconfig/sconfig (unverified)."""

    ssid: str = ""
    password: str = ""
    auth: str = "WPA2"
    channel: str = "6"
    mac: str = ""
    repeater: bool = False


class EZShareClient:
    """Client for ez Share WiFi SD card."""

    def __init__(self, base_url: str = DEFAULT_BASE) -> None:
        self._base = base_url.rstrip("/")

    def _get_bytes(self, path: str, timeout: int = 30) -> bytes:
        with urllib.request.urlopen(f"{self._base}{path}", timeout=timeout) as r:
            data: bytes = r.read()
        return data

    def _get_url_bytes(self, url: str, timeout: int = 30) -> bytes:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data: bytes = r.read()
        return data

    def _parse_xml(self, data: bytes) -> ET.Element:
        # Strip encoding declaration — gb2312 is unsupported by ET's string parser.
        # All filenames in practice are ASCII so this is safe.
        return ET.fromstring(data.replace(b'encoding="gb2312"', b""))

    def get_version(self) -> str:
        root = self._parse_xml(self._get_bytes("/client?command=version"))
        return root.findtext(".//version") or ""

    def get_mac(self) -> str:
        root = self._parse_xml(self._get_bytes("/client?command=devicemac"))
        return root.findtext(".//mac") or ""

    def get_ssid(self) -> str:
        root = self._parse_xml(self._get_bytes("/client?command=devicemac"))
        return root.findtext(".//ssid") or ""

    def get_maxtime(self) -> str:
        root = self._parse_xml(self._get_bytes("/client?command=maxtime"))
        return root.findtext(".//time") or ""

    def get_info(self) -> DeviceInfo:
        return DeviceInfo(
            version=self.get_version(), mac=self.get_mac(), ssid=self.get_ssid(), maxtime=self.get_maxtime()
        )

    def listdir_url(self, url: str) -> list[FileEntry]:
        """List a directory by its GETFILELIST URL. Excludes '.' and '..' entries."""
        root = self._parse_xml(self._get_url_bytes(url))
        return [FileEntry._from_xml(f) for f in root.findall(".//file") if f.findtext("name") not in (".", "..")]

    def listdir(self, path: str = "A:") -> list[FileEntry]:
        """List a directory by Windows path (e.g. 'A:', 'A:\\DATALOG')."""
        url = f"{self._base}/client?command=GETFILELIST&dir={urllib.parse.quote(path)}"
        return self.listdir_url(url)

    def walk(self) -> Iterator[FileEntry]:
        """Yield all file (non-directory) entries on the card via DFS from root."""
        queue = list(self.listdir())
        visited: set[str] = set()
        while queue:
            entry = queue.pop()
            if entry.is_dir:
                if entry.img_url not in visited:
                    visited.add(entry.img_url)
                    queue.extend(self.listdir_url(entry.img_url))
            else:
                yield entry

    def download(self, url: str, dest: Path) -> None:
        """Download a file to dest via a .tmp intermediate (atomic rename on completion)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
            while chunk := r.read(65536):
                f.write(chunk)
        tmp.rename(dest)

    @staticmethod
    def local_path(output_dir: Path, download_url: str) -> Path:
        """Map a download URL to a local path under output_dir.

        Download URLs look like: /download?file=DATALOG%5C20260418%5C202604~1.EDF
        Uses 8.3 short filenames as returned by the card.
        """
        qs = urllib.parse.urlparse(download_url).query
        file_param = urllib.parse.parse_qs(qs).get("file", [""])[0]
        rel = file_param.replace("\\", "/").lstrip("/")
        return output_dir / rel

    # Card config (unverified on this firmware)
    def get_config(self) -> CardConfig:
        root = self._parse_xml(self._get_bytes("/pdclient.cgi?command=gconfig"))
        data = {child.tag: child.text or "" for child in root}
        return CardConfig(
            ssid=data.get("ssid", ""),
            password=data.get("password", ""),
            auth=data.get("auth", "WPA2"),
            channel=data.get("chanel", "6"),
            mac=data.get("mac", ""),
            repeater=data.get("repeater") == "1",
        )

    def reboot(self) -> None:
        self._get_bytes("/pdclient.cgi?command=reboot")

    def login(self, password: str) -> bool:
        data = self._get_bytes(f"/pdclient.cgi?command=Login&password={urllib.parse.quote(password)}")
        return b"<clientLogin>1</clientLogin>" in data
