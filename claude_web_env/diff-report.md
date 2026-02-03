# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count     | %        |
| -------------------- | --------- | -------- |
| Identical            | 120,364   | 11.7%    |
| Excluded (expected)  | 906,628   | 88.1%    |
| **Real differences** | **2,132** | **0.2%** |
| Total                | 1,029,124 |          |

## Real Differences

### Only in live (1,742)

**claude-config** (1)

- `/root/.claude/stop-hook-git-check.sh`

**docs** (9)

- `/usr/share/doc/age`
- `/usr/share/doc/age/changelog.Debian.gz`
- `/usr/share/doc/age/copyright`
- `/usr/share/doc/python3/_static`
- `/usr/share/doc/python3/_static/doctools.js`
- `/usr/share/doc/python3/_static/language_data.js`
- `/usr/share/doc/python3/_static/searchtools.js`
- `/usr/share/doc/python3/_static/sphinx_highlight.js`
- `/usr/share/doc/python3/index.html`

**home** (1)

- `/home/claude/.ssh/commit_signing_key.pub`

**root-home** (1)

- `/root/.bazelrc`

**root-local** (1727)

- `/root/.local/share/pnpm`
- `/root/.local/share/pnpm/store`
- `/root/.local/share/pnpm/store/v3`
- `/root/.local/share/virtualenv`
- `/root/.local/share/virtualenv/py_info`
- `/root/.local/share/virtualenv/py_info/2`
- `/root/.local/share/virtualenv/py_info/2/8544b3b66cebbf8d4bc96652e6245fc8bdfdf722f63bdaa4790110c167815f0e.json`
- `/root/.local/share/virtualenv/py_info/2/8544b3b66cebbf8d4bc96652e6245fc8bdfdf722f63bdaa4790110c167815f0e.lock`
- `/root/.local/share/virtualenv/py_info/2/9c258799489e4ad9c78adba3665f3739d83d708c1c6eccff6bcb9776521c4384.json`
- `/root/.local/share/virtualenv/py_info/2/9c258799489e4ad9c78adba3665f3739d83d708c1c6eccff6bcb9776521c4384.lock`
- `/root/.local/share/virtualenv/wheel`
- `/root/.local/share/virtualenv/wheel/3.11`
- `/root/.local/share/virtualenv/wheel/3.11/embed`
- `/root/.local/share/virtualenv/wheel/3.11/embed/3`
- `/root/.local/share/virtualenv/wheel/3.11/embed/3/pip.json`
- `/root/.local/share/virtualenv/wheel/3.11/embed/3/setuptools.json`
- `/root/.local/share/virtualenv/wheel/3.11/image`
- `/root/.local/share/virtualenv/wheel/3.11/image/1`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any.lock`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/INSTALLER`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/METADATA`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/RECORD`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/WHEEL`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/entry_points.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/AUTHORS.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/LICENSE.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/cachecontrol`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/cachecontrol/LICENSE.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/certifi`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/certifi/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/dependency_groups`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/dependency_groups/LICENSE.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/distlib`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/distlib/LICENSE.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/distro`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/distro/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/idna`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/idna/LICENSE.md`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/msgpack`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/msgpack/COPYING`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/packaging`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/packaging/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/packaging/LICENSE.APACHE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/packaging/LICENSE.BSD`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/pkg_resources`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/pkg_resources/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/platformdirs`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/platformdirs/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/pygments`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/pygments/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/pyproject_hooks`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/pyproject_hooks/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/requests`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/requests/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/resolvelib`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/resolvelib/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/rich`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/rich/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/tomli`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/tomli/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/tomli_w`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/tomli_w/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/truststore`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/truststore/LICENSE`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/urllib3`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.dist-info/licenses/src/pip/_vendor/urllib3/LICENSE.txt`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip-25.3.virtualenv`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/__init__.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/__main__.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/__pip-runner__.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/__init__.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/build_env.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cache.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/__init__.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/autocompletion.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/base_command.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/cmdoptions.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/command_context.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/index_command.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/main.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/main_parser.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/parser.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/progress_bars.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/req_command.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/spinners.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/cli/status_codes.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/commands`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/commands/__init__.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/commands/cache.py`
- `/root/.local/share/virtualenv/wheel/3.11/image/1/CopyPipInstall/pip-25.3-py3-none-any/pip/_internal/commands/check.py`
- _...and 1627 more_

**system-binaries** (2)

- `/usr/bin/age`
- `/usr/bin/age-keygen`

**usr-share** (1)

- `/usr/share/lintian/overrides/age`

### Only in built (61)

**docs** (42)

- `/usr/share/doc-base/python3.12-doc.python3.12-api`
- `/usr/share/doc-base/python3.12-doc.python3.12-ext`
- `/usr/share/doc-base/python3.12-doc.python3.12-lib`
- `/usr/share/doc-base/python3.12-doc.python3.12-new`
- `/usr/share/doc-base/python3.12-doc.python3.12-ref`
- `/usr/share/doc-base/python3.12-doc.python3.12-tut`
- `/usr/share/doc/python3-doc`
- `/usr/share/doc/python3-doc/changelog.Debian.gz`
- `/usr/share/doc/python3-doc/copyright`
- `/usr/share/doc/python3.12-doc`
- `/usr/share/doc/python3.12-doc/changelog.Debian.gz`
- `/usr/share/doc/python3.12-doc/copyright`
- `/usr/share/doc/python3.12/html`
- `/usr/share/doc/python3.12/html/_downloads`
- `/usr/share/doc/python3.12/html/_downloads/6dc1f3f4f0e6ca13cb42ddf4d6cbc8af`
- `/usr/share/doc/python3.12/html/_images`
- `/usr/share/doc/python3.12/html/_sources`
- `/usr/share/doc/python3.12/html/_sources/c-api`
- `/usr/share/doc/python3.12/html/_sources/distributing`
- `/usr/share/doc/python3.12/html/_sources/extending`
- `/usr/share/doc/python3.12/html/_sources/faq`
- `/usr/share/doc/python3.12/html/_sources/howto`
- `/usr/share/doc/python3.12/html/_sources/installing`
- `/usr/share/doc/python3.12/html/_sources/library`
- `/usr/share/doc/python3.12/html/_sources/reference`
- `/usr/share/doc/python3.12/html/_sources/tutorial`
- `/usr/share/doc/python3.12/html/_sources/using`
- `/usr/share/doc/python3.12/html/_sources/whatsnew`
- `/usr/share/doc/python3.12/html/_static`
- `/usr/share/doc/python3.12/html/_static/jquery.js`
- `/usr/share/doc/python3.12/html/_static/underscore.js`
- `/usr/share/doc/python3.12/html/c-api`
- `/usr/share/doc/python3.12/html/distributing`
- `/usr/share/doc/python3.12/html/extending`
- `/usr/share/doc/python3.12/html/faq`
- `/usr/share/doc/python3.12/html/howto`
- `/usr/share/doc/python3.12/html/installing`
- `/usr/share/doc/python3.12/html/library`
- `/usr/share/doc/python3.12/html/reference`
- `/usr/share/doc/python3.12/html/tutorial`
- `/usr/share/doc/python3.12/html/using`
- `/usr/share/doc/python3.12/html/whatsnew`

**etc** (7)

- `/etc/apt/preferences.d/glib-pin`
- `/etc/apt/preferences.d/libreoffice-pin`
- `/etc/apt/preferences.d/live-archive-pin`
- `/etc/apt/preferences.d/php84-pin`
- `/etc/apt/preferences.d/postgresql-pin`
- `/etc/apt/preferences.d/ppa-pin`
- `/etc/apt/preferences.d/snapshot-pin`

**usr-share** (12)

- `/usr/share/devhelp`
- `/usr/share/devhelp/books`
- `/usr/share/info/python3.12`
- `/usr/share/info/python3.12.info.gz`
- `/usr/share/info/python3.12/hashlib-blake2-tree.png`
- `/usr/share/info/python3.12/kde_example.png`
- `/usr/share/info/python3.12/logging_flow.png`
- `/usr/share/info/python3.12/pathlib-inheritance.png`
- `/usr/share/info/python3.12/tk_msg.png`
- `/usr/share/info/python3.12/turtle-star.png`
- `/usr/share/info/python3.12/win_installer.png`
- `/usr/share/lintian/overrides/python3.12-doc`

### Content changed (hash differs) (329)

**docs** (28)

- `/usr/share/doc/binutils-common/changelog.Debian.gz` — size 1928->1963
- `/usr/share/doc/fonts-opensymbol/changelog.Debian.gz` — size 36166->42367
- `/usr/share/doc/fonts-opensymbol/copyright` — size 21830->20199
- `/usr/share/doc/gdb/changelog.Debian.gz` — size 4004->4529
- `/usr/share/doc/gnupg-utils/changelog.Debian.gz` — size 6751->6657
- `/usr/share/doc/gpgconf/changelog.Debian.gz` — size 6753->6659
- `/usr/share/doc/gpgv/changelog.Debian.gz` — size 6751->6657
- `/usr/share/doc/libavahi-common-data/changelog.Debian.gz` — size 7839->7616
- `/usr/share/doc/libctf-nobfd0/changelog.Debian.gz` — size 1931->1965
- `/usr/share/doc/libcups2t64/changelog.Debian.gz` — size 10464->10249
- `/usr/share/doc/libglib2.0-0t64/changelog.Debian.gz` — size 31039->30498
- `/usr/share/doc/libglib2.0-data/changelog.Debian.gz` — size 31039->30498
- `/usr/share/doc/libheif-plugin-aomdec/changelog.Debian.gz` — size 3052->2889
- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 1963->1501
- `/usr/share/doc/libpython3.12-minimal/changelog.Debian.gz` — size 10631->10516
- `/usr/share/doc/libsframe1/changelog.Debian.gz` — size 1928->1962
- `/usr/share/doc/libsodium23/changelog.Debian.gz` — size 749->530
- `/usr/share/doc/libsubid4/changelog.Debian.gz` — size 6467->6641
- `/usr/share/doc/libsystemd-shared/changelog.Debian.gz` — size 83113->82886
- `/usr/share/doc/libsystemd0/changelog.Debian.gz` — size 83110->82883
- `/usr/share/doc/libtasn1-6/changelog.Debian.gz` — size 2272->2158
- `/usr/share/doc/libudev1/changelog.Debian.gz` — size 83112->82888
- `/usr/share/doc/libxml2/changelog.Debian.gz` — size 5122->4762
- `/usr/share/doc/libxslt1.1/changelog.Debian.gz` — size 1911->1731
- `/usr/share/doc/linux-libc-dev/changelog.Debian.gz` — size 532128->531692
- `/usr/share/doc/login/changelog.Debian.gz` — size 6466->6641
- `/usr/share/doc/passwd/changelog.Debian.gz` — size 6467->6641
- `/usr/share/doc/systemd-dev/changelog.Debian.gz` — size 83111->82884

**etc** (2)

- `/etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-noble.sources` — size 1756->1755
- `/etc/pam.d/login` — size 4118->3974

**home** (1)

- `/home/claude/.gitconfig` — size 251->226

**other** (70)

- `/usr/lib/gnupg/dirmngr_ldap` — size 44016->44016
- `/usr/lib/gnupg/gpg-check-pattern` — size 59984->59984
- `/usr/lib/gnupg/gpg-pair-tool` — size 68528->68528
- `/usr/lib/gnupg/gpg-preset-passphrase` — size 35472->35472
- `/usr/lib/gnupg/gpg-protect-tool` — size 89168->89168
- `/usr/lib/gnupg/keyboxd` — size 167288->167288
- `/usr/lib/kernel/install.d/90-loaderentry.install` — size 7123->7123
- `/usr/lib/systemd/system-generators/systemd-cryptsetup-generator` — size 35464->35464
- `/usr/lib/systemd/system-generators/systemd-debug-generator` — size 19024->19024
- `/usr/lib/systemd/system-generators/systemd-fstab-generator` — size 56072->56072
- `/usr/lib/systemd/system-generators/systemd-getty-generator` — size 22984->22984
- `/usr/lib/systemd/system-generators/systemd-gpt-auto-generator` — size 35400->35400
- `/usr/lib/systemd/system-generators/systemd-hibernate-resume-generator` — size 27232->27232
- `/usr/lib/systemd/system-generators/systemd-integritysetup-generator` — size 23024->23024
- `/usr/lib/systemd/system-generators/systemd-rc-local-generator` — size 14712->14712
- `/usr/lib/systemd/system-generators/systemd-run-generator` — size 18992->18992
- `/usr/lib/systemd/system-generators/systemd-system-update-generator` — size 14712->14712
- `/usr/lib/systemd/system-generators/systemd-sysv-generator` — size 35272->35272
- `/usr/lib/systemd/system-generators/systemd-veritysetup-generator` — size 31448->31448
- `/usr/lib/systemd/systemd` — size 100816->100816
- `/usr/lib/systemd/systemd-backlight` — size 35272->35272
- `/usr/lib/systemd/systemd-battery-check` — size 18888->18888
- `/usr/lib/systemd/systemd-binfmt` — size 22984->22984
- `/usr/lib/systemd/systemd-boot-check-no-failures` — size 14792->14792
- `/usr/lib/systemd/systemd-bsod` — size 22984->22984
- `/usr/lib/systemd/systemd-cgroups-agent` — size 14712->14712
- `/usr/lib/systemd/systemd-executor` — size 137792->137792
- `/usr/lib/systemd/systemd-fsck` — size 27008->27008
- `/usr/lib/systemd/systemd-fsckd` — size 27080->27080
- `/usr/lib/systemd/systemd-growfs` — size 22984->22984
- `/usr/lib/systemd/systemd-hibernate-resume` — size 23104->23104
- `/usr/lib/systemd/systemd-hostnamed` — size 47584->47584
- `/usr/lib/systemd/systemd-initctl` — size 22984->22984
- `/usr/lib/systemd/systemd-integritysetup` — size 23056->23056
- `/usr/lib/systemd/systemd-journald` — size 193664->193664
- `/usr/lib/systemd/systemd-localed` — size 55776->55776
- `/usr/lib/systemd/systemd-logind` — size 285264->285264
- `/usr/lib/systemd/systemd-makefs` — size 14712->14712
- `/usr/lib/systemd/systemd-measure` — size 47880->47880
- `/usr/lib/systemd/systemd-modules-load` — size 19008->19008
- `/usr/lib/systemd/systemd-network-generator` — size 43464->43464
- `/usr/lib/systemd/systemd-networkd` — size 1669864->1669864
- `/usr/lib/systemd/systemd-networkd-wait-online` — size 39536->39536
- `/usr/lib/systemd/systemd-pcrextend` — size 27272->27272
- `/usr/lib/systemd/systemd-pcrlock` — size 137928->137928
- `/usr/lib/systemd/systemd-pstore` — size 23008->23008
- `/usr/lib/systemd/systemd-quotacheck` — size 14792->14792
- `/usr/lib/systemd/systemd-random-seed` — size 27080->27080
- `/usr/lib/systemd/systemd-remount-fs` — size 18888->18888
- `/usr/lib/systemd/systemd-reply-password` — size 14712->14712
- `/usr/lib/systemd/systemd-rfkill` — size 22984->22984
- `/usr/lib/systemd/systemd-shutdown` — size 55760->55760
- `/usr/lib/systemd/systemd-sleep` — size 47560->47560
- `/usr/lib/systemd/systemd-socket-proxyd` — size 31184->31184
- `/usr/lib/systemd/systemd-storagetm` — size 51808->51808
- `/usr/lib/systemd/systemd-sulogin-shell` — size 18808->18808
- `/usr/lib/systemd/systemd-sysctl` — size 23104->23104
- `/usr/lib/systemd/systemd-sysupdate` — size 117592->117592
- `/usr/lib/systemd/systemd-time-wait-sync` — size 18808->18808
- `/usr/lib/systemd/systemd-timedated` — size 43488->43488
- `/usr/lib/systemd/systemd-tpm2-setup` — size 27200->27200
- `/usr/lib/systemd/systemd-update-done` — size 14712->14712
- `/usr/lib/systemd/systemd-update-utmp` — size 22984->22984
- `/usr/lib/systemd/systemd-user-runtime-dir` — size 22904->22904
- `/usr/lib/systemd/systemd-user-sessions` — size 14712->14712
- `/usr/lib/systemd/systemd-veritysetup` — size 27304->27304
- `/usr/lib/systemd/systemd-volatile-root` — size 22904->22904
- `/usr/lib/systemd/systemd-xdg-autostart-condition` — size 14712->14712
- `/usr/lib/systemd/user-environment-generators/30-systemd-environment-d-generator` — size 14712->14712
- `/usr/lib/systemd/user-generators/systemd-xdg-autostart-generator` — size 35272->35272

**python-libs** (52)

- `/usr/lib/python3.12/_sysconfigdata__x86_64-linux-gnu.py` — size 49505->49505
- `/usr/lib/python3.12/config-3.12-x86_64-linux-gnu/Makefile` — size 178567->178567
- `/usr/lib/python3.12/config-3.12-x86_64-linux-gnu/libpython3.12-pic.a` — size 13332658->13332658
- `/usr/lib/python3.12/config-3.12-x86_64-linux-gnu/libpython3.12.a` — size 14670634->14667786
- `/usr/lib/python3.12/config-3.12-x86_64-linux-gnu/python.o` — size 4912->4912
- `/usr/lib/python3.12/http/client.py` — size 57965->57228
- `/usr/lib/python3.12/lib-dynload/_asyncio.cpython-312-x86_64-linux-gnu.so` — size 82184->82184
- `/usr/lib/python3.12/lib-dynload/_bz2.cpython-312-x86_64-linux-gnu.so` — size 32112->32112
- `/usr/lib/python3.12/lib-dynload/_codecs_cn.cpython-312-x86_64-linux-gnu.so` — size 154184->154184
- `/usr/lib/python3.12/lib-dynload/_codecs_hk.cpython-312-x86_64-linux-gnu.so` — size 162408->162408
- `/usr/lib/python3.12/lib-dynload/_codecs_iso2022.cpython-312-x86_64-linux-gnu.so` — size 39528->39528
- `/usr/lib/python3.12/lib-dynload/_codecs_jp.cpython-312-x86_64-linux-gnu.so` — size 277064->277064
- `/usr/lib/python3.12/lib-dynload/_codecs_kr.cpython-312-x86_64-linux-gnu.so` — size 141896->141896
- `/usr/lib/python3.12/lib-dynload/_codecs_tw.cpython-312-x86_64-linux-gnu.so` — size 117320->117320
- `/usr/lib/python3.12/lib-dynload/_contextvars.cpython-312-x86_64-linux-gnu.so` — size 14560->14560
- `/usr/lib/python3.12/lib-dynload/_crypt.cpython-312-x86_64-linux-gnu.so` — size 14744->14744
- `/usr/lib/python3.12/lib-dynload/_ctypes.cpython-312-x86_64-linux-gnu.so` — size 137968->137968
- `/usr/lib/python3.12/lib-dynload/_ctypes_test.cpython-312-x86_64-linux-gnu.so` — size 31352->31352
- `/usr/lib/python3.12/lib-dynload/_curses.cpython-312-x86_64-linux-gnu.so` — size 128584->128584
- `/usr/lib/python3.12/lib-dynload/_curses_panel.cpython-312-x86_64-linux-gnu.so` — size 24168->24168
- `/usr/lib/python3.12/lib-dynload/_dbm.cpython-312-x86_64-linux-gnu.so` — size 23880->23880
- `/usr/lib/python3.12/lib-dynload/_decimal.cpython-312-x86_64-linux-gnu.so` — size 372904->372904
- `/usr/lib/python3.12/lib-dynload/_hashlib.cpython-312-x86_64-linux-gnu.so` — size 64368->64368
- `/usr/lib/python3.12/lib-dynload/_json.cpython-312-x86_64-linux-gnu.so` — size 48952->48952
- `/usr/lib/python3.12/lib-dynload/_lsprof.cpython-312-x86_64-linux-gnu.so` — size 32032->32032
- `/usr/lib/python3.12/lib-dynload/_lzma.cpython-312-x86_64-linux-gnu.so` — size 49256->49256
- `/usr/lib/python3.12/lib-dynload/_multibytecodec.cpython-312-x86_64-linux-gnu.so` — size 54664->54664
- `/usr/lib/python3.12/lib-dynload/_multiprocessing.cpython-312-x86_64-linux-gnu.so` — size 24280->24280
- `/usr/lib/python3.12/lib-dynload/_posixshmem.cpython-312-x86_64-linux-gnu.so` — size 15080->15080
- `/usr/lib/python3.12/lib-dynload/_queue.cpython-312-x86_64-linux-gnu.so` — size 23816->23816
- `/usr/lib/python3.12/lib-dynload/_sqlite3.cpython-312-x86_64-linux-gnu.so` — size 144792->144792
- `/usr/lib/python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so` — size 225488->225488
- `/usr/lib/python3.12/lib-dynload/_testbuffer.cpython-312-x86_64-linux-gnu.so` — size 54216->54216
- `/usr/lib/python3.12/lib-dynload/_testcapi.cpython-312-x86_64-linux-gnu.so` — size 356136->356136
- `/usr/lib/python3.12/lib-dynload/_testclinic.cpython-312-x86_64-linux-gnu.so` — size 68936->68936
- `/usr/lib/python3.12/lib-dynload/_testimportmultiple.cpython-312-x86_64-linux-gnu.so` — size 14664->14664
- `/usr/lib/python3.12/lib-dynload/_testinternalcapi.cpython-312-x86_64-linux-gnu.so` — size 37128->37128
- `/usr/lib/python3.12/lib-dynload/_testmultiphase.cpython-312-x86_64-linux-gnu.so` — size 35752->35752
- `/usr/lib/python3.12/lib-dynload/_testsinglephase.cpython-312-x86_64-linux-gnu.so` — size 15456->15456
- `/usr/lib/python3.12/lib-dynload/_xxinterpchannels.cpython-312-x86_64-linux-gnu.so` — size 36360->36360
- `/usr/lib/python3.12/lib-dynload/_xxsubinterpreters.cpython-312-x86_64-linux-gnu.so` — size 23672->23672
- `/usr/lib/python3.12/lib-dynload/_xxtestfuzz.cpython-312-x86_64-linux-gnu.so` — size 23144->23144
- `/usr/lib/python3.12/lib-dynload/_zoneinfo.cpython-312-x86_64-linux-gnu.so` — size 53352->53352
- `/usr/lib/python3.12/lib-dynload/audioop.cpython-312-x86_64-linux-gnu.so` — size 64896->64896
- `/usr/lib/python3.12/lib-dynload/mmap.cpython-312-x86_64-linux-gnu.so` — size 32600->32600
- `/usr/lib/python3.12/lib-dynload/ossaudiodev.cpython-312-x86_64-linux-gnu.so` — size 33576->33576
- `/usr/lib/python3.12/lib-dynload/readline.cpython-312-x86_64-linux-gnu.so` — size 40640->40640
- `/usr/lib/python3.12/lib-dynload/resource.cpython-312-x86_64-linux-gnu.so` — size 19432->19432
- `/usr/lib/python3.12/lib-dynload/termios.cpython-312-x86_64-linux-gnu.so` — size 35520->35520
- `/usr/lib/python3.12/lib-dynload/xxlimited.cpython-312-x86_64-linux-gnu.so` — size 15200->15200
- `/usr/lib/python3.12/lib-dynload/xxlimited_35.cpython-312-x86_64-linux-gnu.so` — size 15104->15104
- `/usr/lib/python3.12/lib-dynload/xxsubtype.cpython-312-x86_64-linux-gnu.so` — size 16056->16056

**root-home** (2)

- `/root/.gitconfig` — size 251->226
- `/root/.wget-hsts` — size 209->209

**system-binaries** (116)

- `/usr/bin/busctl` — size 96864->96864
- `/usr/bin/chage` — size 72184->72184
- `/usr/bin/chfn` — size 72792->72792
- `/usr/bin/chsh` — size 44760->44760
- `/usr/bin/dirmngr` — size 485136->485136
- `/usr/bin/dirmngr-client` — size 56240->56240
- `/usr/bin/expiry` — size 27152->27152
- `/usr/bin/faillog` — size 23168->23168
- `/usr/bin/gapplication` — size 22920->22920
- `/usr/bin/gdb` — size 11744504->8920528
- `/usr/bin/gdbus` — size 51592->51592
- `/usr/bin/getsubids` — size 14640->14640
- `/usr/bin/gio` — size 104856->104856
- `/usr/bin/glib-compile-resources` — size 51520->51520
- `/usr/bin/gobject-query` — size 14656->14656
- `/usr/bin/gpasswd` — size 76248->76248
- `/usr/bin/gpg` — size 1147800->1147800
- `/usr/bin/gpg-agent` — size 366096->366096
- `/usr/bin/gpg-connect-agent` — size 89400->89400
- `/usr/bin/gpgconf` — size 118128->118128
- `/usr/bin/gpgparsemail` — size 35200->35200
- `/usr/bin/gpgsm` — size 513400->513400
- `/usr/bin/gpgsplit` — size 27256->27256
- `/usr/bin/gpgtar` — size 69456->69456
- `/usr/bin/gpgv` — size 310416->310416
- `/usr/bin/gresource` — size 22840->22840
- `/usr/bin/gsettings` — size 31032->31032
- `/usr/bin/gtester` — size 31056->31056
- `/usr/bin/hostnamectl` — size 31184->31184
- `/usr/bin/journalctl` — size 80800->80800
- `/usr/bin/kbxutil` — size 64336->64336
- `/usr/bin/kernel-install` — size 55984->55984
- `/usr/bin/lastlog` — size 28456->28456
- `/usr/bin/localectl` — size 27080->27080
- `/usr/bin/login` — size 53056->53056
- `/usr/bin/loginctl` — size 68176->68176
- `/usr/bin/networkctl` — size 125520->125520
- `/usr/bin/newgidmap` — size 41864->41864
- `/usr/bin/newgrp` — size 40664->40664
- `/usr/bin/newuidmap` — size 41864->41864
- `/usr/bin/passwd` — size 64152->64152
- `/usr/bin/python3.12` — size 8020928->8016832
- `/usr/bin/systemctl` — size 1501304->1501304
- `/usr/bin/systemd-ac-power` — size 14792->14792
- `/usr/bin/systemd-analyze` — size 203624->203624
- `/usr/bin/systemd-ask-password` — size 19024->19024
- `/usr/bin/systemd-cat` — size 18896->18896
- `/usr/bin/systemd-cgls` — size 23112->23112
- `/usr/bin/systemd-cgtop` — size 39392->39392
- `/usr/bin/systemd-creds` — size 43744->43744
- `/usr/bin/systemd-cryptenroll` — size 72624->72624
- `/usr/bin/systemd-cryptsetup` — size 80840->80840
- `/usr/bin/systemd-delta` — size 27080->27080
- `/usr/bin/systemd-detect-virt` — size 18888->18888
- `/usr/bin/systemd-escape` — size 22984->22984
- `/usr/bin/systemd-firstboot` — size 60232->60232
- `/usr/bin/systemd-id128` — size 22984->22984
- `/usr/bin/systemd-inhibit` — size 23008->23008
- `/usr/bin/systemd-machine-id-setup` — size 19072->19072
- `/usr/bin/systemd-mount` — size 52000->52000
- `/usr/bin/systemd-notify` — size 27304->27304
- `/usr/bin/systemd-path` — size 18888->18888
- `/usr/bin/systemd-repart` — size 199912->199912
- `/usr/bin/systemd-run` — size 68392->68392
- `/usr/bin/systemd-socket-activate` — size 31176->31176
- `/usr/bin/systemd-stdio-bridge` — size 22992->22992
- `/usr/bin/systemd-sysext` — size 55952->55952
- `/usr/bin/systemd-sysusers` — size 68224->68224
- `/usr/bin/systemd-tmpfiles` — size 117448->117448
- `/usr/bin/systemd-tty-ask-password-agent` — size 35272->35272
- `/usr/bin/timedatectl` — size 47560->47560
- `/usr/bin/varlinkctl` — size 31176->31176
- `/usr/bin/watchgnupg` — size 22840->22840
- `/usr/bin/x86_64-linux-gnu-addr2line` — size 31440->31440
- `/usr/bin/x86_64-linux-gnu-ar` — size 55792->55792
- `/usr/bin/x86_64-linux-gnu-as` — size 745768->745768
- `/usr/bin/x86_64-linux-gnu-c++filt` — size 22800->22800
- `/usr/bin/x86_64-linux-gnu-dwp` — size 1961344->1961344
- `/usr/bin/x86_64-linux-gnu-elfedit` — size 35552->35552
- `/usr/bin/x86_64-linux-gnu-gp-archive` — size 162336->162336
- `/usr/bin/x86_64-linux-gnu-gp-collect-app` — size 178552->178552
- `/usr/bin/x86_64-linux-gnu-gp-display-src` — size 153960->153960
- `/usr/bin/x86_64-linux-gnu-gp-display-text` — size 297336->297336
- `/usr/bin/x86_64-linux-gnu-gprof` — size 102184->102184
- `/usr/bin/x86_64-linux-gnu-gprofng` — size 141672->141672
- `/usr/bin/x86_64-linux-gnu-ld.bfd` — size 1359128->1359128
- `/usr/bin/x86_64-linux-gnu-ld.gold` — size 3218592->3218592
- `/usr/bin/x86_64-linux-gnu-nm` — size 44544->44544
- `/usr/bin/x86_64-linux-gnu-objcopy` — size 166536->166536
- `/usr/bin/x86_64-linux-gnu-objdump` — size 390848->390848
- `/usr/bin/x86_64-linux-gnu-ranlib` — size 55792->55792
- `/usr/bin/x86_64-linux-gnu-readelf` — size 789280->789280
- `/usr/bin/x86_64-linux-gnu-size` — size 31184->31184
- `/usr/bin/x86_64-linux-gnu-strings` — size 35440->35440
- `/usr/bin/x86_64-linux-gnu-strip` — size 166568->166568
- `/usr/bin/xmlcatalog` — size 22840->22840
- `/usr/bin/xmllint` — size 80840->80840
- `/usr/sbin/chgpasswd` — size 59720->59720
- `/usr/sbin/chpasswd` — size 55736->55736
- `/usr/sbin/cppw` — size 49608->49608
- _...and 16 more_

**system-libs** (52)

- `/usr/lib/x86_64-linux-gnu/bfd-plugins/libdep.so` — size 14560->14560
- `/usr/lib/x86_64-linux-gnu/cryptsetup/libcryptsetup-token-systemd-fido2.so` — size 18736->18736
- `/usr/lib/x86_64-linux-gnu/cryptsetup/libcryptsetup-token-systemd-pkcs11.so` — size 18736->18736
- `/usr/lib/x86_64-linux-gnu/cryptsetup/libcryptsetup-token-systemd-tpm2.so` — size 22832->22832
- `/usr/lib/x86_64-linux-gnu/glib-2.0/deb-can-run` — size 14648->14648
- `/usr/lib/x86_64-linux-gnu/glib-2.0/gi-compile-repository` — size 171008->171008
- `/usr/lib/x86_64-linux-gnu/glib-2.0/gi-decompile-typelib` — size 47416->47416
- `/usr/lib/x86_64-linux-gnu/glib-2.0/gi-inspect-typelib` — size 14648->14648
- `/usr/lib/x86_64-linux-gnu/glib-2.0/gio-launch-desktop` — size 14648->14648
- `/usr/lib/x86_64-linux-gnu/glib-2.0/gio-querymodules` — size 18744->18744
- `/usr/lib/x86_64-linux-gnu/glib-2.0/glib-compile-schemas` — size 55608->55608
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-collector.so` — size 1341720->1341720
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-collectorAPI.a` — size 34346->34362
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-collectorAPI.so` — size 14536->14536
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-heap.so` — size 18744->18744
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-iotrace.so` — size 63832->63832
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-sync.so` — size 26904->26904
- `/usr/lib/x86_64-linux-gnu/libavahi-client.so.3.2.9` — size 72016->72016
- `/usr/lib/x86_64-linux-gnu/libavahi-common.so.3.5.4` — size 51648->51648
- `/usr/lib/x86_64-linux-gnu/libbfd-2.42-system.so` — size 1479888->1479888
- `/usr/lib/x86_64-linux-gnu/libctf-nobfd.so.0.0.0` — size 216096->216096
- `/usr/lib/x86_64-linux-gnu/libctf.so.0.0.0` — size 220384->220384
- `/usr/lib/x86_64-linux-gnu/libcups.so.2` — size 653416->653416
- `/usr/lib/x86_64-linux-gnu/libexslt.so.0.8.21` — size 96416->96416
- `/usr/lib/x86_64-linux-gnu/libgio-2.0.a` — size 4792862->4792830
- `/usr/lib/x86_64-linux-gnu/libgio-2.0.so.0.8000.0` — size 1887792->1887792
- `/usr/lib/x86_64-linux-gnu/libgirepository-2.0.so.0.8000.0` — size 162648->162648
- `/usr/lib/x86_64-linux-gnu/libglib-2.0.a` — size 2370422->2369830
- `/usr/lib/x86_64-linux-gnu/libglib-2.0.so.0.8000.0` — size 1343056->1343056
- `/usr/lib/x86_64-linux-gnu/libgmodule-2.0.so.0.8000.0` — size 22736->22736
- `/usr/lib/x86_64-linux-gnu/libgobject-2.0.so.0.8000.0` — size 399752->399752
- `/usr/lib/x86_64-linux-gnu/libgprofng.so.0.0.0` — size 2334672->2334672
- `/usr/lib/x86_64-linux-gnu/libgthread-2.0.so.0.8000.0` — size 14488->14488
- `/usr/lib/x86_64-linux-gnu/libheif.so.1.17.6` — size 784960->784960
- `/usr/lib/x86_64-linux-gnu/libheif/plugins/libheif-aomdec.so` — size 14440->14440
- `/usr/lib/x86_64-linux-gnu/libheif/plugins/libheif-libde265.so` — size 18624->18624
- `/usr/lib/x86_64-linux-gnu/libopcodes-2.42-system.so` — size 911656->911656
- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357020->355524
- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — size 223304->223304
- `/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0` — size 9056904->9056904
- `/usr/lib/x86_64-linux-gnu/libsframe.so.1.0.0` — size 35168->35168
- `/usr/lib/x86_64-linux-gnu/libsodium.so.23.3.0` — size 355040->355040
- `/usr/lib/x86_64-linux-gnu/libsubid.so.4.0.0` — size 41248->41248
- `/usr/lib/x86_64-linux-gnu/libsystemd.so.0.38.0` — size 910592->910592
- `/usr/lib/x86_64-linux-gnu/libtasn1.so.6.6.3` — size 88216->88216
- `/usr/lib/x86_64-linux-gnu/libudev.so.1.7.8` — size 207288->207288
- `/usr/lib/x86_64-linux-gnu/libxml2.so.2.9.14` — size 1967424->1967424
- `/usr/lib/x86_64-linux-gnu/libxslt.so.1.1.39` — size 260536->260536
- `/usr/lib/x86_64-linux-gnu/security/pam_systemd.so` — size 537832->537832
- `/usr/lib/x86_64-linux-gnu/security/pam_systemd_loadkey.so` — size 31752->31752
- `/usr/lib/x86_64-linux-gnu/systemd/libsystemd-core-255.so` — size 2140744->2140744
- `/usr/lib/x86_64-linux-gnu/systemd/libsystemd-shared-255.so` — size 3755032->3755032

**usr-share** (6)

- `/usr/share/gdb/python/gdb/dap/breakpoint.py` — size 13936->13851
- `/usr/share/gdb/python/gdb/dap/bt.py` — size 5671->5632
- `/usr/share/gdb/python/gdb/dap/disassemble.py` — size 1634->3527
- `/usr/share/gdb/python/gdb/dap/memory.py` — size 1386->1513
- `/usr/share/gdb/python/gdb/dap/sources.py` — size 2990->3137
- `/usr/share/info/gnupg-module-overview.png` — size 122772->122772

## Excluded (expected differences)

- excluded: 867,820
- expected_only_left: 24,564
- expected_only_right: 12,908
- hash_excluded: 1,336
