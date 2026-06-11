---
title: Activating Android USB tethering via adb
---

I have an oldish Android 2.3.6 phone (a Samsung Galaxy Mini). The last few
months, I have been tethering it to my laptop via USB. It's a bit tedious to
manually activate tethering every time I connect the phone.

Luckily, Android contains a utility called `service` (which lives in
`/system/bin/service`). This utility lets you issue calls to Android services
through `adb shell`. You can list all available services by running `service
list`. Apparently, services provide API endpoints identified by a numerical
identifier, and every endpoint accepts some number of arguments. The `service`
utility lets you pass strings or 32-bit integers, like this:

```
service call [service_name] [endpoint_id] s16 [string_arg] i32 [int_arg] ...
```

By Googling around, I found that Android phones have a `connectivity` service,
which is probably implemented as an instance of
[android.net.IConnectivityManager](https://android.googlesource.com/platform/frameworks/base/+/master/core/java/android/net/IConnectivityManager.aidl).
Unfortunately, I couldn't find any API definition that would exactly match
what my phone seemed to have. Maybe the API was updated a few times
between my Android version and the current one?

By blindly probing and looking at `adb logcat`, I found the following
interesting call IDs:

- Numbers 4 and 5 seem to describe some aspects of the available mobile
  networks.
  <pre>
  # service call connectivity 4
  Result: Parcel(
    0x00000000: 00000000 00000001 00000000 00000008 '................'
    0x00000010: 00000006 006f006d 00690062 0065006c '....m.o.b.i.l.e.'
    0x00000020: 00000000 00000005 00530048 00500044 '........H.S.D.P.'
    0x00000030: 00000041 00000009 004f0043 004e004e 'A.......C.O.N.N.'
    0x00000040: 00430045 00450054 00000044 00000009 'E.C.T.E.D.......'
    0x00000050: 004f0043 004e004e 00430045 00450054 'C.O.N.N.E.C.T.E.'
    0x00000060: 00000044 00000000 00000001 00000000 'D...............'
    0x00000070: 00000009 00690073 004c006d 0061006f '....s.i.m.L.o.a.'
    0x00000080: 00650064 00000064 00000008 006e0069 'd.e.d.......i.n.'
    0x00000090: 00650074 006e0072 00740065 00000000 't.e.r.n.e.t.....')
  # (similar for 5)
  </pre>
- 6 is apparently `setWifiEnabled`. Calling `service call connectivity 6
i32 1` calls `setWifiEnabled(true)`. Looks useful.
- 9 is `stopUsingNetworkFeature`.
- 10 is `requestRouteToHost`.
- 13 is `getMobileDataEnabled`.
- 14 is `setMobileDataEnabled`. Calling with `i32 0` turns off mobile data,
  `i32 1` turns mobile data on.
- 15 starts tethering on a network interface. The name of the interface is
  passed as the first string argument.

I aborted my search here, because I found what I came for:
calling `service call connectivity 15 s16 usb0` starts tethering via USB.
So, to get an Android 2.3.6 phone to tether with your PC, you can
call: `adb shell su -c service call connectivity 15 s16 usb0`.
I'll probably turn this snippet into a mini-script.
Unfortunately, you need to use a rooted phone and the `su` part:
otherwise, you'll get back a Parcel with a permission complaint.

And, just as I finished my search, I found
[this interesting blog post](http://ktnr74.blogspot.com/2014/09/calling-android-services-from-adb-shell.html),
where the author describes a better way of getting at the service codes
than wild probing: apparently if you know your Android version and the service
name you want, you can find an AIDL (Android Interface Definition Language)
with the service description on [https://android.googlesource.com/](https://android.googlesource.com/).

YMMV. Cheers.
