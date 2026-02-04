# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count     | %        |
| -------------------- | --------- | -------- |
| Identical            | 120,639   | 11.7%    |
| Excluded (expected)  | 907,038   | 88.3%    |
| **Real differences** | **69**    | **0.0%** |
| Total                | 1,027,746 |          |

## Real Differences

### Only in live (11)

**claude-config** (2)

- `/root/.claude/stats-cache.json`
- `/root/.claude/stop-hook-git-check.sh`

**docs** (6)

- `/usr/share/doc/python3/_static`
- `/usr/share/doc/python3/_static/doctools.js`
- `/usr/share/doc/python3/_static/language_data.js`
- `/usr/share/doc/python3/_static/searchtools.js`
- `/usr/share/doc/python3/_static/sphinx_highlight.js`
- `/usr/share/doc/python3/index.html`

**root-local** (3)

- `/root/.local/state`
- `/root/.local/state/pnpm`
- `/root/.local/state/pnpm/pnpm-state.json`

### Content changed (hash differs) (58)

**docs** (2)

- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 1963->1501
- `/usr/share/doc/libpython3.12-minimal/changelog.Debian.gz` — size 10631->10516

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

**system-binaries** (1)

- `/usr/bin/python3.12` — size 8020928->8016832

**system-libs** (3)

- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357020->355524
- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — size 223304->223304
- `/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0` — size 9056904->9056904

## Excluded (expected differences)

- excluded: 868,228
- expected_only_left: 24,565
- expected_only_right: 12,908
- hash_excluded: 1,337
