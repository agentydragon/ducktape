---
title: It might be interesting to have a realistic planning system
---

# There's many different kinds of things to do

There's a bunch of things I want or need to do, and they have different shapes.

- Some can be _finite sequences of actions_. Thing like this are for example
  "move to a new apartment". You have actions like "get moving boxes",
  "look into moving options", "pack these things into this box". You have
  dependencies like "you can only unpack if you have already moved the boxes to
  the new place", or "you need to order the boxes online before packing them
  up".
  When you finish all the tasks, you are _done_, and you can forget about the
  problem forever.
- Some are _basic needs_, like the need to sleep or the need to get food.
  If I don't get sleep, I am slow, and will eventually fall asleep no matter
  how much I try to stay awake.
  Unlike moving to a new apartment, I will _always_ need to sleep. I can't,
  say, sleep for 24 hours and then go awake for a whole week.
- Some things take a long time and are open-ended, like _learning a language_.
  There will be a time when I will be competent enough, but it will take a lot
  of practice, and cannot be practically modelled as a finite sequence of steps.
- And more advanced needs, like "I want to do something fun" or "I want to
  hang out with friends".

# Productivity methodologies I know about are way too narrow

There's a bunch of methodologies for productivity, but I feel they often only
model a small part of planning. For example, (my interpretation of) GTD puts
everything in a framing of "you have Projects and projects have Actions".
But I don't think that's a good framing for, say, learning a language.

The model of "cost of time" is also wrong. It is a useful heuristic, but the
value you assign to any activity is going to assume perfect elasticity.
For example, you cannot just walk up to your employer and say "I want to work
120 hours per week". No (reasonable) employer will say "yes" to that.
Also, the many things a person wants cannot be converted into a one-dimensional
value. (Footnote: Yeah I know about von Neumann-Morgenstern theorem. But people
are not rational agents, and von Neumann-Morgenstern does not say anything about
how practical will the resulting utility function be to evaluate.)

What I tend to do is some sort of intuitive "higher-level planning" which is
sometimes a bit reflective, but not very often. When I'm in "work mode" (i.e.,
in my actual job), I have a few ways I try to figure out what to do on any
particular day.

But the process by which I decide, say, "enough work today, let's go get some
sleep", or maybe "I'm a bit tired but let's go walk on the treadmill for a
while", is very intuitive.

I don't know about any formal methodology which would basically take an input
like:

- I need to work so I get money and feel good;
- I also need to sleep roughly 8 hours daily;
- I need to have some fun;
- I also want to learn German; and
- I need to do a bunch of multi-step things before their deadline,

and which would output a plan of what I should be doing at any given time.

And I would like the plan to be _reasonable_. The methodology should not, for
example, assume I can do without sleep, or without a regular sleep schedule.

And that's what I mean by a _"realistic planning system"_.

# Maybe good old fashioned AI-style planning could be adapted?

In uni, I studied a bunch of planning and scheduling, which is mostly used for
cases like "this is how you construct a submarine; you can't screw in screw B217
until you screw in screws B210-B216; make a schedule which takes as little time
as possible".

These algorithms can be extended to work in more general environments, like:

- Some people have working hours and when a worker is not working, you can't
  plan jobs for them.
- There's limited resources, like "you need a drill to drill a hole and there
  are only 10 drills; you can't have more than 10 workers drilling at the same
  time".

I wonder if you could make a realistic and formal planning system from some
extension of that setup. Say something like:

- There is a "sleep meter". If sleep meter goes to 0, agent must sleep for
  8 hours. "Sleep meter" slowly goes down during the day.
- There is some sort of penalty on effectiveness when underslept or when sleep
  is irregular.
- Different types of actions deplete other types of meters. Light socialization
  depletes "introvert points" (for me). Snuggles increase "oxytocin meter"
  (which is a dimension of "happiness").
- And there could be some modelling of "if you get too tired you have to
  sleep". Perhaps the algorithm would compete for control with "drive to sleep",
  and the lower the sleep counter is, the higher the likelihood the person
  just falls asleep wherever they are.
- And I think it would be nice if the algorithm treated all "needs"
  symmetrically. According to internal family systems, people have many
  relatively smart parts good at different things and wanting different things.
  If the system is cooperative and does not place any particular part at
  a "command" level or at a "subordinate" level, it would hopefully make it
  easy for parts to agree to collective decisions.

# Meh, too hard. I got other stuff to do

I guess getting any model halfway realistic would be too complicated, and I
probably will keep using my bunch of ad-hoc heuristics to make decisions.
I have way too many things that I want to do to spend the first couple years
developing a planning algorithm rooted in psychological theory.

A thing I used to do that might be useful to start doing again is having some
time in which I try to optimize what I'm doing. It could bootstrap into more
conscious/mindful action.
