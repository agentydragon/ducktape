# Filesystem Diff Report

**live** vs **built**

Generated: 2026-02-03

## Status

**Build v11**: Added stripping, PHP 8.4 version pin, and python3-apt version pin.

Previous run (v10) showed 241 real differences. The changes in this version:

1. **Stripping added**: `rm -rf` for `/usr/share/doc`, `/usr/share/man`, `/usr/include`,
   `/usr/sbin`, `/usr/share/X11`, etc. to match live container's size optimization.

2. **PHP 8.4 pinned**: Version `8.4.15-1+ubuntu24.04.1+deb.sury.org+1` via APT preferences.

3. **python3-apt pinned**: Version `2.7.7ubuntu5.1` to match live.

## Remaining Work

For exact binary matching, still need to pin:

- util-linux, binutils, gdb to specific versions
- Use snapshot.ubuntu.com for remaining packages

## Summary

| | Count | % |
|---|---|---|
| Identical | 120,924 | 15.4% |
| Excluded (expected) | 662,767 | 84.5% |
| **Real differences** | **241** | **0.0%** |
| Total | 783,932 | |

## Real Differences

### Only in live (4)

**python-libs** (4)

- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.1.egg-info`
- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.1.egg-info/PKG-INFO`
- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.1.egg-info/dependency_links.txt`
- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.1.egg-info/top_level.txt`

### Only in built (4)

**python-libs** (4)

- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.egg-info`
- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.egg-info/PKG-INFO`
- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.egg-info/dependency_links.txt`
- `/usr/lib/python3/dist-packages/python_apt-2.7.7+ubuntu5.egg-info/top_level.txt`

### Content changed (hash differs) (233)

**docs** (19)

- `/usr/share/doc/binutils-common/changelog.Debian.gz` — size 1928->1963
- `/usr/share/doc/bsdutils/changelog.Debian.gz` — size 13927->14002
- `/usr/share/doc/fonts-opensymbol/changelog.Debian.gz` — size 36166->42367
- `/usr/share/doc/fonts-opensymbol/copyright` — size 21830->20199
- `/usr/share/doc/gdb/changelog.Debian.gz` — size 4004->4529
- `/usr/share/doc/libblkid1/changelog.Debian.gz` — size 13925->14001
- `/usr/share/doc/libctf-nobfd0/changelog.Debian.gz` — size 1931->1965
- `/usr/share/doc/libcups2t64/changelog.Debian.gz` — size 10464->10249
- `/usr/share/doc/libsframe1/changelog.Debian.gz` — size 1928->1962
- `/usr/share/doc/libsmartcols1/changelog.Debian.gz` — size 13926->14001
- `/usr/share/doc/libsubid4/changelog.Debian.gz` — size 6467->6641
- `/usr/share/doc/libuuid1/changelog.Debian.gz` — size 13929->14004
- `/usr/share/doc/login/changelog.Debian.gz` — size 6466->6641
- `/usr/share/doc/passwd/changelog.Debian.gz` — size 6467->6641
- `/usr/share/doc/php-common/changelog.gz` — size 2107->2169
- `/usr/share/doc/php8.4-common/changelog.Debian.gz` — size 7882->8090
- `/usr/share/doc/php8.4-common/changelog.gz` — size 24220->26057
- `/usr/share/doc/python-apt-common/changelog.gz` — size 6832->6737
- `/usr/share/doc/python3-apt/changelog.gz` — size 6831->6736

**etc** (1)

- `/etc/pam.d/login` — size 4118->3974

**headers** (3)

- `/usr/include/php/20240924/Zend/zend.h` — size 17662->17662
- `/usr/include/php/20240924/ext/mbstring/php_onig_compat.h` — size 256->426
- `/usr/include/php/20240924/main/php_version.h` — size 266->266

**other** (38)

- `/usr/lib/php/20240924/build/gen_stub.php` — size 223580->223643
- `/usr/lib/php/20240924/build/run-tests.php` — size 142534->142534
- `/usr/lib/php/20240924/calendar.so` — size 39176->39176
- `/usr/lib/php/20240924/ctype.so` — size 14600->14600
- `/usr/lib/php/20240924/curl.so` — size 137480->137480
- `/usr/lib/php/20240924/dom.so` — size 2099008->2099008
- `/usr/lib/php/20240924/exif.so` — size 92424->92424
- `/usr/lib/php/20240924/ffi.so` — size 186632->186632
- `/usr/lib/php/20240924/fileinfo.so` — size 8653120->8653120
- `/usr/lib/php/20240924/ftp.so` — size 67848->67848
- `/usr/lib/php/20240924/gd.so` — size 149768->149768
- `/usr/lib/php/20240924/gettext.so` — size 22792->22792
- `/usr/lib/php/20240924/iconv.so` — size 55560->55560
- `/usr/lib/php/20240924/intl.so` — size 653664->653664
- `/usr/lib/php/20240924/mbstring.so` — size 1225600->1225600
- `/usr/lib/php/20240924/mysqli.so` — size 166152->166152
- `/usr/lib/php/20240924/mysqlnd.so` — size 223048->223048
- `/usr/lib/php/20240924/opcache.so` — size 1204888->1209112
- `/usr/lib/php/20240924/pdo.so` — size 137480->137480
- `/usr/lib/php/20240924/pdo_mysql.so` — size 39176->39176
- `/usr/lib/php/20240924/pdo_pgsql.so` — size 63752->63752
- `/usr/lib/php/20240924/pgsql.so` — size 186632->186632
- `/usr/lib/php/20240924/phar.so` — size 293128->297224
- `/usr/lib/php/20240924/posix.so` — size 47368->47368
- `/usr/lib/php/20240924/readline.so` — size 39176->39176
- `/usr/lib/php/20240924/shmop.so` — size 18696->18696
- `/usr/lib/php/20240924/simplexml.so` — size 63752->63752
- `/usr/lib/php/20240924/sockets.so` — size 117000->117000
- `/usr/lib/php/20240924/sysvmsg.so` — size 22792->22792
- `/usr/lib/php/20240924/sysvsem.so` — size 14600->14600
- `/usr/lib/php/20240924/sysvshm.so` — size 22792->22792
- `/usr/lib/php/20240924/tokenizer.so` — size 35080->35080
- `/usr/lib/php/20240924/xml.so` — size 76040->76040
- `/usr/lib/php/20240924/xmlreader.so` — size 55560->55560
- `/usr/lib/php/20240924/xmlwriter.so` — size 55560->55560
- `/usr/lib/php/20240924/xsl.so` — size 39176->39176
- `/usr/lib/php/20240924/zip.so` — size 108808->108808
- `/usr/lib/php/php-maintscript-helper` — size 9234->9217

**python-libs** (2)

- `/usr/lib/python3/dist-packages/apt_inst.cpython-312-x86_64-linux-gnu.so` — size 60072->60072
- `/usr/lib/python3/dist-packages/apt_pkg.cpython-312-x86_64-linux-gnu.so` — size 347328->347328

**system-binaries** (143)

- `/usr/bin/addpart` — size 14720->14720
- `/usr/bin/chage` — size 72184->72184
- `/usr/bin/chfn` — size 72792->72792
- `/usr/bin/choom` — size 22912->22912
- `/usr/bin/chrt` — size 31104->31104
- `/usr/bin/chsh` — size 44760->44760
- `/usr/bin/delpart` — size 14720->14720
- `/usr/bin/dmesg` — size 70288->70288
- `/usr/bin/expiry` — size 27152->27152
- `/usr/bin/faillog` — size 23168->23168
- `/usr/bin/fallocate` — size 27008->27008
- `/usr/bin/findmnt` — size 69280->69280
- `/usr/bin/flock` — size 23024->23024
- `/usr/bin/gdb` — size 11744504->8920528
- `/usr/bin/getopt` — size 22912->22912
- `/usr/bin/getsubids` — size 14640->14640
- `/usr/bin/gpasswd` — size 76248->76248
- `/usr/bin/hardlink` — size 47600->47600
- `/usr/bin/ionice` — size 18816->18816
- `/usr/bin/ipcmk` — size 22984->22984
- `/usr/bin/ipcrm` — size 18816->18816
- `/usr/bin/ipcs` — size 39296->39296
- `/usr/bin/last` — size 35200->35200
- `/usr/bin/lastlog` — size 28456->28456
- `/usr/bin/logger` — size 39904->39904
- `/usr/bin/login` — size 53056->53056
- `/usr/bin/lsblk` — size 149896->149896
- `/usr/bin/lscpu` — size 113032->113032
- `/usr/bin/lsipc` — size 51584->51584
- `/usr/bin/lslocks` — size 31504->31504
- `/usr/bin/lslogins` — size 51584->51584
- `/usr/bin/lsmem` — size 39296->39296
- `/usr/bin/lsns` — size 43400->43400
- `/usr/bin/mcookie` — size 27080->27080
- `/usr/bin/mesg` — size 14720->14720
- `/usr/bin/more` — size 47496->47496
- `/usr/bin/mount` — size 51584->51584
- `/usr/bin/mountpoint` — size 18816->18816
- `/usr/bin/namei` — size 22912->22912
- `/usr/bin/newgidmap` — size 41864->41864
- `/usr/bin/newgrp` — size 40664->40664
- `/usr/bin/newuidmap` — size 41864->41864
- `/usr/bin/nsenter` — size 31336->31336
- `/usr/bin/partx` — size 63880->63880
- `/usr/bin/passwd` — size 64152->64152
- `/usr/bin/phar8.4.phar` — size 15242->15242
- `/usr/bin/php-config8.4` — size 4803->4803
- `/usr/bin/php8.4` — size 6025808->6029904
- `/usr/bin/prlimit` — size 27536->27536
- `/usr/bin/rename.ul` — size 22912->22912
- `/usr/bin/renice` — size 14720->14720
- `/usr/bin/resizepart` — size 22912->22912
- `/usr/bin/rev` — size 14720->14720
- `/usr/bin/script` — size 55680->55680
- `/usr/bin/scriptlive` — size 43392->43392
- `/usr/bin/scriptreplay` — size 35200->35200
- `/usr/bin/setarch` — size 27288->27288
- `/usr/bin/setpriv` — size 39304->39304
- `/usr/bin/setsid` — size 14720->14720
- `/usr/bin/setterm` — size 35200->35200
- `/usr/bin/su` — size 55680->55680
- `/usr/bin/taskset` — size 31104->31104
- `/usr/bin/uclampset` — size 31104->31104
- `/usr/bin/umount` — size 39296->39296
- `/usr/bin/unshare` — size 43624->43624
- `/usr/bin/utmpdump` — size 22912->22912
- `/usr/bin/wall` — size 22912->22912
- `/usr/bin/wdctl` — size 35224->35224
- `/usr/bin/whereis` — size 31576->31576
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
- `/usr/sbin/agetty` — size 60992->60992
- `/usr/sbin/blkdiscard` — size 22912->22912
- `/usr/sbin/blkid` — size 55720->55720
- `/usr/sbin/blkzone` — size 35200->35200
- `/usr/sbin/blockdev` — size 35200->35200
- `/usr/sbin/chcpu` — size 31104->31104
- `/usr/sbin/chgpasswd` — size 59720->59720
- `/usr/sbin/chmem` — size 35200->35200
- `/usr/sbin/chpasswd` — size 55736->55736
- *...and 43 more*

**system-libs** (20)

- `/usr/lib/x86_64-linux-gnu/bfd-plugins/libdep.so` — size 14560->14560
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-collector.so` — size 1341720->1341720
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-collectorAPI.a` — size 34346->34362
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-collectorAPI.so` — size 14536->14536
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-heap.so` — size 18744->18744
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-iotrace.so` — size 63832->63832
- `/usr/lib/x86_64-linux-gnu/gprofng/libgp-sync.so` — size 26904->26904
- `/usr/lib/x86_64-linux-gnu/libbfd-2.42-system.so` — size 1479888->1479888
- `/usr/lib/x86_64-linux-gnu/libblkid.so.1.1.0` — size 236592->236592
- `/usr/lib/x86_64-linux-gnu/libctf-nobfd.so.0.0.0` — size 216096->216096
- `/usr/lib/x86_64-linux-gnu/libctf.so.0.0.0` — size 220384->220384
- `/usr/lib/x86_64-linux-gnu/libcups.so.2` — size 653416->653416
- `/usr/lib/x86_64-linux-gnu/libfdisk.so.1.1.0` — size 350064->350064
- `/usr/lib/x86_64-linux-gnu/libgprofng.so.0.0.0` — size 2334672->2334672
- `/usr/lib/x86_64-linux-gnu/libmount.so.1.1.0` — size 309960->309960
- `/usr/lib/x86_64-linux-gnu/libopcodes-2.42-system.so` — size 911656->911656
- `/usr/lib/x86_64-linux-gnu/libsframe.so.1.0.0` — size 35168->35168
- `/usr/lib/x86_64-linux-gnu/libsmartcols.so.1.1.0` — size 112792->112792
- `/usr/lib/x86_64-linux-gnu/libsubid.so.4.0.0` — size 41248->41248
- `/usr/lib/x86_64-linux-gnu/libuuid.so.1.3.0` — size 35032->35032

**usr-share** (7)

- `/usr/share/gdb/python/gdb/dap/breakpoint.py` — size 13936->13851
- `/usr/share/gdb/python/gdb/dap/bt.py` — size 5671->5632
- `/usr/share/gdb/python/gdb/dap/disassemble.py` — size 1634->3527
- `/usr/share/gdb/python/gdb/dap/memory.py` — size 1386->1513
- `/usr/share/gdb/python/gdb/dap/sources.py` — size 2990->3137
- `/usr/share/lintian/overrides/php8.4-cli` — size 135->197
- `/usr/share/lintian/overrides/php8.4-common` — size 302->367

## Excluded (expected differences)

- excluded: 624,090
- expected_only_left: 24,923
- expected_only_right: 12,969
- hash_excluded: 785
