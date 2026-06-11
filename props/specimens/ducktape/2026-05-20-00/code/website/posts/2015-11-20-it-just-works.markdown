---
title: It Just Works
---

Dropbox's unofficial motto is "It Just Works". Over the last year or so, I've
started seeing the benefits of this maxim. 14 months ago, I started a half-year
internship at Google Paris, where I worked with a very nice set of tools that
allowed me to focus more on doing and less on setup.
Since then, I tried to get the same thing in the way I operate, and I like the
results a lot so far.

Some examples include:

## Mail

In high school, I had a lot of free time that I spent hacking on stuff.
One of the things I did was finely tuning my environment and building and
maintaining my own "infrastructure".

One day when I was probably like 16-ish or so, I grabbed the domain
`rny.cz`, which can nicely spell out my last name and my country's code:
`poko.rny.cz`. I maintained my own VPS there with some HTTP server to host
my websites, a few Rails apps here and there, a mail server and some
miscellanious experiments. I started using a cool email address: `pok@rny.cz`.

What I didn't realize back then was that maintaining a mail server is a lot of
work. Sometimes your mails aren't delivered to their destination, and it's up
to you to figure out why. Sometimes things suddenly break when you upgrade some
packages. Sometimes your entire VPS just gets randomly bricked by some freak
accident and you need to get it running again.

This is even worse when you maintain the server where you get your own mail.
If your mail server needs to stop working altogether, you are effectively deaf
and mute to any mail communication, including any errors your mail server
would otherwise be sending you. If you for some reason mess up and your
mail stops working, you have no way to e.g. recover passwords you forgot.
For this reason, you must avoid cyclical dependences, like registering with
your DNS provider using an address that points to the provider. Or, in general,
any service you need to maintain your server. Suddenly, you must have at least
two mail addresses. You give a chunk of your future free time to maintaining
the setup.

Hosting your server also has some pros. I was happy I had all my mail downloaded
on my laptop, so I could easily access it while offline. I've become proficient
at using `mutt`. I set up my own mail notifications, which looked and behaved
exactly like I dreamt up. My server has much more storage than usual
services offer for free (around 500 gigs). You are your own master and you
know nobody's mining your mails for ad targetting (barring possibly your
hosting company and highly motivated hostiles).

Only now I'm in university, I work and I'm trying to get rid of computer
things poisoning my free time. I decided I want to get rid of stupid server
maintenance. About a month ago, I partially switched to Google's Gmail.

I asked Gmail to send my mail through my server and to automatically download
any new mail from my server. Effectively, I added Gmail between my computer
and my server. The initial download of my mail took several days to finish
and I had to do a bunch of figuring out how to do it properly, but I think it
was well worth it.

Today, I mostly access my mail through Gmail. Gmail is (effectively) always
up without me kicking it every now and then. It searches better than `mutt`.
I can now look at my mails without my laptop (yeah, I could also get my
own server to do that, but that would only give me more work).
And one benefit I did not expect: _it also works on my phone_! That's so good!
(I used to be pretty skeptical about the whole "mobile first" thing, but I
quickly changed my opinion when I got a phone with Android 4.)

There's probably just one thing I'm not happy about: I still need to maintain
my server, because Gmail still sends and receives mail through it. I'm not sure
how I could get rid of this annoyance. I need to keep my old address working:
I'm registered everywhere with it and many services don't let you change your
mail. Google used to offer a free tier of Google Apps for your own domain.
Unfortunately, now the lowest tier is $cheap per month. $cheap is acceptable,
but I don't like to impose obligations on my future self. For now, I just
started to use my Gmail address for registrations so they work even if I decide
to get rid of `pok@rny.cz` at some point.
This is an issue only I brought upon myself.

## Calendar

I have a `NOTES.txt` file. This file is usually around 80 TODO's, random notes,
appointments, observations, and so on. Until a few months ago, I stored where
and when do I need to go in there.

Once again, this has certain benefits. It's just text, so it's easy to work
with. It works offline. It makes some of your fellow communist Linux hackers
jealous.

I switched to Google Calendar. It works on my phone, and it's more pleasant
to use than when I automatically synced my `NOTES.txt` file to my HTTP server
and used a browser to look at it on my phone. It has notifications. Across
devices. And Google Now tells you when do you need to leave for your
appointments and how can you get there. You can efficiently search.
Visualization is really good. Another very useful feature is using showing
external calendars. Think holidays, your band, your soccer team, or your
university schedule.

I'm not looking back.

## Storing stuff

I have a lot of files. I keep backups of my old work, any school or study stuff,
photos and videos, and tons of music. And Bitcoin wallets. Backups of old
backup DVDs I burnt in grade 8 for nostalgia's sake. I don't want to lose
them when I crush my laptop with my falling body (again).

In high school, the obvious way to do this was to just cron up a script
to `rsync` everything. It worked and I was happy to tweak it to do exactly
what I wanted. It only uploaded deltas, so the backups weren't usually too
upload-hungry. It also backed up my `/etc`, a list of installed packages on my
system (so I know all I need to install when restoring the system), and some
other stuff.

One horrible issue was handling deletions. I wanted to be able to delete
some files from my computer and only keep them on the server, like large old
backups. No problem: just ask `rsync` not to delete files when syncing.
But: what if I do actually want to delete the file?
After several years of using this system without resolving this issue,
my backups became very dirty. They contained a lot of Vim swapfiles, useless
files, executables, object files and outputs for source code, and so on.
When restoring from backups, I had to partially manually delete those to get
a restored backup with acceptable amounts of junk.

Employees at Dropbox get free space as a perk. During my internship, I set
my quota absurdly high. I moved all my files to Dropbox and I'm very happy
about it.

Dropbox has search. Dropbox is available everywhere, even when you're on a
Windows machine and can't be bothered to install `ssh`, `rsync` and what have
you. Dropbox allows excluding paths from syncing, which corresponds to my "I
want to keep this file, but not on this machine" use case. Dropbox has more
nines in uptime than I could ever hope for. It has a mobile client!

Dropbox is not perfect. Dropbox's Linux client is a pain to use when you don't
have a system tray - you can't get to the GUI. You have to spend a lot of time
making the initial backup (which is no fault of Dropbox's).

## More?

There are more things I may want to simplify one day, but where I didn't find
the switch beneficial enough yet.

Through I threw out the appointments, I still have my `NOTES.txt` file.
One possible replacement might be Google Keep, but I don't like the desktop
GUI too much and it takes more clicks to add a new note - right now, I just
add a note using an XMonad `appendFilePrompt` bound to a keyboard shortcut.

In my 3rd year of high school (oh my god, that's four years ago...),
I started using XMonad. It's glorious in many ways: speed, amount of
customization possible, stability and geekiness factor.
On the other hand, many programs don't play well with it - especially Java GUIs
and ssh X-forwarded applications sometimes get confused and render nothing.
Full-screen games in Wine interact weirdly with a tiling WM. Many packages
assume that every system has a system tray, so you can't interact well with
them when their tray icons don't render anywhere (examples:
NetworkManager's applet, Dropbox, Skype).
I'm thinking it might be worth it to instead stop being a hipster and start
using Gnome or KDE like everybody else.

I have an encrypted partition. It's possible to use the computer without
mounting it, but I keep sensitive data there, like my SSH keys, confidental
info, Bitcoin wallets, and so on. My original idea was having two scripts:
`cryptomount` and `cryptounmount`. The first one prompts for a password,
mounts the partition and starts services that depend on it, and the latter
one does the inverse. Unfortunately, over time I started using the partition
more and more, so I had it mounted most of the time. I got sloppy and the
system became less usable without having the partition mounted. Programs get
confused when their configuration is in a broken symlink pointing to an
unmounted partition.
I'm thinking about full-disk encryption. It's a bit slower, but just as secure
and it would make my set of personal scripts much simpler. It also
wouldn't allow a mode of operation where your computer is usable, but
anything sensitive is unavailable.
Changing the partition layout of my laptop and encrypting it would also
probably need at least a day of my time. I'm not sure if it's worth it.

I'm thinking about installing Windows into dual-boot again. It's easier
to run Starcraft smoothly. When I need to develop some C# in Visual Studio,
I wouldn't need any silly VMs or remote desktops.
However, same as with the encrypted partition, I'm not as fond of repartitioning
and installing as I was in high school.

<hr />

Overall, I lost some customizations, but I gained a lot in value that I would
never get around to implementing myself. I rescued a lot of future free time
(ok, it took me some time to do this migration, but I hope I rescued more).

Today, I say it's generally better to use a generic proven solution than build
one that best fits your current wants. The best way to fix something might not
be to throw more work at it - maybe you could just settle for a bit less and
save your energy for something more interesting.
You shouldn't enjoy maintenance of your things: it should be an enemy to be
vanquished by automation or having someone else do it. Maintaining things
you need for your life just creates a self-reinforcing cycle of work.
