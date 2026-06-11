---
title: Making Neovim's color scheme follow system light/dark preference on GNOME
---

GNOME has a feature where there's a system-level setting for whether user
prefers light or dark theme. This got added relatively recently (in [GNOME
42][gnome-42]). Its intended use is so that apps can automatically adjust to match
the user's preferences without hacks. Hacks like "read the current GTK theme
and try to figure out whether it's light or dark based on some heuristic".

Today I've managed to put together one annoying missing piece of personal infra.
I've had `gnome-terminal` sorta-following the setting for a while, and tonight
I've made [Neovim][neovim] also do that. To celebrate, I wanted to share how
this works.

<figure>
<video controls loop autoplay style="max-width: 100%">
<source src="/static/2022-09-02-themes.mp4" type="video/mp4">
</video>
<figcaption>GNOME Terminal and Neovim both following system theme</figcaption>
</figure>

All scripts copied here are in my [ducktape][ducktape] repo in
[`dotfiles/local/bin`][ducktape-local-bin], where you can copy fork and improve
to your heart's content. The Neovim part was added in [MR #81][mr-81].

Those are the dependencies:

```bash
pip install pynvim absl-py dbus-python
```

## Shared setup

### Night Theme Switcher

Install the [Night Theme Switcher][night-theme-switcher] GNOME extension.
This extension lets you attach scripts to when the theme is changed.

### Light/dark scripts

Create a pair of scripts, `set_light_theme` and `set_dark_theme`, put them
wherever. Mine are currently in `~/.local/bin`.

Point Night Theme Switcher to run those when the theme is changed.

## Neovim

In my particular case, I like [Solarized colors][solarized], which I have
everywhere I can (VSCode, Neovim, `gnome-terminal`, even this site - as of now).
I use the [`vim-colors-solarized`][vim-colors-solarized] plugin which adds
both light and dark variants, toggled by `set background=light` or `dark`.

### `init.vim`

Open `~/.config/nvim/init.vim` and add this hunk somewhere near the top.
It'll read the current setting from `gsettings` and update Vim's background to
match.

```vim
" Set color theme to light/dark based on current system preferences.
" Done early to prefer flashing of the wrong theme before this runs.
" Will later be picked up when setting up Solarized colors.
" Called on theme switches by set_light_theme, set_dark_theme scripts.
function! UpdateThemeFromGnome()
  if !executable('gsettings')
    return
  endif

  let color_scheme = system('gsettings get org.gnome.desktop.interface color-scheme')
  " remove newline character from color_scheme
  let color_scheme = substitute(color_scheme, "\n", "", "")
  " Remove quote marks
  let color_scheme = substitute(color_scheme, "'", "", "g")

  if color_scheme == 'prefer-dark'
    set background=dark
  else
    " With disabled night mode, value seems to be to 'default' on my system.
    set background=light
  endif
endfunction

call UpdateThemeFromGnome()
```

### `update_nvim_theme_from_gnome`

Create this script somewhere and `chmod +x` it.
I named it `update_nvim_theme_from_gnome`.
It'll use `pynvim` to connect to running Neovim instances and run the function
we made above to update the background.

```python
#!/usr/bin/python
# Updates the theme on all running Neovim instances.

import glob
import os

from pynvim import attach

# TODO: should probably only try to do this to *my* neovim instances
for dir in glob.glob('/tmp/nvim*'):
    socket = os.path.join(dir, '0')
    nvim = attach("socket", path=socket)
    nvim.command("call UpdateThemeFromGnome()")
```

Update `set_light_theme` and `set_dark_theme` to call it. This will make it so
that when you switch theme, it'll not just affect new Neovim instances, but also
all currently running ones.

There's a TODO in there. Exercise for the reader I guess - I don't particularly
care because I rarely run Neovim as `root`, but I expect this would crash
and burn if there were Neovim running as any user other than you. Cause it would
probably not let you write into that socket.

## GNOME Terminal

I have another script for GNOME Terminal doing something similar.

It assumes that you have a light and dark profile set up. Open GNOME Terminal
preferences and note down the names of the profiles you wanna use in
light/dark configurations

### `switch_gnome_terminal_profile`

Let's call our script `switch_gnome_terminal_profile`:

```python
#!/usr/bin/python
# Works on gnome-terminal 3.44.0 as of 2022-09-03.
# Requirements: absl-py, dbus-python

from typing import List
import json
import re
import subprocess
import ast
import dbus
from xml.etree import ElementTree
from absl import app, flags, logging

_PROFILE = flags.DEFINE_string('profile', None,
                               'Name or UUID of profile to set everywhere')


def gsettings_get_profile_uuid_list() -> List[str]:
    out = subprocess.check_output(
        ["gsettings", "get", "org.gnome.Terminal.ProfilesList",
         "list"]).decode('utf-8')
    return ast.literal_eval(out)


def gsettings_get_default_profile_uuid() -> str:
    out = subprocess.check_output(
        ["gsettings", "get", "org.gnome.Terminal.ProfilesList",
         "default"]).decode('utf-8')
    return ast.literal_eval(out)


def gsettings_set_default_profile_uuid(uuid: str) -> None:
    out = subprocess.check_output([
        "gsettings", "set", "org.gnome.Terminal.ProfilesList", "default",
        f"'{uuid}'"
    ]).decode('utf-8')
    assert out == ''


def dconf_get_profile_visible_name(uuid: str) -> str:
    # As of 2022-09-03 (gnome-terminal 3.44.0), somehow the visible-name only
    # seems to propagate correctly into dconf, not into gsettings...
    # but the list of profiles (ProfileList) is up in gsettings.
    # dconf list /org/gnome/terminal/legacy/profiles:/ returns a lot of profiles
    # which I've deleted a long time back.
    name = subprocess.check_output([
        "dconf", "read",
        f"/org/gnome/terminal/legacy/profiles:/:{uuid}/visible-name"
    ]).decode('utf-8').strip()
    return ast.literal_eval(name)


def dbus_update_profile_on_all_windows(uuid: str) -> None:
    bus = dbus.SessionBus()

    obj = bus.get_object('org.gnome.Terminal', '/org/gnome/Terminal/window')
    iface = dbus.Interface(obj, 'org.freedesktop.DBus.Introspectable')

    tree = ElementTree.fromstring(iface.Introspect())
    windows = [child.attrib['name'] for child in tree if child.tag == 'node']
    logging.info("requesting new uuid: %s", uuid)

    def _get_window_profile_uuid(window_actions_iface):
        # gnome-terminal source code pointer:
        # https://gitlab.gnome.org/GNOME/gnome-terminal/-/blob/f85f2a381e5ba9904d00236e46fc72ae31253ff0/src/terminal-window.cc#L402
        # D-Feet (https://wiki.gnome.org/action/show/Apps/DFeet) is useful for
        # manual poking.
        description = window_actions_iface.Describe('profile')
        profile_uuid = description[2][0]
        return profile_uuid

    for window in windows:
        window_path = f'/org/gnome/Terminal/window/{window}'
        # TODO: if there's other windows open - like Gnome Terminal preferences,
        # About dialog etc. - this will also catch those windows and fail
        # because they do not have the 'profile' action.

        obj = bus.get_object('org.gnome.Terminal', window_path)
        logging.info("talking to: %s", obj)
        window_actions_iface = dbus.Interface(obj, 'org.gtk.Actions')
        logging.info("current uuid: %s",
                     _get_window_profile_uuid(window_actions_iface))
        #res = window_actions_iface.Activate('about', [], [])
        res = window_actions_iface.SetState(
            # https://wiki.gnome.org/Projects/GLib/GApplication/DBusAPI#Overview-2
            # https://gitlab.gnome.org/GNOME/gnome-terminal/-/blob/f85f2a381e5ba9904d00236e46fc72ae31253ff0/src/terminal-window.cc#L2132
            'profile',
            # Requested new state
            # https://gitlab.gnome.org/GNOME/gnome-terminal/-/blob/f85f2a381e5ba9904d00236e46fc72ae31253ff0/src/terminal-window.cc#L1319
            uuid,
            # "Platform data" - `a{sv}`
            [])
        logging.info("window_actions_iface.SetState result: %s", res)
        uuid_after = _get_window_profile_uuid(window_actions_iface)
        logging.info("new uuid: %s", uuid_after)
        assert new_uuid == uuid
        # TODO: this only includes currently active tabs, not background tabs :/


def main(_):
    profile_uuids = set(gsettings_get_profile_uuid_list())
    uuid_by_name = {}
    for uuid in profile_uuids:
        name = dconf_get_profile_visible_name(uuid)
        uuid_by_name[name] = uuid

    if _PROFILE.value in profile_uuids:
        uuid = _PROFILE.value
    elif _PROFILE.value in uuid_by_name:
        uuid = uuid_by_name[_PROFILE.value]
    else:
        raise Exception("No such profile (by UUID or name)")

    gsettings_set_default_profile_uuid(uuid)
    dbus_update_profile_on_all_windows(uuid)


if __name__ == '__main__':
    flags.mark_flag_as_required(_PROFILE.name)
    app.run(main)
```

This script expects a profile name or UUID in `--profile`, and when called,
it'll update GNOME Terminal's settings to have that profile be the default.
That will make any new terminal windows/tabs use that profile.

Then it'll talk to GNOME Terminal over [dbus][dbus] and update the profile
of each window. Unfortunately, this only updates the theme on windows that
are currently active - i.e., not on background tabs. I've not yet figured out
how to fix this - I've looked into [`gnome-terminal`'s source
code][gnome-terminal-source] when I originally wrote the script, and I even
faintly remember reporting this as an issue. Basically that the dbus interface
should be a bit extended. If you know how to fix this, let me know.

Figuring this out took a while. [D-Feet][d-feet] has been useful for it.

<figure>
  <img src="/static/2022-09-02-d-feet.png" alt="Screenshot of using D-Feet to poke GNOME Terminal" style="max-width: 100%">
</figure>

Generally, it's very questionable and broke for me at least once (because of
something having to do with which particular knobs are in `gconf` vs `dconf`
vs `gsettings`). Works for me on gnome-terminal 3.44.0. Caveat emptor.

As with the other scripts in here, it's currently [in my ducktape
repo][ducktape-switch] and if I update it later, it'll be reflected there.

## Putting it together

Just make your `set_light_theme` and `set_dark_theme` scripts call the
appropriate scripts for `gnome-terminal` and Neovim. Here's how they look for
me:

### `set_dark_theme`

```bash
#!/bin/bash
switch_gnome_terminal_profile --profile='Solarized Dark'
update_nvim_theme_from_gnome
```

### `set_light_theme`

```bash
#!/bin/bash
switch_gnome_terminal_profile --profile='Solarized Light'
update_nvim_theme_from_gnome
```

Why is one on `PATH` and not the other? Tech debt in my personal infra.
Deployment step of built artifacts isn't separated and my old scripts repo isn't
yet merged into my [maximally glorious Ducktape monorepo][ducktape]. Sue me :P

Still, over time, I've made it a project to make the duct tape holding together
my computer have a better CI setup than many commercial software projects :P

<figure>
  <!-- TODO: technically a <video>. oh well. -->
  <img src="/static/im-not-proud-of-it.gif" alt="I'm not proud of it. I am a bit." />
</figure>

## Short update

Oh also I'm now in San Francisco and at [OpenAI][openai], working on
reinforcement learning. Long time, much news. Also [Copilot][copilot] is
a thing and has surprised me very strongly by how good and useful it is.
Sometime I'll be writing some sorta summary of last year or two, but today is
not the day and this is not that blogpost.

EDIT (2023-01-16): [How I got to OpenAI](/posts/2023-01-11-how-i-got-to-openai.html) is that blogpost.

Cheers, have a nice long weekend if you're in the US.

[neovim]: https://neovim.io/
[night-theme-switcher]: https://extensions.gnome.org/extension/2236/nightthemeswitcher/
[vim-colors-solarized]: https://github.com/altercation/vim-colors-solarized
[solarized]: https://ethanschoonover.com/solarized/
[dbus]: https://dbus.freedesktop.org/doc/dbus-python/tutorial.html
[gnome-terminal-source]: https://gitlab.gnome.org/GNOME/gnome-terminal/-/tree/master
[copilot]: https://github.com/features/copilot/
[openai]: https://openai.com/
[gnome-42]: https://release.gnome.org/42/
[d-feet]: https://wiki.gnome.org/Apps/DFeet
[ducktape]: https://gitlab.com/agentydragon/ducktape
[ducktape-local-bin]: https://gitlab.com/agentydragon/ducktape/-/tree/main/dotfiles/local/bin
[ducktape-switch]: https://gitlab.com/agentydragon/ducktape/-/blob/main/dotfiles/local/bin/switch_gnome_terminal_profile
[mr-81]: https://gitlab.com/agentydragon/ducktape/-/merge_requests/81
