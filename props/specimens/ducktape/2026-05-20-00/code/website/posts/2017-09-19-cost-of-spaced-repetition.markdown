---
title: Cost of spaced repetition
---

[Spaced repetition](https://en.wikipedia.org/wiki/Spaced_repetition) is
a technique for efficiently memorizing things developed by smart people
who noticed that when you get reminded of something after not having seen
it for some time, you remember it longer next time. You represent things you
want to memorize as two-sided "cards", which have a prompt on one
side (e.g., "Population: Asia") and the answer on the other side ("4.4 B").
Each card has an interval attached to it, and it starts as a small constant,
say, 1 day. When you study a card, you look at the prompt, try to remember
the answer, and then flip over the card. If you answered correctly, you
exponentially increase the interval, say, multiply it by two. If you
answered incorrectly, you return the interval back to the minimum.
Different systems have slightly different algorithms.

Spaced repetition is really good for learning large amounts of raw
information. I have been using it for about a year now, for example
for German words, for studying for my master's final exam, for
country names and flags, and for flags and command line options of tools
I use. I use the spaced repetition program [Anki](https://apps.ankiweb.net/).

I have noticed that I sometimes use spaced repetition even if I probably
could be doing something more fun, or more productive. It is addictive,
sort of like playing Mafia Wars on Facebook in high school was addictive
or like checking my stock portfolio every day was addictive - seeing the
number of due cards going down has a nice feeling like "I am winning the game"
and "I feel smart".

The cost of Anki addiction is opportunity cost: time I am spending on Anki
is time I am not using to do more important things. I want to have a rule
of thumb I can use when I consider whether to add stuff to Anki, or whether
to keep it there, so I can deliberate: "What is more valuable to me? Having
X more hours of free time, or remembering the atomic number of every element?"

<h2>Spoiler</h2>

How much time does learning one Anki card consume?
My rule of thumb is **250 seconds** per card over the first year.
That means that learning a stack of 100 cards will take about **7 hours**.

Read on for how I came up with that.

<h2>My process</h2>

I will simplify Anki's algorithm to make the estimate. Let's assume:

- A single review takes r = 6 seconds.
  As of today, I have done on average 542.1 reviews per day and studied
  on average 54.7 minutes per day, so it works out to about 6 seconds
  per review.
- It takes l = 4 reviews to get a card from "learning" to "young" (i.e.,
  from "I have not seen this card or I forgot it" to "I am reviewing it,
  starting with an interval of 1 day"). I have pulled the number 4 out
  of my hat.
- If a card is "young" (i.e., its interval is less than 21 days), I have
  a 16% chance of getting it wrong and resetting its interval to 1 day
  (p<sub>1</sub> = 0.16). 16% is from my Anki statistics.
- If a card is "mature" (i.e., its interval is more than 21 days),
  I have only a 10% chance of getting it wrong
  (p<sub>2</sub> = 0.1).
- Card intervals multiply by 2 if I get the card right, or drop back to
  1 day.

I want to know how much review time will a card cost me over the next
365 days. I start with a card that is unreviewed.

<h2>Try 1: Markov chain stationary distribution</h2>

I will model it as a Markov chain. The first state is the state of
a card being forgotten/in learning, the second state is the state
of an interval of 2 days, then 4 days, etc., up to 256 days. (I don't
care about longer intervals for the purpose of 1 year. I'll pretend
a card doesn't get an interval longer than 256 days.)
I will figure out its stationary distribution, and then assume each
day I'm paying l &times; r seconds if I'm in the learning state,
r seconds for an interval of 1 day, r/2 for an interval
of 2 days, etc. So, I have 8 states.

A Markov chain is only a good model if the long term (where the probability
distribution converges to the stationary distribution) accounts for most
of the learning cost. This might not necessarily be the case.

I'll create a transition matrix and get its eigenvector in Octave.

```
p1 = 0.16;
p2 = 0.1;
M = zeros(8);
M(1:5,1) = p1;
for i = 1:5; M(i,i + 1) = 1 - p1; endfor
M(6:8,1) = p2;
for i = 6:7; M(i,i + 1) = 1 - p2; endfor
M(8,8) = 1 - p2;
```

I want to find a vector `x` such that `Mx=x` and the sum
of the components of `x` is 1:

```
x = [M' - eye(8); ones(1, 8)] \ [zeros(8, 1); 1];
```

I got: <code>x = [0.127905; 0.107440; 0.090250; 0.075810; 0.063680; 0.053491; 0.048142; 0.433281]</code>

Now to compute the cost.

```
r = 6;
w = zeros(1, 8);
w(1) = r * l;
for i = 2:5
  w(i) = (l * r * p1 + (1 - p1) * r) / (2 ** (i - 1));
endfor
for i = 6:8
  w(i) = (l * r * p2 + (1 - p1) * r) / (2 ** (i - 1));
endfor
time_daily = w * x;
disp(time_daily);
```

That adds up to **3.9 seconds** daily.

I noticed the probability mass on the last interval size
(0.433) is high. This means that in the stationary distribution,
there are many cards with intervals over 256 days, so in the
first year of studying a card, the distribution will be more heavily
weighted on the smaller intervals.

I decided to try again.

<h2>Try 2: Change the assumptions a bit</h2>

I'll slightly change the assumptions of the model. Let's say that if a
card's interval is x days, it doesn't mean that it will show
up exactly in x days - only that in each following day, it has a
1/x chance of coming up.

Let's create a matrix of transition probabilities:

```
p1 = 0.16;
p2 = 0.1;
M = zeros(8);
for i = 1:8;
  p_change = 1 / (2 ** i);
  % Fill in probabilities of reviewing and then forgetting.
  if i <= 5
    M(i,1) = p1 * p_change;
  else
    M(i,1) = p2 * p_change;
  endif
  % Fill in probabilities of reviewing and then getting it right.
  if i < 8
    if i <= 5
      M(i,i+1) = (1 - p1) * p_change;
    else i < 8
      M(i,i+1) = (1 - p2) * p_change;
    endif
    % Fill in probability of staying in the same state.
    M(i,i) = 1 - M(i,1) - M(i,i+1);
  else
    M(i,i) = 1 - M(i,1);
  endif
endfor
% Fix for the first row.
M(1,1) = p1;
M(1,2) = 1 - p1;
```

We are going to pay for transitioning from state A to a different state B.
Transitioning from A to A+1 or from A to 1 costs r seconds, and
transitioning from 1 to 2 costs r &times; l seconds.
Let's create a cost matrix.

```
r = 6;
l = 4;
C = zeros(8);
for i = 2:8;
  if i < 8
    C(i,i+1) = r;
  endif
  C(i,1) = r;
endfor
C(1,1) = r;
C(1,2) = r * l;
```

We start with the card in state 1 with probability 1, and we have paid
no costs yet. I will represent the costs as a vector of costs paid
entering a given state. At the beginning, we have paid nothing.

```
pd1 = [1 0 0 0 0 0 0 0]';
c1 = [0 0 0 0 0 0 0 0]';
```

I will want to get the total costs by doing matrix multiplication.
After 1 day, the probability distribution will change, and we will
incur some costs.

```
pd2 = M' * pd1;
c2 = (C' \.* M') \* pd1 + c1;
```

This change is linear, and we will represent the state as a combination
of the probability distribution vector and the costs paid so far.

In Octave, I will create a matrix transitioning from one state to
another and a vector representing the entire state:

```
T = [M', zeros(8); M' .* C', eye(8)];
s0 = [1; zeros(7, 1); zeros(8, 1)];
```

So, after 365 days, we have paid these daily costs:

```
daily_costs = sum((T**365 * s0)(9:16)) / 365;
disp(daily_costs);
```

This model gives me **0.32 seconds** per day.
This is 117.69 seconds per year, of which 58 are spent transitioning
from state 1 to state 2.
The probability distribution was also heavily skewed towards the higher
intervals - 64% of cards were in the last state. That means this
model is just overly optimistic; there is no way 60% of cards in the
unsimplified algorithm could have an interval this long after just 1 year
(they would first have to go through the 128 + 256 days).
This tells me this model might be **way off and broken**.

<h2>Try 3: Python</h2>

And at this point, I'm like "fuck this, I'm better at Python than at math".
So I wrote a thing in Python:

```
#!/usr/bin/python2

import random
import numpy

p1 = 0.16
p2 = 0.1
review_time = 6
reviews_to_learn = 4

def simulate_cost_of_card_over_year():
  next_day = 0
  interval = 0
  cost = 0
  while next_day < 365:
    if interval == 0:
      cost += reviews_to_learn * review_time
      interval = 1
    else:
      cost += review_time
      if interval < 21:
        if random.random() < p1:
          interval = 0
        else:
          interval *= 2
      else:
        if random.random() < p2:
          interval = 0
        else:
          interval *= 2
    next_day += interval
  return cost

tries = [simulate_cost_of_card_over_year() for _ in range(1000)]
print numpy.mean(tries)
```

And I got these results:

```
30 81.804
365 159.438
3650 252.15
18250 367.38
```

That tells me that over the first year, I will spend about **0.43 seconds per day**
reviewing the card. I trust this result more than I trust my previous hacky math.

<h2>Why all the estimates are bullshit</h2>

At this point, I was thinking about to declare "victory, it's about 0.43 seconds
per day, and let me now also say why exactly the number is bullshit":

- Anki actually doesn't use a constant interval multiplier of 2; it uses 2.5
  by default, and it also adjusts the interval as it goes.
- You can also answer cards as "Easy", which makes the interval even bigger.
- The estimate of "it takes 4 tries to learn a new card" was totally made up.

And then I realized I can actually fix the last point, and that it's probably also
the most significant problem - most of the time spend on a given card will be
learning it, not reviewing it.

<h2>Fixing the reviews per learning constant</h2>

Anki says I did ~35k "Again" responses on cards in learning, and ~65k "Good"
responses (and a negligible amount of "Easy" responses). This means that
on an attempt to answer a card in the learning phase, I have a p=0.62
chance to get it right. I have Anki setup such that a card graduates to
reviewing after I get it right 3 times.

Denote e0, e1, e2 the expected number of attempts it will take me
to get a card graduated, given that I have successfully answered it the
last 0, 1, or 2 times.

We have: e2 = p &times; 1 + (1-p)&times; (1 + e0), e1=p&times; (1 + e2) + (1-p)&times; (1 + e0),
e0 = p&times; (1 + e1) + (1-p)(1 + e0).
Splashing that around a little bit, I get e0 = 1/p + 1/p<sup>2</sup> + 1/p<sup>3</sup>,
which means: e0 = 8.44.

So, that entails some corrections.

Correcting for that in the Python program (setting `reviews_to_learn = 8.44`)
yields:

```
30 135.76992
365 252.52464
3650 364.75824
18250 502.50888
```

This means **0.69 seconds per day** in the first year, and a total of
250 seconds in the first year, and ~2 minutes in the first month.

<h2>Corollaries</h2>

- Learning a new keyboard shortcut is worth it if it will save me
  0.7 seconds per day over the following year. (Or 250 seconds in total).
- My German deck, which has 26 241 cards, would take roughly a whooping
  **1822 hours** over the next year. That's a full **227 workdays**.
- Rai has an Anki problem.
