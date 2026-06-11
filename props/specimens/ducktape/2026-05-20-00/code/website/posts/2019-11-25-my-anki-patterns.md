---
title: My Anki patterns
---

I’ve used Anki for ~3 years, have 37k cards and did 0.5M reviews. I have learned
some useful heuristics for using it effectively. I’ll borrow software
engineering terminology and call heuristics for “what’s good” _patterns_
and heuristics for “what’s bad” _antipatterns_. Cards with antipatterns are
unnecessarily difficult to learn. I will first go over antipatterns I have
noticed, and then share patterns I use, mostly to counteract the antipatterns.
I will then throw in a grab-bag of things I’ve found useful to learn with Anki,
and some miscellaneous tips.

Alex Vermeer’s book [Anki Essentials](https://alexvermeer.com/anki-essentials/)
helped me learn how to use Anki effectively, and I can wholeheartedly recommend
it. I learned at least about the concept of interference from it, but I am
likely reinventing other wheels from it.

# Antipatterns

## Interference

Interference occurs when trying to learn two cards together is harder than
learning just one of them - one card _interferes_&nbsp;with learning another
one. For example, when learning languages, I often confuse words which rhyme
together or have a similar meaning (e.g., “vergeblich” and “erheblich” in
German).

Interference is bad, because you will keep getting those cards wrong, and Anki
will keep showing them to you, which is frustrating.

## Ambiguity

Ambiguity occurs when the front side of a card allows multiple answers, but the
back side does not list all options. For example, if the front side of a English
→ German card says “great”, there are at least two acceptable answers:
“großartig” and “gewaltig”.

Ambiguity is bad, because when you review an ambiguous card and give the answer
the card does not expect, you need to spend mental effort figuring out: “Do I
accept my answer or do I go with Again?”

You will spend this effort every time you review the card. When you (eventually,
given enough time) go with Again, Anki will treat the card as lapsed for reasons
that don’t track whether you are learning the facts you want to learn.

If you try to “power through” and learn ambiguous cards, you will be learning
factoids that are not inherent to the material you are learning, but just
accidental due to how your notes and cards represent the material. If you learn
to disambiguate two ambiguous cards, it will often be due to some property such
as how the text is laid out. You might end up learning “great (adj.)
→ großartig” and “great, typeset in boldface → gewaltig”, instead of the useful
lesson of what actually disambiguates the words (“großartig” is “metaphorically
great” as in “what a great sandwich”, whereas “gewaltig” means “physically
great” as in “the Burj Khalifa is a great structure”).

### Vagueness

I carve out “vagueness” as a special case of ambiguity. Vague cards are cards
where question the front side is asking is not clear. When I started using Anki,
I often created cards with a trigger such as “Plato” and just slammed everything
I wanted to learn about Plato on the back side: “Pupil of Socrates, Forms, wrote
The Republic criticising Athenian democracy, teacher of Aristotle”.

The issue with this sort of card is that if I recall just “Plato was a pupil of
Socrates and teacher of Aristotle”, I would still give the review an
Again&nbsp;mark, because I have not recalled the remaining factoids.

Again, if you try to power through, you will have to learn “Plato → I have to
recite 5 factoids”. But the fact that your card has 5 factoids on it is not
knowledge of Greek philosophers.

# Patterns

## Noticing

The first step to removing problems is knowing that they exist and where they
exist. Learn to **notice**&nbsp;when you got an answer wrong for the wrong
reasons.

“I tried to remember for a minute and nothing came up” is a good reason. Bad
reasons include the aforementioned interference, ambiguity and vagueness.

## Bug tracking

When you notice a problem in your Anki deck, you are often not in the best
position to immediately fix it - for example, you might be on your phone, or it
might take more energy to fix it than you have at the moment. So, create a way
to **track maintenance tasks**&nbsp;to delegate them to future you, who has more
energy and can edit the deck comfortably. Make it very easy to add a maintenance
task.

The way I do this is:

- I have a **big document** titled “Anki” with a structure mirroring my Anki
  deck hierarchy, with a list of problems for each deck.
  Unfortunately, adding things to a Google Doc on Android takes annoyingly many
  taps.
- So I also use **Google Keep**, which is more ergonomic, to store short notes
  marking a problem I notice. For example: “great can be großartig/gewaltig”.
  I move these to the doc later.
- I also use Anki’s note marking feature to note minor issues such as bad
  formatting of a card. I use Anki’s card browser later (with a “tag:marked”
  search) to fix those.

I use the same system also for tracking what information I’d like to put into
Anki at some point. (This mirrors the idea from the Getting Things Done theory
that _your TODO list belong outside your mind_.)

## Disambiguators

_2020-06-01 update: In an earlier version, I used to call those "distinguishers".
I now call them "disambiguators", because I think it's a more appropriate name._

Disambiguators are one way I fight interference. They are **cards that teach
disambiguating interfering facts**.

For example: “erheblich” means “considerable” and “vergeblich” means “in vain”.
Say I notice that when given the prompt “considerable”, I sometimes recall
“vergeblich” instead of the right answer.

When I get the card wrong, I notice the interference, and write down
“erheblich/vergeblich” into my Keep. Later, when I organize my deck on my
computer, I add a “disambiguator”, typically using Cloze deletion.
For example, like this:

{{c1::e}}r{{c1::h}}eblich: {{c2::considerable}}

{{c1::ve}}r{{c1::g}}eblich: {{c2::in vain}}

This creates two cards: one that asks me to assign the right English meaning to
the German words, and another one that shows me two English words and the common
parts of the German words (“\_r_eblich”) and asks me to correctly fill in the
blanks.

This sometimes fixes interference. When I learn the disambiguator note and later
need to translate the word “considerable” into German, I might still think of
the wrong word (“vergeblich”) first. But now the word “vergeblich” is also a
trigger for the disambiguator, so I will likely remember: “Oh, but wait,
vergeblich can be confused with erheblich, and vergeblich means ‘in vain’, not
‘considerably’”. And I will more likely answer the formerly interfering card
correctly.

## Constraints

Constraints are useful against interference, ambiguity and vagueness.

Starting from a question such as “What’s the German word for ‘great’”, we can
add a constraint&nbsp;such as “... that contains the letter O”, or “... that
does not contain the letter E”. The **constraint makes the question have only
one acceptable answer** - artificially.

Because constraints are artificial, I only use them when I can’t make
a disambiguator. For example, when two German words are true synonyms, they
cannot be disambiguated based on nuances of their meaning.

In Anki, you can annotate a Cloze with a hint text. I often put the constraint
into it. I use a hint of “~a~” to mean “word that contains the letter A”, and
other similar shorthands.

# Other tips

## Redundancy

Try to create cards using a fact in multiple ways or contexts. For example, when
learning a new word, include a couple of example sentences with the word.
When learning how to conjugate a verb, include both the conjugation table,
and sentences with examples of each conjugated form.

## Æsthethethics

It’s easier to do something if you like it. I like having all my cards follow
the same style, nicely typesetting my equations with `align*`, `\underbrace`
etc.

## Clozes

Most of my early notes were just front-back and back-front cards. Clozes are
often a much better choice, because they make entering the context and expected
response more natural, in situations such as:

- Fill in the missing step in this algorithm
- Complete the missing term in this equation
- Correctly conjugate this verb in this sentence
- In a line of code such as `matplotlib.pyplot.bar(x, y, color='r')`, you
  can cloze out the name of the function, its parameters, and the effect it
  has.

## Datasets I found useful

- Shortcut keys for every program I use frequently.
  - G Suite (Docs, Sheets, Keep, etc.)
  - Google Colab
  - Vim, Vimdiff
  - Command-line programs (Git, Bash, etc.)
- Programming languages and libraries
  - Google's technologies that have an open-source counterpart
  - What’s the name of a useful function
  - What are its parameters
- Unicode symbols (how to write 🐉, ←, ...)
- People: first and last name ↔ photo (I am not good with names)
- English terms (spelling of “curriculum”, what is “cupidity”)
- NATO phonetic alphabet, for spelling things over the phone
- Mathematics (learned for fun), computer science
