# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count   | %        |
| -------------------- | ------- | -------- |
| Identical            | 120,330 | 17.6%    |
| Excluded (expected)  | 561,727 | 82.3%    |
| **Real differences** | **578** | **0.1%** |
| Total                | 682,635 |          |

## Real Differences

### Only in live (43)

**system-libs** (42)

- `/usr/lib/x86_64-linux-gnu/dri/apple_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/armada-drm_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/asahi_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/exynos_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/gm12u320_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/hdlcd_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/hx8357d_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/ili9163_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/ili9225_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/ili9341_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/ili9486_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/imx-dcss_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/imx-drm_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/imx-lcdif_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/ingenic-drm_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/kirin_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/komeda_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/mali-dp_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/mcde_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/mediatek_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/meson_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/mi0283qt_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/mxsfb-drm_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/panel-mipi-dbi_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/pl111_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/rcar-du_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/repaper_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/rockchip_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/rzg2l-du_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/ssd130x_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/st7586_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/st7735r_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/sti_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/stm_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/sun4i-drm_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/udl_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/vkms_dri.so`
- `/usr/lib/x86_64-linux-gnu/dri/zynqmp-dpsub_dri.so`
- `/usr/lib/x86_64-linux-gnu/libdrm.so.2.125.0`
- `/usr/lib/x86_64-linux-gnu/libdrm_amdgpu.so.1.125.0`
- `/usr/lib/x86_64-linux-gnu/libdrm_intel.so.1.125.0`
- `/usr/lib/x86_64-linux-gnu/libgallium-25.2.8-0ubuntu0.24.04.1.so`

**usr-share** (1)

- `/usr/share/fish`

### Only in built (9)

**docs** (3)

- `/usr/share/doc/libwayland-server0`
- `/usr/share/doc/libwayland-server0/changelog.Debian.gz`
- `/usr/share/doc/libwayland-server0/copyright`

**system-libs** (6)

- `/usr/lib/x86_64-linux-gnu/libdrm.so.2.4.0`
- `/usr/lib/x86_64-linux-gnu/libdrm_amdgpu.so.1.0.0`
- `/usr/lib/x86_64-linux-gnu/libdrm_intel.so.1.0.0`
- `/usr/lib/x86_64-linux-gnu/libgallium-25.0.7-0ubuntu0.24.04.2.so`
- `/usr/lib/x86_64-linux-gnu/libwayland-server.so.0`
- `/usr/lib/x86_64-linux-gnu/libwayland-server.so.0.22.0`

### Content changed (hash differs) (523)

**docs** (18)

- `/usr/share/doc/libboost-iostreams1.83.0/changelog.Debian.gz` — size 5505->5420
- `/usr/share/doc/libboost-thread1.83.0/changelog.Debian.gz` — size 5503->5419
- `/usr/share/doc/libc6/changelog.Debian.gz` — size 40474->40271
- `/usr/share/doc/libdrm-common/changelog.Debian.gz` — size 2884->2528
- `/usr/share/doc/libdrm2/changelog.Debian.gz` — size 2881->2525
- `/usr/share/doc/libgbm1/copyright` — size 18288->14847
- `/usr/share/doc/libgl1-mesa-dri/copyright` — size 18288->14847
- `/usr/share/doc/libglib2.0-0t64/changelog.Debian.gz` — size 31285->30357
- `/usr/share/doc/libglib2.0-data/changelog.Debian.gz` — size 31285->30357
- `/usr/share/doc/libglx-mesa0/copyright` — size 18288->14847
- `/usr/share/doc/libldap2/changelog.Debian.gz` — size 14271->13017
- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 2056->1501
- `/usr/share/doc/libssl3t64/changelog.Debian.gz` — size 12356->11757
- `/usr/share/doc/linux-libc-dev/changelog.Debian.gz` — size 639258->531692
- `/usr/share/doc/locales/changelog.Debian.gz` — size 40474->40271
- `/usr/share/doc/mesa-libgallium/changelog.Debian.gz` — size 15362->14209
- `/usr/share/doc/mesa-libgallium/copyright` — size 18288->14847
- `/usr/share/doc/openjdk-21-jre-headless/changelog.Debian.gz` — size 11202->10777

**etc** (3)

- `/etc/java-21-openjdk/jfr/default.jfc` — size 37449->37446
- `/etc/java-21-openjdk/security/java.security` — size 68362->65636
- `/etc/ld.so.cache` — size 34271->34339

**headers** (21)

- `/usr/include/linux/bpf.h` — size 275621->275501
- `/usr/include/linux/falloc.h` — size 3643->3584
- `/usr/include/linux/if_link.h` — size 55825->55797
- `/usr/include/linux/in6.h` — size 7578->7578
- `/usr/include/linux/io_uring.h` — size 19716->19724
- `/usr/include/linux/iommufd.h` — size 25846->25113
- `/usr/include/linux/kfd_ioctl.h` — size 53995->53956
- `/usr/include/linux/landlock.h` — size 9414->9294
- `/usr/include/linux/pci_regs.h` — size 61883->61807
- `/usr/include/linux/pfrut.h` — size 8003->7987
- `/usr/include/linux/types.h` — size 1829->1773
- `/usr/include/linux/vhost.h` — size 11007->9972
- `/usr/include/linux/vm_sockets.h` — size 7428->7354
- `/usr/include/x86_64-linux-gnu/asm/bootparam.h` — size 8549->8547
- `/usr/include/x86_64-linux-gnu/asm/debugreg.h` — size 4024->3329
- `/usr/include/x86_64-linux-gnu/asm/e820.h` — size 2581->2579
- `/usr/include/x86_64-linux-gnu/asm/ldt.h` — size 1308->1306
- `/usr/include/x86_64-linux-gnu/asm/msr.h` — size 348->346
- `/usr/include/x86_64-linux-gnu/asm/ptrace-abi.h` — size 2040->2037
- `/usr/include/x86_64-linux-gnu/asm/ptrace.h` — size 1497->1495
- `/usr/include/x86_64-linux-gnu/asm/signal.h` — size 2050->2046

**java** (146)

- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jar` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jarsigner` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/java` — size 14456->14456
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/javac` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/javadoc` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/javap` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jcmd` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jconsole` — size 14544->14544
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jdb` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jdeprscan` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jdeps` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jfr` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jhsdb` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jimage` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jinfo` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jlink` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jmap` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jmod` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jpackage` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jps` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jrunscript` — size 14520->14520
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jshell` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jstack` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jstat` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jstatd` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/jwebserver` — size 14488->14488
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/keytool` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/rmiregistry` — size 14512->14512
- `/usr/lib/jvm/java-21-openjdk-amd64/bin/serialver` — size 14480->14480
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.base.jmod` — size 24805478->24789683
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.compiler.jmod` — size 132035->132035
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.datatransfer.jmod` — size 59306->59307
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.desktop.jmod` — size 12532585->12530107
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.instrument.jmod` — size 50605->50605
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.logging.jmod` — size 129983->129983
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.management.jmod` — size 900322->900321
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.management.rmi.jmod` — size 98811->98811
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.naming.jmod` — size 482844->482843
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.net.http.jmod` — size 787346->787347
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.prefs.jmod` — size 70264->70264
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.rmi.jmod` — size 273000->272722
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.scripting.jmod` — size 48052->48052
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.se.jmod` — size 9869->9868
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.security.jgss.jmod` — size 598578->598577
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.security.sasl.jmod` — size 89344->89344
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.smartcardio.jmod` — size 62921->62922
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.sql.jmod` — size 83900->83899
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.sql.rowset.jmod` — size 221367->221367
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.transaction.xa.jmod` — size 11681->11680
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.xml.crypto.jmod` — size 707297->707296
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/java.xml.jmod` — size 5228454->5228447
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.accessibility.jmod` — size 57679->57679
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.attach.jmod` — size 38878->38929
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.charsets.jmod` — size 1248674->1248672
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.compiler.jmod` — size 10902139->10902055
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.crypto.cryptoki.jmod` — size 421023->421032
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.crypto.ec.jmod` — size 146481->146480
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.dynalink.jmod` — size 163990->163991
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.editpad.jmod` — size 15304->15305
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.hotspot.agent.jmod` — size 2268070->2268068
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.httpserver.jmod` — size 166302->164400
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.incubator.vector.jmod` — size 1167444->1167443
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.ed.jmod` — size 15167->15167
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.jvmstat.jmod` — size 96060->96052
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.le.jmod` — size 487205->487207
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.opt.jmod` — size 95611->95612
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.vm.ci.jmod` — size 501977->501976
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.vm.compiler.jmod` — size 9655->9654
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.internal.vm.compiler.management.jmod` — size 9660->9660
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jartool.jmod` — size 283171->280200
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.javadoc.jmod` — size 1584427->1584428
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jcmd.jmod` — size 139537->139534
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jconsole.jmod` — size 484801->484802
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jdeps.jmod` — size 760655->760657
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jdi.jmod` — size 877060->877061
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jdwp.agent.jmod` — size 155147->155147
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jfr.jmod` — size 880917->880908
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jlink.jmod` — size 456258->456254
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jpackage.jmod` — size 365175->365171
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jshell.jmod` — size 768172->768170
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jsobject.jmod` — size 10749->10753
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.jstatd.jmod` — size 37464->37465
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.localedata.jmod` — size 11949324->11949321
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.management.agent.jmod` — size 96580->96580
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.management.jfr.jmod` — size 62161->62160
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.management.jmod` — size 80780->80779
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.naming.dns.jmod` — size 72508->72509
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.naming.rmi.jmod` — size 31007->31007
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.net.jmod` — size 32373->32373
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.nio.mapmode.jmod` — size 10221->10220
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.random.jmod` — size 29475->29475
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.sctp.jmod` — size 93313->93313
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.security.auth.jmod` — size 75031->75031
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.security.jgss.jmod` — size 32879->32879
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.unsupported.desktop.jmod` — size 21751->21751
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.unsupported.jmod` — size 25230->25231
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.xml.dom.jmod` — size 49964->49964
- `/usr/lib/jvm/java-21-openjdk-amd64/jmods/jdk.zipfs.jmod` — size 112490->112491
- `/usr/lib/jvm/java-21-openjdk-amd64/lib/classlist` — size 77360->77384
- `/usr/lib/jvm/java-21-openjdk-amd64/lib/ct.sym` — size 10663370->10663370
- _...and 46 more_

**system-binaries** (22)

- `/usr/bin/gapplication` — size 22920->22920
- `/usr/bin/gdbus` — size 51592->51592
- `/usr/bin/gencat` — size 27072->27072
- `/usr/bin/getconf` — size 26992->26992
- `/usr/bin/getent` — size 39648->39648
- `/usr/bin/gio` — size 104856->104856
- `/usr/bin/glib-compile-resources` — size 51520->51520
- `/usr/bin/gobject-query` — size 14656->14656
- `/usr/bin/gresource` — size 22840->22840
- `/usr/bin/gsettings` — size 31032->31032
- `/usr/bin/gtester` — size 31056->31056
- `/usr/bin/iconv` — size 68072->68072
- `/usr/bin/ldd` — size 5382->5382
- `/usr/bin/locale` — size 50824->50824
- `/usr/bin/localedef` — size 326752->326752
- `/usr/bin/openssl` — size 1005368->1005368
- `/usr/bin/pldd` — size 22976->22976
- `/usr/bin/tzselect` — size 15378->15378
- `/usr/bin/zdump` — size 31008->31008
- `/usr/sbin/iconvconfig` — size 35296->35296
- `/usr/sbin/ldconfig.real` — size 1051280->1051280
- `/usr/sbin/zic` — size 67984->67984

**system-libs** (308)

- `/usr/lib/x86_64-linux-gnu/audit/sotruss-lib.so` — size 14544->14544
- `/usr/lib/x86_64-linux-gnu/dri/libdril_dri.so` — size 117064->108872
- `/usr/lib/x86_64-linux-gnu/engines-3/afalg.so` — size 22976->22976
- `/usr/lib/x86_64-linux-gnu/engines-3/loader_attic.so` — size 56192->56192
- `/usr/lib/x86_64-linux-gnu/engines-3/padlock.so` — size 26848->26848
- `/usr/lib/x86_64-linux-gnu/gbm/dri_gbm.so` — size 56424->56424
- `/usr/lib/x86_64-linux-gnu/gconv/ANSI_X3.110.so` — size 30928->30928
- `/usr/lib/x86_64-linux-gnu/gconv/ARMSCII-8.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/ASMO_449.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/BIG5.so` — size 96464->96464
- `/usr/lib/x86_64-linux-gnu/gconv/BIG5HKSCS.so` — size 243920->243920
- `/usr/lib/x86_64-linux-gnu/gconv/BRF.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP10007.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1125.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1250.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1251.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1252.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1253.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1254.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1255.so` — size 22736->22736
- `/usr/lib/x86_64-linux-gnu/gconv/CP1256.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1257.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP1258.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP737.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP770.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP771.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP772.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP773.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP774.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP775.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CP932.so` — size 104656->104656
- `/usr/lib/x86_64-linux-gnu/gconv/CSN_369103.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/CWI.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/DEC-MCS.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-AT-DE-A.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-AT-DE.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-CA-FR.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-DK-NO-A.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-DK-NO.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-ES-A.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-ES-S.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-ES.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-FI-SE-A.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-FI-SE.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-FR.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-IS-FRISS.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-IT.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-PT.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-UK.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EBCDIC-US.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/ECMA-CYRILLIC.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EUC-CN.so` — size 26832->26832
- `/usr/lib/x86_64-linux-gnu/gconv/EUC-JISX0213.so` — size 22736->22736
- `/usr/lib/x86_64-linux-gnu/gconv/EUC-JP-MS.so` — size 92368->92368
- `/usr/lib/x86_64-linux-gnu/gconv/EUC-JP.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EUC-KR.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/EUC-TW.so` — size 35024->35024
- `/usr/lib/x86_64-linux-gnu/gconv/GB18030.so` — size 182480->182480
- `/usr/lib/x86_64-linux-gnu/gconv/GBBIG5.so` — size 63696->63696
- `/usr/lib/x86_64-linux-gnu/gconv/GBGBK.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/GBK.so` — size 129232->129232
- `/usr/lib/x86_64-linux-gnu/gconv/GEORGIAN-ACADEMY.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/GEORGIAN-PS.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/GOST_19768-74.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/GREEK-CCITT.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/GREEK7-OLD.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/GREEK7.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/HP-GREEK8.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/HP-ROMAN8.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/HP-ROMAN9.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/HP-THAI8.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/HP-TURKISH8.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM037.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM038.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1004.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1008.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1008_420.so` — size 14544->14544
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1025.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1026.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1046.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1047.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1097.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1112.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1122.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1123.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1124.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1129.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1130.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1132.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1133.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1137.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1140.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1141.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1142.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1143.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1144.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1145.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1146.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1147.so` — size 18640->18640
- `/usr/lib/x86_64-linux-gnu/gconv/IBM1148.so` — size 18640->18640
- _...and 208 more_

**usr-local** (1)

- `/usr/local/bin/check-tools` — size 8428->7912

**usr-share** (4)

- `/usr/share/drirc.d/00-mesa-defaults.conf` — size 69597->74857
- `/usr/share/libdrm/amdgpu.ids` — size 23250->19045
- `/usr/share/lintian/overrides/openjdk-21-jre` — size 400->372
- `/usr/share/lintian/overrides/openjdk-21-jre-headless` — size 887->706

### Symlink target changed (3)

**system-libs** (3)

- `/usr/lib/x86_64-linux-gnu/libdrm.so.2` — libdrm.so.2.125.0->libdrm.so.2.4.0
- `/usr/lib/x86_64-linux-gnu/libdrm_amdgpu.so.1` — libdrm_amdgpu.so.1.125.0->libdrm_amdgpu.so.1.0.0
- `/usr/lib/x86_64-linux-gnu/libdrm_intel.so.1` — libdrm_intel.so.1.125.0->libdrm_intel.so.1.0.0

## Excluded (expected differences)

- excluded: 523,735
- expected_only_left: 24,460
- expected_only_right: 12,908
- hash_excluded: 624

## Exclusion Pattern Utilization

138 patterns excluded 561,727 paths (561,727 attributed to specific patterns). 17 patterns matched 0 paths.
Ratio: 0.2x patterns per real diff.

### `skip_paths` (36 patterns, 523,735 hits, 4 unused)

|    Hits | Pattern                              |
| ------: | ------------------------------------ |
| 305,082 | `/tmp`                               |
| 122,748 | `/root/.cache`                       |
|  43,824 | `/mnt`                               |
|  21,995 | `/proc`                              |
|  18,039 | `/home/user/ducktape`                |
|   6,206 | `/root/.npm`                         |
|   2,889 | `/var/lib/dpkg/info`                 |
|   1,724 | `/root/.local/share/virtualenv`      |
|     775 | `/usr/lib/debug/.build-id`           |
|     176 | `/sys`                               |
|     123 | `/home/claude/.npm`                  |
|      40 | `/run`                               |
|      30 | `/var/lib/apt/lists`                 |
|      19 | `/var/log`                           |
|      18 | `/dev`                               |
|      14 | `/root/.claude/projects`             |
|       5 | `/root/.claude/session-env`          |
|       4 | `/var/cache/apt`                     |
|       3 | `/root/.claude/debug`                |
|       3 | `/root/.claude/todos`                |
|       3 | `/var/lib/containers`                |
|       2 | `/etc/containerd`                    |
|       2 | `/home/claude/.claude/remote`        |
|       2 | `/home/claude/.ssh`                  |
|       2 | `/root/.claude/shell-snapshots`      |
|       1 | `/etc/default/docker`                |
|       1 | `/etc/docker`                        |
|       1 | `/etc/init.d/docker`                 |
|       1 | `/home/claude/.cache`                |
|       1 | `/root/.claude/plans`                |
|       1 | `/root/.local/share/pnpm`            |
|       1 | `/var/tmp`                           |
|       0 | `/nix` **UNUSED**                    |
|       0 | `/root/.claude/plugins` **UNUSED**   |
|       0 | `/root/.claude/statsig` **UNUSED**   |
|       0 | `/root/.claude/telemetry` **UNUSED** |

### `volatile_paths` (44 patterns, 37,866 hits, 3 unused)

|   Hits | Pattern                                   |
| -----: | ----------------------------------------- |
| 22,093 | `/root/.local/share/uv/**`                |
|  8,701 | `/opt/ruby-*`                             |
|  2,903 | `/usr/local/lib/python*/**`               |
|  1,824 | `/opt/rbenv/**`                           |
|  1,242 | `/root/.local/lib/python*/**`             |
|    909 | `**/__pycache__/**`                       |
|     99 | `/opt/nvm/**`                             |
|     19 | `/var/cache/fontconfig/**`                |
|     17 | `/root/.rustup/**`                        |
|      8 | `/opt/node*/**`                           |
|      6 | `/root/.local/share/gem/**`               |
|      6 | `/var/lib/systemd/**`                     |
|      4 | `/var/cache/debconf/**`                   |
|      4 | `/var/lib/dpkg/alternatives/**`           |
|      3 | `/var/lib/postgresql/**`                  |
|      2 | `/root/.local/bin/*`                      |
|      2 | `/usr/local/use-go-*.sh`                  |
|      1 | `/etc/group`                              |
|      1 | `/etc/group-`                             |
|      1 | `/etc/gshadow`                            |
|      1 | `/etc/gshadow-`                           |
|      1 | `/etc/hostname`                           |
|      1 | `/etc/hosts`                              |
|      1 | `/etc/machine-id`                         |
|      1 | `/etc/passwd`                             |
|      1 | `/etc/passwd-`                            |
|      1 | `/etc/postgresql/**`                      |
|      1 | `/etc/shadow`                             |
|      1 | `/etc/shadow-`                            |
|      1 | `/etc/ssl/certs/java/cacerts`             |
|      1 | `/etc/ssl/certs/ssl-cert-snakeoil.pem`    |
|      1 | `/etc/ssl/private/ssl-cert-snakeoil.key`  |
|      1 | `/etc/sudoers`                            |
|      1 | `/root/.wget-hsts`                        |
|      1 | `/usr/local/bin/composer`                 |
|      1 | `/var/cache/ldconfig/**`                  |
|      1 | `/var/lib/apt/extended_states`            |
|      1 | `/var/lib/dbus/machine-id`                |
|      1 | `/var/lib/dpkg/status`                    |
|      1 | `/var/lib/dpkg/status-old`                |
|      1 | `/var/lib/dpkg/triggers/**`               |
|      0 | `**/__pycache__` **UNUSED**               |
|      0 | `/usr/local/bin/golangci-lint` **UNUSED** |
|      0 | `/var/lib/sgml-base/**` **UNUSED**        |

### `only_in_live` (47 patterns, 120 hits, 5 unused)

| Hits | Pattern                                         |
| ---: | ----------------------------------------------- |
|   30 | `/root/.config/**`                              |
|   18 | `/root/.gradle/**`                              |
|    8 | `/root/.launchpadlib/**`                        |
|    8 | `/usr/share/doc/docker-*`                       |
|    7 | `/etc/rc*.d/*docker`                            |
|    5 | `/root/.claude.json.backup*`                    |
|    4 | `/usr/libexec/docker/**`                        |
|    2 | `/etc/systemd/system/*/docker.*`                |
|    2 | `/root/.local/state/**`                         |
|    2 | `/usr/lib/systemd/system/docker.*`              |
|    2 | `/usr/share/doc/containerd.io/**`               |
|    2 | `/usr/share/fish/**`                            |
|    1 | `/.dockerenv`                                   |
|    1 | `/container_info.json`                          |
|    1 | `/etc/alternatives/python`                      |
|    1 | `/etc/apt/keyrings/docker.asc`                  |
|    1 | `/etc/apt/sources.list`                         |
|    1 | `/etc/apt/sources.list.d/docker.list`           |
|    1 | `/etc/apt/sources.list.d/ubuntu.sources`        |
|    1 | `/etc/containers/networks`                      |
|    1 | `/etc/ssl/certs/*.0`                            |
|    1 | `/etc/systemd/system/*/containerd.service`      |
|    1 | `/root/.bazelrc`                                |
|    1 | `/root/.claude.json`                            |
|    1 | `/root/.claude/stop-hook-git-check.sh`          |
|    1 | `/root/.gradle`                                 |
|    1 | `/root/.launchpadlib`                           |
|    1 | `/root/.local/state`                            |
|    1 | `/usr/bin/containerd`                           |
|    1 | `/usr/bin/containerd-shim-runc-v2`              |
|    1 | `/usr/bin/ctr`                                  |
|    1 | `/usr/bin/docker`                               |
|    1 | `/usr/bin/docker-proxy`                         |
|    1 | `/usr/bin/dockerd`                              |
|    1 | `/usr/bin/python`                               |
|    1 | `/usr/bin/runc`                                 |
|    1 | `/usr/lib/systemd/system/containerd.service`    |
|    1 | `/usr/libexec/docker`                           |
|    1 | `/usr/share/bash-completion/completions/docker` |
|    1 | `/usr/share/doc/containerd.io`                  |
|    1 | `/usr/share/zsh/vendor-completions/_docker`     |
|    1 | `/var/lib/dpkg/alternatives/python`             |
|    0 | `/root/.claude/stats-cache.json` **UNUSED**     |
|    0 | `/usr/share/doc/docker-*/**` **UNUSED**         |
|    0 | `/var/cache/containers` **UNUSED**              |
|    0 | `/var/cache/containers/**` **UNUSED**           |
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
