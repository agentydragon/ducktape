---
title: How I got to OpenAI
date: 2023-01-11
slug: "2023-01-11-how-i-got-to-openai"
---

In May 2022, I started working at OpenAI as a resident on RL team, and
eventually I got hired as full-time. As of today, I'm still at OpenAI, and I'm
working on some ChatGPT related stuff and try to make progress on scalable
alignment. I want to make a brief write-up of how this happened, because
sometimes people ask about it and it might be helpful to other people in a
similar situation.

TL;DR, this is the last couple steps:

- Repeat a couple of times:
  - Make a list of stuff that's relevant to ML I don't yet grok.
  - Find resources to grok it. Some examples:
    - I consumed all the TensorFlow tutorials that don't go to too niche topics (though today I'd do PyTorch).
      And of course made [tons of Anki cards](https://agentydragon.com/posts/2019-11-25-my-anki-patterns.html).
      Contact me if you want some of them.
    - [Deep Learning specialization on Coursera](https://www.coursera.org/specializations/deep-learning)
    - [DeepLearning.AI TensorFlow Developer Professional Certificate on Coursera](https://www.coursera.org/professional-certificates/tensorflow-in-practice)
    - [Reinforcement Learning Specialization on Coursera](https://www.coursera.org/specializations/reinforcement-learning) (from Martha and Adam White of University of Alberta)
    - For RL, I did a course on Coursera
  - Learn all I can from those resources, collect stuff I don't yet
    understand, find references (e.g., papers I wanna read sometime etc.).
- Read up on recent papers.
- Read [Spinning Up in Deep RL](https://spinningup.openai.com/en/latest/)
  and follow up on the references.
- Implement some basic RL and ML experiments - e.g., lunar lander, [half-cheetah](https://agentydragon.com/posts/2021-10-18-rai-ml-mistakes-3.html).
- Take part in [AI Safety Camp](https://aisafety.camp/).
- Go to [Zurich AI alignment](https://www.zurich-ai-alignment.com/) meetups.
- Take part in an [AGI Safety
  Fundamentals](https://www.agisafetyfundamentals.com/ai-alignment-curriculum)
  course.
- Mail the author of one paper I read about some typos/errors I found in the
  paper.
- When I feel ready-ish, keep on lookout for roles:
  - [OpenAI residency](https://openai.com/blog/openai-residency/)
  - [CHAI internship](https://humancompatible.ai/news/2020/11/12/internship-applications/)
  - [DeepMind](https://www.deepmind.com/)
  - [Anthropic](https://www.anthropic.com/)
  - [Redwood Research](https://www.redwoodresearch.org/)
  - [Machine Intelligence Research Institute](https://intelligence.org/)
  - [Cohere](https://cohere.ai/)
  - [Generally Intelligent](https://generallyintelligent.com/)
  - [Ought](https://ought.org/).
- Eventually, via reference from the author, apply to OpenAI residency,
  take the interviews, be super nervous, get hired :)

My longer relevant history follows.

## My history

### Before university

<figure>
<img src="/static/2023-01-11-computer.png" style="max-width: 8cm">
<figcaption>Rai sticker, by
<a href="https://www.furaffinity.net/user/pareldraakje/">ParelDraakje</a></figcaption>
</figure>

I've always been good with math and computers. (I started out as a more general
hacker, including electronics etc., but eventually specialized.)

I did some competitive programming, high school programming seminars, olympiads
etc. I got to district level rounds, and my top achievements were being ~#20-30
in competitive "algorithmic programming" (which is part of math olympiad in
Czech Republic), and being #1 in competitive "practical programming" (which is
more "write a program for task X" rather than "solve this theoretical problem".

### Bachelor's

I took a wide approach to consume all the knowledge at uni. Since high school
I've been freelancing, writing up small programs/websites. I leveraged that into
a series of internships - first Represent.com in Prague (via [Juraj
Masar](https://www.jurajmasar.com/), who
was in roughly the same uni year), then half a year for Google in Paris, then
Dropbox.

When in USA on the Dropbox internship, I got an offer from
[Cruise](https://getcruise.com/) and [Coinbase](https://www.coinbase.com/)
via [Triplebyte](https://triplebyte.com/). I also did full-time interviews with
Google and got an offer.

It turned out I was not mentally/spiritually ready for making the move to USA.
I continued on with a master's, keeping the Google offer in my back pocket.

### Master's

It was easy to do all the required courses but I ended up getting stuck on the
last master's requirement - the thesis.
I tried to work on knowledge base completion with
[Pasky](https://github.com/pasky) as my supervisor (who ended up (co-?)founding
[Rossum](https://rossum.ai/) where he's currently CTO).
This was roughly at the time when the seminal Transformers paper
[Attention is All You Need](https://arxiv.org/abs/1706.03762), maybe +/- half
a year or so.

I got involved in the rationality community in Prague and the EA community
that eventually sprouted out of it. I co-founded the
[Czech EA association](https://efektivni-altruismus.cz/).

I wanted to do an open-source re-implementation of [Google's Knowledge
Vault](https://research.google/pubs/pub45634/).
It's basically where you take Google's Knowledge Graph, you mix in a bunch of
unstructured data like Wikipedia or such, and based on that and some neural
networks trained on the graph, you make a bigger graph, where you suggest "I think there's relation R between entities X and Y, with probability P=0.97".

Coming from Google, I assumed this would be pretty easy - you'd plug in standard
building blocks for MapReduce-type pipelines, and out would come a model.
I was trying to immediately jump to a larger scale project - completing the
[DBpedia](https://www.dbpedia.org/) knowledge graph with Wikipedia text.

Things turned out way harder than I expected. I tried to use Bazel to build
Hadoop stuff to chew up Wikipedia articles, and HBase (Hadoop's BigTable
basically) to store Wikipedia articles. I ran into tons of stupid problems -
like "oops, library X which I need and library Y which I need depend on versions
of library Z which are incompatible, so you gotta split these 2 functions into
separate binaries". Or "oops maximum runtime on this cluster is 24 hours lol".
The iteration time was horrible.

<figure>
<img src="/static/2023-01-11-laptop-ded.png" style="max-width: 8cm">
<figcaption>Rai sticker, by
<a href="https://www.furaffinity.net/user/ketzel99/">Ketzel99</a>.<br>It doesn't
have a name but I think "computers were a mistake" would fit.</figcaption>
</figure>

Today I'd probably try to approach this with less hubris, and try to start from
smaller pieces, so that I'd have a fast iteration turn-out.
And also, I'd just ... drop all the manual NLP and throw a Transformer at the
problem. But oh well, you live and learn.

I was working in the same office as a group of CUNI folks who
ended up winning an Amazon tournament for writing a chit-chat agent, but I
wasn't working in their ecosystem - the knowledge base completion I was doing
was basically playing on my own little field.

On top of that, I experienced a mental health crisis, and I felt like neither me
nor anyone else will ever care about what I do in the thesis. I became depressed
and to this day feel like I haven't quite 100% recovered. But I think that's
more "pre-existing problems came to the surface and I can no longer ignore
them", rather than "at this point I started having problems".
But I always felt sadness/envy/impostor syndrome seeing that I wasn't one of the
national-best-level people in the theoretical stuff.

In between the work being hard and frustrating and disconnected, and the mental
health crisis, I dropped my master's thesis, but stayed formally enrolled as a
student.

<figure>
<img src="/static/2023-01-11-ded.png" style="max-width: 8cm">
<figcaption>"ded x.x" Rai sticker, by
<a href="https://www.furaffinity.net/user/pareldraakje/">ParelDraakje</a></figcaption>
</figure>

Google ended up getting tired of me trying to postpone the offer for longer. I
accepted it, didn't get a H1b, and ended up getting an offer in Google
Switzerland.

### Google

At Google, the first team I worked on was doing some stuff which cared about
metrics and was downstream of NLP, but there wasn't really organizational
alignment that AI mattered. There were some applications of AI in isolated
places, but the problems my team was responsible for were much more shaped like
software engineering - building pipelines, services, etc. - than research
engineering.

I was sorta hoping to finish my master's thesis, but of course, didn't have time
to do it. I kept paying my uni to extend my studies, but eventually the clock
ran out. And so I did finish all the courses for a master's, but never actually
got it, because I didn't get around to doing the thesis.

Within Google, my original hope was that I'd try to position myself into a team
that would be AI or AI-adjacent, do researchy stuff, eventually position myself
to work on AI safety. But it ended up very hard trying to switch myself from
"software engineer working in team X doing software engineer things" into
"research engineer in ML". I didn't have any proven experience saying "I can do
ML" - all I had was "hey I did all this stuff in uni". But whenever I tried to
apply for an internal transfer, somehow it didn't end up working out. I think
there must have been very hard competition. And getting rejections was always an
ow.

My self-confidence suffered and I started feeling like I'm "not smart enough" or
"not good enough to do this". I envied people I knew who were working with ML.

Eventually I ended up saving up a bunch of money, including being lucky with
crypto. I though about it a bunch, and decided that I just felt dissatisfied
with the work at Google. It wasn't evolving me in the direction of working in
AI. I was good at my work, but I wasn't getting the skills I wanted to get.

### FIRE mode

Since uni I was following the "financial independence / early retirement" (FIRE)
philosophy. See places like
[/r/financialindependence](https://reddit.com/r/financialindependence) for info
on that. With the money I saved my simulations showed I had a maybe like 80% of
being able to stay in Switzerland, on my level of spending, basically
indefinitely.

So I started just chilling in Switzerland, basically trying out "what early
retirement would be like".

I played a lot of Civilization 6 and Civilization 5. I didn't really feel better
than when I was working - maybe even worse. When you're at work, you
automatically get a bit of socializing. When you're not, it's actually up to
your own initiative to meet people, and that was sorta hard in Switzerland.

### Chilling and learning ML

I never thought of FIRE as "I'd retire and then I just chill on the beach
sipping piña coladas forever". I wanted to find a mix of seeing friends, having
fun, and doing stuff that I felt like it mattered.

When I did the EA stuff in Czech Republic (like organizing the first
EAGxPrague), it felt like it mattered. Work on AI alignment would matter - if I
could manage to do it. Failing that, I thought maybe I'd find work someplace
where I liked the mission and product. I contributed to some open-source, like
[Anki](https://apps.ankiweb.net/) and [Athens Research](https://www.athensresearch.org/).

I've decided to try to put some more effort re-learning all the stuff I learned
about AI in uni, except this time I wanted to _actually grok it_, where you
could wake me up at 2 AM 5 years from now and I'd still be able to explain to
you how it works. I went over old materials and re-learned them, making Anki
cards, and started refilling the holes where my knowledge was stuck before
2017-era progress in AI - like deep RL or Transformer language models.

<figure>
<img src="/static/2023-01-11-papers-ded.png" style="max-width: 8cm" alt="Sticker of Rai overwhelmed with papers">
<figcaption>Also a Rai sticker by
<a href="https://www.furaffinity.net/user/ketzel99/">Ketzel99</a>.<br>
"Science was a mistake"? Maybe writing was? When reading paper X
that assumes you've taken <em>Advanced Xology</em> and did all the background
reading on Y, Z, &alpha;, &beta;... sometimes it indeed do be like that.</figcaption>
</figure>

To repeat the bullet points from the initial TL;DR, this was the procedure:

- Repeat a couple of times:
  - Make a list of stuff that's relevant to ML I don't yet grok.
  - Find resources to grok it. Some examples:
    - I consumed all the TensorFlow tutorials that don't go to too niche topics (though today I'd do PyTorch).
      And of course made [tons of Anki cards](https://agentydragon.com/posts/2019-11-25-my-anki-patterns.html).
      Contact me if you want some of them.
    - [Deep Learning specialization on Coursera](https://www.coursera.org/specializations/deep-learning)
    - [DeepLearning.AI TensorFlow Developer Professional Certificate on Coursera](https://www.coursera.org/professional-certificates/tensorflow-in-practice)
    - [Reinforcement Learning Specialization on Coursera](https://www.coursera.org/specializations/reinforcement-learning) (from Martha and Adam White of University of Alberta)
    - For RL, I did a course on Coursera
  - Learn all I can from those resources, collect stuff I don't yet
    understand, find references (e.g., papers I wanna read sometime etc.).
- Read up on recent papers.
- Read [Spinning Up in Deep RL](https://spinningup.openai.com/en/latest/)
  and follow up on the references.
- Implement some basic RL and ML experiments - e.g., lunar lander, [half-cheetah](https://agentydragon.com/posts/2021-10-18-rai-ml-mistakes-3.html).
- Take part in [AI Safety Camp](https://aisafety.camp/).
- Go to [Zurich AI alignment](https://www.zurich-ai-alignment.com/) meetups.
- Take part in an [AGI Safety
  Fundamentals](https://www.agisafetyfundamentals.com/ai-alignment-curriculum)
  course.

Nowadays, I'd actually recommend [Jacob Hilton](https://www.jacobh.co.uk/)'s
[Deep Learning curriculum](https://github.com/jacobhilton/deep_learning_curriculum).
It's based on an OpenAI-internal curriculum for residents and it's really good
at building up a good background for work at OpenAI.

So, I've been slowly taking courses, sometimes experimenting, sometimes reading
papers. Eventually I found the paper [Proximal Policy Optimization Algorithms](https://arxiv.org/abs/1707.06347).

<figure>
<img src="/static/2023-01-11-layers.png" style="max-width: 8cm" alt="Sticker of Rai going 'Stack more layers!'">
<figcaption>Rai "STACK MORE LAYERS" sticker, by
<a href="https://www.furaffinity.net/user/ketzel99/">Ketzel99</a>.<br>
This is my life now.</figcaption>
</figure>

I tried pretty hard to grok it, because it is sorta magic. One day I wanna write
a blog post explaining it. There's a whole bunch of math involved in proving
that it actually works.
But I've been trying to build really deep foundations, really grok all the
stuff involved in doing ML. To the point that theoretically I'd be able to
relay all of this to a Russian babushka, given enough time.

Reading that paper, I found a few small mistakes in exposition.
The first author on that paper is [John Schulman](http://joschu.net/).
I sent him an email.

In the background I've been on the lookout for some roles. I hoped eventually
I'd be able to take some role where I'd build up a bit of ML experience, and
then I'd be able to take that and leverage it into a role closer to AI safety.

Places I've been looking at included:

- [OpenAI residency](https://openai.com/blog/openai-residency/)
- [CHAI internship](https://humancompatible.ai/news/2020/11/12/internship-applications/)
- [DeepMind](https://www.deepmind.com/)
- [Anthropic](https://www.anthropic.com/)
- [Redwood Research](https://www.redwoodresearch.org/)
- [Machine Intelligence Research Institute](https://intelligence.org/)
- [Cohere](https://cohere.ai/)
- [Generally Intelligent](https://generallyintelligent.com/)
- [Ought](https://ought.org/).

John responded to the email, and invited me to apply to the OpenAI residency.

OpenAI had 2 residency tracks: software engineer and research. Originally I was
scared of applying for research, because of all the self-doubt and impostor
syndrome I was (and still to a degree am) carrying from having failed to write
a master's thesis.

But John encouraged me to try the research track, maybe believing in myself more
than I did - and I made it through. The research interview was the most nervous
I felt interviewing ever - it was a multi-hour small sized research project
involving tools like Jupyter, NumPy, etc.

And I made it. It was very useful to have all the commands for Pandas, NumPy,
etc. memorized in Anki. But if I had to take the interview again, I'd recommend
myself to spend more time playing around with various algorithms, grokking how
they behave as you change parameters, etc.

I ended up getting accepted both for the OpenAI residency, and an internship at
CHAI. Both of them would have been awesome to work on. Out of these two, I chose
to go with the residency - mostly because I felt that because CHAI was an
academic institution, it would have been harder to grow there post-internship
if I wasn't a PhD candidate.

## Conclusions and recommendations

Here's stuff I would have done differently and that I'd recommend to people in a
similar position:

- **Be safe but take risks**.

  I think I should have been less afraid to leave Google and be without a source
  of income for a while. Theoretically I think I could have executed the "take a
  year off and do some studying" thing maybe without even going through Google.
  Though the Google experience definitely made me a much better engineer.

  If you want to work on AI safety, I'd recommend doing something like this ("take
  a year off and learn AI") when you have maybe like 18 months of runway. I had way
  more runway than that at the point when I did that.

- **Community is super important**.

  I'd recommend myself to try harder to find other people who do things like
  read AI papers, do experiments, etc. - like AI meetups, AI safety camp, that
  sort of thing.

- **Local optimizations are also important**.

  In Switzerland, I was for a while in a loop where I felt depressed that I
  wasn't moving forward toward working in AI, and didn't have the energy to do
  that.

  I think one thing which helped a bunch was starting to actually learn German.
  Did I want to stay in Switzerland long-term? No, not necessarily. But it did
  make me feel way less like an alien. I didn't immediately broadcast to
  everyone "I'm a tourist" whenever I wanted to have a coffee. And I felt like
  "hey - I'm achieving a thing!"

  I'd generalize this to:

  <blockquote>"Tactics mean doing what you can with what you have."
  <br>
  -- Civilization 6 flavor text for [Military Tactics](https://civilization.fandom.com/wiki/Military_Tactics_(Civ6)) tech, originally [Saul Alinsky](https://www.brainyquote.com/quotes/saul_alinsky_378818).
  </blockquote>

  Do you really want to do thing [X] but it's really hard and discouraging and
  depressing because all actions towards [X] are not in the range of stuff you
  can do now?

  Maybe go and take a walk around the block. Wash the dishes. Will it solve [X]?
  No, but you'll make things easier for future-you. Exercise is healthy for the
  mind and body. Having a nice orderly environment makes it so that when you
  wake up, your first thought isn't "ugh, all this mess".

  Do what you can with what you have.

- **Don't fall victim to impostor syndrome**.

  If anyone figures out how to solve this one in full generality, let me know :)

  I think part of that can be just hanging around people who are nice and
  support you. For me, actively joining the furry community has been an
  important part of that. I haven't read Vega's [Opinionated
  Guides](https://opguides.info/) yet (they're on the to-read list), but I
  think the [Community](https://opguides.info/engineering/intro/community/)
  guide might have useful pointers. The furry community is great and so is the
  hacker community. For both of those, I've known for a long time that they
  exist and I wish I became actively involved way earlier.

I hope maybe this might give another nudge to people who'd like to work on
alignment, or that some of this advice might be useful.
