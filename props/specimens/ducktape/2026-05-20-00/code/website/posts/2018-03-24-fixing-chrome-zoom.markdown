---
title: When I use my touchpad to scroll and then press Ctrl, Chrome starts zooming in/out. How to fix that?
---

This has been plaguing me for a few days (I just got a new Lenovo X1 Yoga
laptop), and I think I now figured it out. The Synaptics touchpad driver has
a `CoastingSpeed` option (see [man 4 synaptics](http://manpages.ubuntu.com/manpages/xenial/man4/synaptics.4.html)).

The issue is that when I start two-finger scrolling on a webpage, the driver
interprets that to mean "oh and when I take my fingers off the touchpad, please
continue scrolling for a few more seconds in the same direction, while slowing
down until you stop scrolling".

Guess what? When you press Ctrl in Chrome and the touchpad driver keeps sending
the "ooh we're scrolling!" message, Chrome starts changing the font size, which
is what "Ctrl + scroll" does.

Quoting from the manual:

Your finger needs to produce this many scrolls per second in order to start
coasting. The default is 20 which should prevent you from starting coasting
unintentionally. 0 disables coasting. Property: "Synaptics Coasting Speed"

I found a command at [the Ubuntu community wiki article about Synaptics touchpads](https://help.ubuntu.com/community/SynapticsTouchpad)
which disables this: `synclient CoastingSpeed=0`.
But I guess that won't stay between restarts.

I'll add this to a new file in `/etc/X11/xorg.conf.d/99-custom.conf`:

```
Section "InputClass"
  # Disable annoying "zoom after two-finger scroll" in Chrome.
  Identifier "touchpad disable coasting"
  MatchDriver "synaptics"
  Option "CoastingSpeed" "0"
EndSection
```

And I'll report back if that doesn't work. (So if this text is still here, you
can assume it did and I was too lazy to update this post :p)
