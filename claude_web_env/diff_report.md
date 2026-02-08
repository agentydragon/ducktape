# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count     | %        |
| -------------------- | --------- | -------- |
| Identical            | 120,576   | 6.8%     |
| Excluded (expected)  | 1,664,740 | 93.2%    |
| **Real differences** | **112**   | **0.0%** |
| Total                | 1,785,428 |          |

## Real Differences

### Only in built (1)

**python-libs** (1)

- `/usr/lib/python3.13/pydoc_data/module_docs.py`

### Content changed (hash differs) (111)

**docs** (4)

- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 1963->1501
- `/usr/share/doc/libpython3.13-stdlib/changelog.Debian.gz` — size 19356->19377
- `/usr/share/doc/linux-libc-dev/changelog.Debian.gz` — size 532128->531692
- `/usr/share/doc/python3.13/changelog.Debian.gz` — size 19350->19369

**headers** (6)

- `/usr/include/python3.13/cpython/object.h` — size 19074->19461
- `/usr/include/python3.13/cpython/pyerrors.h` — size 2908->2932
- `/usr/include/python3.13/internal/pycore_ceval.h` — size 11366->11384
- `/usr/include/python3.13/internal/pycore_ceval_state.h` — size 3921->4036
- `/usr/include/python3.13/internal/pycore_pymath.h` — size 8600->8600
- `/usr/include/python3.13/patchlevel.h` — size 1301->1301

**python-libs** (97)

- `/usr/lib/python3.13/_android_support.py` — size 7065->7417
- `/usr/lib/python3.13/_pyio.py` — size 93809->93862
- `/usr/lib/python3.13/_pyrepl/unix_console.py` — size 26721->26762
- `/usr/lib/python3.13/_pyrepl/windows_console.py` — size 21620->21892
- `/usr/lib/python3.13/_sitebuiltins.py` — size 3128->2699
- `/usr/lib/python3.13/_sysconfigdata__x86_64-linux-gnu.py` — size 47913->47913
- `/usr/lib/python3.13/argparse.py` — size 101661->102926
- `/usr/lib/python3.13/asyncio/__main__.py` — size 6171->6281
- `/usr/lib/python3.13/asyncio/futures.py` — size 14157->14189
- `/usr/lib/python3.13/asyncio/selector_events.py` — size 48474->48623
- `/usr/lib/python3.13/config-3.13-x86_64-linux-gnu/Makefile` — size 204830->205166
- `/usr/lib/python3.13/config-3.13-x86_64-linux-gnu/libpython3.13-pic.a` — size 11497270->11502642
- `/usr/lib/python3.13/config-3.13-x86_64-linux-gnu/libpython3.13.a` — size 11868934->11877914
- `/usr/lib/python3.13/config-3.13-x86_64-linux-gnu/python.o` — size 11048->11048
- `/usr/lib/python3.13/email/_encoded_words.py` — size 8541->8541
- `/usr/lib/python3.13/email/_header_value_parser.py` — size 111175->113072
- `/usr/lib/python3.13/email/feedparser.py` — size 22905->22869
- `/usr/lib/python3.13/email/generator.py` — size 20829->21417
- `/usr/lib/python3.13/email/headerregistry.py` — size 20819->21244
- `/usr/lib/python3.13/enum.py` — size 85593->85619
- `/usr/lib/python3.13/hmac.py` — size 7716->7759
- `/usr/lib/python3.13/http/cookies.py` — size 19951->20854
- `/usr/lib/python3.13/inspect.py` — size 128640->128803
- `/usr/lib/python3.13/lib-dynload/_asyncio.cpython-313-x86_64-linux-gnu.so` — size 65560->65560
- `/usr/lib/python3.13/lib-dynload/_bz2.cpython-313-x86_64-linux-gnu.so` — size 32112->32112
- `/usr/lib/python3.13/lib-dynload/_codecs_cn.cpython-313-x86_64-linux-gnu.so` — size 154160->154160
- `/usr/lib/python3.13/lib-dynload/_codecs_hk.cpython-313-x86_64-linux-gnu.so` — size 162384->162384
- `/usr/lib/python3.13/lib-dynload/_codecs_iso2022.cpython-313-x86_64-linux-gnu.so` — size 31312->31312
- `/usr/lib/python3.13/lib-dynload/_codecs_jp.cpython-313-x86_64-linux-gnu.so` — size 272944->272944
- `/usr/lib/python3.13/lib-dynload/_codecs_kr.cpython-313-x86_64-linux-gnu.so` — size 141872->141872
- `/usr/lib/python3.13/lib-dynload/_codecs_tw.cpython-313-x86_64-linux-gnu.so` — size 113200->113200
- `/usr/lib/python3.13/lib-dynload/_contextvars.cpython-313-x86_64-linux-gnu.so` — size 14536->14536
- `/usr/lib/python3.13/lib-dynload/_ctypes.cpython-313-x86_64-linux-gnu.so` — size 138376->138376
- `/usr/lib/python3.13/lib-dynload/_ctypes_test.cpython-313-x86_64-linux-gnu.so` — size 31360->31360
- `/usr/lib/python3.13/lib-dynload/_curses.cpython-313-x86_64-linux-gnu.so` — size 128584->128584
- `/usr/lib/python3.13/lib-dynload/_curses_panel.cpython-313-x86_64-linux-gnu.so` — size 28272->28272
- `/usr/lib/python3.13/lib-dynload/_dbm.cpython-313-x86_64-linux-gnu.so` — size 23888->23888
- `/usr/lib/python3.13/lib-dynload/_decimal.cpython-313-x86_64-linux-gnu.so` — size 330808->334904
- `/usr/lib/python3.13/lib-dynload/_hashlib.cpython-313-x86_64-linux-gnu.so` — size 64336->64336
- `/usr/lib/python3.13/lib-dynload/_interpchannels.cpython-313-x86_64-linux-gnu.so` — size 45440->45440
- `/usr/lib/python3.13/lib-dynload/_interpqueues.cpython-313-x86_64-linux-gnu.so` — size 31880->31880
- `/usr/lib/python3.13/lib-dynload/_interpreters.cpython-313-x86_64-linux-gnu.so` — size 36928->36928
- `/usr/lib/python3.13/lib-dynload/_json.cpython-313-x86_64-linux-gnu.so` — size 44840->44840
- `/usr/lib/python3.13/lib-dynload/_lsprof.cpython-313-x86_64-linux-gnu.so` — size 27920->27920
- `/usr/lib/python3.13/lib-dynload/_lzma.cpython-313-x86_64-linux-gnu.so` — size 45104->45104
- `/usr/lib/python3.13/lib-dynload/_multibytecodec.cpython-313-x86_64-linux-gnu.so` — size 50568->50568
- `/usr/lib/python3.13/lib-dynload/_multiprocessing.cpython-313-x86_64-linux-gnu.so` — size 24248->24248
- `/usr/lib/python3.13/lib-dynload/_posixshmem.cpython-313-x86_64-linux-gnu.so` — size 14880->14880
- `/usr/lib/python3.13/lib-dynload/_queue.cpython-313-x86_64-linux-gnu.so` — size 19632->19632
- `/usr/lib/python3.13/lib-dynload/_sqlite3.cpython-313-x86_64-linux-gnu.so` — size 145040->149136
- `/usr/lib/python3.13/lib-dynload/_ssl.cpython-313-x86_64-linux-gnu.so` — size 225776->225776
- `/usr/lib/python3.13/lib-dynload/_testbuffer.cpython-313-x86_64-linux-gnu.so` — size 54248->54248
- `/usr/lib/python3.13/lib-dynload/_testcapi.cpython-313-x86_64-linux-gnu.so` — size 292088->292088
- `/usr/lib/python3.13/lib-dynload/_testclinic.cpython-313-x86_64-linux-gnu.so` — size 91848->91848
- `/usr/lib/python3.13/lib-dynload/_testclinic_limited.cpython-313-x86_64-linux-gnu.so` — size 14696->14696
- `/usr/lib/python3.13/lib-dynload/_testexternalinspection.cpython-313-x86_64-linux-gnu.so` — size 14816->14816
- `/usr/lib/python3.13/lib-dynload/_testimportmultiple.cpython-313-x86_64-linux-gnu.so` — size 14728->14728
- `/usr/lib/python3.13/lib-dynload/_testinternalcapi.cpython-313-x86_64-linux-gnu.so` — size 81360->81360
- `/usr/lib/python3.13/lib-dynload/_testlimitedcapi.cpython-313-x86_64-linux-gnu.so` — size 178912->178912
- `/usr/lib/python3.13/lib-dynload/_testmultiphase.cpython-313-x86_64-linux-gnu.so` — size 31888->31888
- `/usr/lib/python3.13/lib-dynload/_testsinglephase.cpython-313-x86_64-linux-gnu.so` — size 20104->20104
- `/usr/lib/python3.13/lib-dynload/_uuid.cpython-313-x86_64-linux-gnu.so` — size 14720->14720
- `/usr/lib/python3.13/lib-dynload/_xxtestfuzz.cpython-313-x86_64-linux-gnu.so` — size 23152->23152
- `/usr/lib/python3.13/lib-dynload/_zoneinfo.cpython-313-x86_64-linux-gnu.so` — size 36872->36872
- `/usr/lib/python3.13/lib-dynload/mmap.cpython-313-x86_64-linux-gnu.so` — size 32600->32600
- `/usr/lib/python3.13/lib-dynload/readline.cpython-313-x86_64-linux-gnu.so` — size 36544->36544
- `/usr/lib/python3.13/lib-dynload/resource.cpython-313-x86_64-linux-gnu.so` — size 23552->23552
- `/usr/lib/python3.13/lib-dynload/termios.cpython-313-x86_64-linux-gnu.so` — size 35584->35584
- `/usr/lib/python3.13/lib-dynload/xxlimited.cpython-313-x86_64-linux-gnu.so` — size 15176->15176
- `/usr/lib/python3.13/lib-dynload/xxlimited_35.cpython-313-x86_64-linux-gnu.so` — size 15080->15080
- `/usr/lib/python3.13/lib-dynload/xxsubtype.cpython-313-x86_64-linux-gnu.so` — size 16032->16032
- `/usr/lib/python3.13/linecache.py` — size 7284->7488
- `/usr/lib/python3.13/logging/handlers.py` — size 62372->62524
- `/usr/lib/python3.13/mailbox.py` — size 81644->81531
- `/usr/lib/python3.13/multiprocessing/forkserver.py` — size 12648->12842
- `/usr/lib/python3.13/multiprocessing/spawn.py` — size 9644->9659
- `/usr/lib/python3.13/pdb.py` — size 91982->92522
- `/usr/lib/python3.13/plistlib.py` — size 30034->30024
- `/usr/lib/python3.13/pydoc.py` — size 110322->110658
- `/usr/lib/python3.13/pydoc_data/topics.py` — size 530360->533248
- `/usr/lib/python3.13/ssl.py` — size 52706->52706
- `/usr/lib/python3.13/stat.py` — size 6147->6308
- `/usr/lib/python3.13/subprocess.py` — size 90826->90827
- `/usr/lib/python3.13/test/libregrtest/cmdline.py` — size 24278->24635
- `/usr/lib/python3.13/test/libregrtest/main.py` — size 28970->28975
- `/usr/lib/python3.13/test/libregrtest/runtests.py` — size 7191->7533
- `/usr/lib/python3.13/test/libregrtest/utils.py` — size 24701->25039
- `/usr/lib/python3.13/test/support/__init__.py` — size 93368->93985
- `/usr/lib/python3.13/test/support/import_helper.py` — size 10688->10688
- `/usr/lib/python3.13/test/support/pty_helper.py` — size 3052->3300
- `/usr/lib/python3.13/types.py` — size 11207->11324
- `/usr/lib/python3.13/typing.py` — size 133263->133261
- `/usr/lib/python3.13/unittest/mock.py` — size 110923->110945
- `/usr/lib/python3.13/urllib/request.py` — size 102464->102673
- `/usr/lib/python3.13/wsgiref/headers.py` — size 6766->6943
- `/usr/lib/python3.13/xml/dom/minidom.py` — size 68388->68456
- `/usr/lib/python3.13/zoneinfo/_common.py` — size 5529->5587

**system-binaries** (1)

- `/usr/bin/python3.13` — size 6498936->6507184

**system-libs** (3)

- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357020->355524
- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — size 223304->223304
- `/usr/lib/x86_64-linux-gnu/libpython3.13.so.1.0` — size 7359248->7363400

## Excluded (expected differences)

- excluded: 1,625,907
- expected_only_left: 24,569
- expected_only_right: 12,910
- hash_excluded: 1,354

## Exclusion Pattern Utilization

109 patterns excluded 1,664,740 paths (1,664,740 attributed to specific patterns). 10 patterns matched 0 paths.
Ratio: 1.0x patterns per real diff.

### `skip_paths` (30 patterns, 1,625,907 hits, 1 unused)

|      Hits | Pattern                         |
| --------: | ------------------------------- |
| 1,125,259 | `/tmp`                          |
|   469,513 | `/root/.cache`                  |
|    14,125 | `/proc`                         |
|     6,223 | `/root/.npm`                    |
|     5,194 | `/home/user/ducktape`           |
|     2,870 | `/var/lib/dpkg/info`            |
|     1,721 | `/root/.local/share/virtualenv` |
|       408 | `/root/.claude/plugins`         |
|       176 | `/sys`                          |
|       123 | `/home/claude/.npm`             |
|        48 | `/run`                          |
|        40 | `/root/.claude/projects`        |
|        33 | `/root/.claude/session-env`     |
|        33 | `/root/.claude/todos`           |
|        26 | `/var/lib/apt/lists`            |
|        21 | `/var/tmp`                      |
|        20 | `/root/.claude/debug`           |
|        19 | `/var/log`                      |
|        18 | `/dev`                          |
|        16 | `/root/.claude/shell-snapshots` |
|         4 | `/root/.claude/statsig`         |
|         4 | `/var/cache/apt`                |
|         3 | `/root/.local/share/pnpm`       |
|         3 | `/var/lib/containers`           |
|         2 | `/home/claude/.claude/remote`   |
|         2 | `/home/claude/.ssh`             |
|         1 | `/home/claude/.cache`           |
|         1 | `/root/.claude/plans`           |
|         1 | `/root/.claude/telemetry`       |
|         0 | `/nix` **UNUSED**               |

### `volatile_paths` (44 patterns, 38,750 hits, 3 unused)

|   Hits | Pattern                                  |
| -----: | ---------------------------------------- |
| 22,094 | `/root/.local/share/uv/**`               |
|  8,701 | `/opt/ruby-*`                            |
|  2,903 | `/usr/local/lib/python*/**`              |
|  1,832 | `/opt/rbenv/**`                          |
|  1,496 | `**/__pycache__/**`                      |
|  1,240 | `/root/.local/lib/python*/**`            |
|    257 | `/opt/nvm/**`                            |
|    145 | `/opt/node*/**`                          |
|     19 | `/var/cache/fontconfig/**`               |
|     17 | `/root/.rustup/**`                       |
|      6 | `/root/.local/share/gem/**`              |
|      4 | `/var/cache/debconf/**`                  |
|      4 | `/var/lib/dpkg/alternatives/**`          |
|      3 | `/var/lib/postgresql/**`                 |
|      2 | `/root/.local/bin/*`                     |
|      2 | `/usr/local/use-go-*.sh`                 |
|      1 | `/etc/group`                             |
|      1 | `/etc/group-`                            |
|      1 | `/etc/gshadow`                           |
|      1 | `/etc/gshadow-`                          |
|      1 | `/etc/hostname`                          |
|      1 | `/etc/hosts`                             |
|      1 | `/etc/machine-id`                        |
|      1 | `/etc/passwd`                            |
|      1 | `/etc/passwd-`                           |
|      1 | `/etc/postgresql/**`                     |
|      1 | `/etc/shadow`                            |
|      1 | `/etc/shadow-`                           |
|      1 | `/etc/ssl/certs/java/cacerts`            |
|      1 | `/etc/ssl/certs/ssl-cert-snakeoil.pem`   |
|      1 | `/etc/ssl/private/ssl-cert-snakeoil.key` |
|      1 | `/etc/sudoers`                           |
|      1 | `/root/.wget-hsts`                       |
|      1 | `/usr/local/bin/composer`                |
|      1 | `/usr/local/bin/golangci-lint`           |
|      1 | `/var/cache/ldconfig/**`                 |
|      1 | `/var/lib/apt/extended_states`           |
|      1 | `/var/lib/dbus/machine-id`               |
|      1 | `/var/lib/dpkg/status`                   |
|      1 | `/var/lib/dpkg/status-old`               |
|      1 | `/var/lib/dpkg/triggers/**`              |
|      0 | `**/__pycache__` **UNUSED**              |
|      0 | `/var/lib/sgml-base/**` **UNUSED**       |
|      0 | `/var/lib/systemd/**` **UNUSED**         |

### `only_in_live` (24 patterns, 77 hits, 1 unused)

| Hits | Pattern                                         |
| ---: | ----------------------------------------------- |
|   30 | `/root/.config/**`                              |
|   18 | `/root/.gradle/**`                              |
|    8 | `/root/.launchpadlib/**`                        |
|    2 | `/root/.local/state/**`                         |
|    1 | `/.dockerenv`                                   |
|    1 | `/container_info.json`                          |
|    1 | `/etc/alternatives/python`                      |
|    1 | `/etc/apt/sources.list`                         |
|    1 | `/etc/apt/sources.list.d/ubuntu.sources`        |
|    1 | `/etc/containers/networks`                      |
|    1 | `/etc/ssl/certs/*.0`                            |
|    1 | `/root/.bazelrc`                                |
|    1 | `/root/.claude.json`                            |
|    1 | `/root/.claude.json.backup`                     |
|    1 | `/root/.claude/stats-cache.json`                |
|    1 | `/root/.claude/stop-hook-git-check.sh`          |
|    1 | `/root/.gradle`                                 |
|    1 | `/root/.launchpadlib`                           |
|    1 | `/root/.local/state`                            |
|    1 | `/usr/bin/python`                               |
|    1 | `/var/cache/containers`                         |
|    1 | `/var/cache/containers/**`                      |
|    1 | `/var/lib/dpkg/alternatives/python`             |
|    0 | `/var/lib/dpkg/alternatives/python3` **UNUSED** |

### `session_hook_artifacts` (5 patterns, 0 hits, 5 unused)

| Hits | Pattern                                         |
| ---: | ----------------------------------------------- |
|    0 | `/etc/containers/containers.conf` **UNUSED**    |
|    0 | `/root/.nix-defexpr` **UNUSED**                 |
|    0 | `/root/.nix-defexpr/**` **UNUSED**              |
|    0 | `/root/.nix-profile` **UNUSED**                 |
|    0 | `/usr/local/bin/crun-gvisor-wrapper` **UNUSED** |

### `only_in_built` (6 patterns, 6 hits, 0 unused)

| Hits | Pattern                         |
| ---: | ------------------------------- |
|    1 | `/etc/apt/apt.conf.d/80retries` |
|    1 | `/etc/ssl/certs/*.0`            |
|    1 | `/usr/local/bin/conan`          |
|    1 | `/usr/local/bin/httpx`          |
|    1 | `/usr/local/bin/normalizer`     |
|    1 | `/usr/local/bin/websockets`     |
