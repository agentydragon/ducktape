---
title: HackCambridge 2016
---

Last weekend, I went to [HackCambridge](https://hackcambridge.com) with
[Petr](http://hudecekpetr.cz) to hack on things. Some things could be organized
a bit better (like the WiFi at the main venue being hilariously broken), but we
ended up having a lot of fun. Luckily, a bunch of my homies from Dropbox
also happen to study in Cambridge and it was great to catch up with them.

We built a cool game. It's called [_The Deadlock Empire_](https://deadlockempire.github.io)
and it lets you _slay dragons and learn concurrency_ - what could me more
awesome?!?!

Just look at this presentation!

<div style="text-align: center;">
<iframe width="560" height="315" src="https://www.youtube.com/embed/eZp1qSF06uM?start=2847" frameborder="0" allowfullscreen></iframe>
</div>

It's at [https://deadlockempire.github.io](https://deadlockempire.github.io).
You can laugh at our poor coding practices on GitHub ([deadlockempire/deadlockempire.github.io](https://github.com/deadlockempire/deadlockempire.github.io)).

The Deadlock Empire has a sequence of challenges for practicing concurrent
thinking. Each challenge gives you several threads, each running their own
source code in C#, and your objective is to play _the mischievous Scheduler_:
take seemingly-innocent parallel code and run it so that it breaks.

You can step each thread line-by-line (and undo your steps), and some
non-atomic instructions can be expanded into atomic parts (i.e., `i = i + 1`
translates to a separate load and store). Sometimes, you have to do this to
properly crack the challenges, like in
[Tutorial 2: Non-Atomic Instructions](http://deadlockempire.github.io/#T2-Expansion).

If you feel bored by how simple the first challenges are, scroll down
a bit. The _Deadlock Empire_ quest at the bottom should keep you busy for a
while :)

I think it took us around 18 hours of actual work to write _The Deadlock Empire_:
both of us had a bit of shut-eye early morning and we didn't have enough time
by the end to start working on the really ambitious features, so the last few
hours were just a commit here and there, fixing up a font or an obscure bug.

There aren't many shiny technologies: it's plain HTML5 and JavaScript
with jQuery, Bootstrap, and a few other things. The game has a simple
"internal virtual machine" that executes simple instructions, like "increment
this global variable", "lock this lock and block this thread", and so on.
We used pseudo-C# as the challenge language mostly because of our familiarity
with its ideas about concurrency, but it would be straightforward to write in
pseudo-Java instead.

In the upcoming days, we'll add more synchronization primitives and polish the
experience. We want this game to be more fun, educational and useful.

I now have a favorite bookstore. Next door from their regular store, Cambridge
University Press sells their damaged books. (Even through the only damage
I found on them was the red 'DAMAGED' stamp at the front page.) Awesome books
on advanced mathematics and CS, philosophy, sociology, biology. For 3 pounds
a paperback or 7 pounds a hardcover. People grab them by the stack. Note to
self: bring big suitcase next time.

Also:

<div style="font-weight: bold;">
WE WON THE HACKATHON!!! HOOORAY!!!!1111
</div>

We also won the _Best Hack in Gaming/VR_ award (judged by Improbable) and
_Bloomberg's Favorite Hack_.
