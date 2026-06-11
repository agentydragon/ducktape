---
title: Model of continuous asset growth
---

<a href="https://colab.research.google.com/github/agentydragon/agentydragon.github.io/blob/devel/notebooks/2019-09-02-continuous-asset-growth.ipynb" target="_parent"><img src="https://colab.research.google.com/assets/colab-badge.svg" alt="Open In Colab"/></a>

FIRE stands for financial independence/early retirement.
The point is to save and invest money, and pay yourself a salary from the interest, eventually becoming independent on other sources of inocme.

There is a relationship between:

- How much you have invested
- The interest your investment makes. (The widely cited "[Trinity study](https://en.wikipedia.org/wiki/Trinity_study)" suggests 4% as a "safe withdrawal rate".)
- The salary you pay yourself
- How long your savings last for you

I have a program named worthy ([on Github](https://github.com/agentydragon/worthy)) that tracks my net worth and models when will I be financially independent under various assumptions. Here I describe the slightly fancy math behind a
more accurate model for this relationship I finished implementing today.

I am probably rediscovering Financial Mathematics 101 ¯\\\_(ツ)\_/¯

# The questions

- The **"how much" question**: _I want to pay myself 1000 USD. My stocks grow 4% per year. How much money do I need?_
- The **"how long until" question**: _I have 100 000 USD and save 3000 USD per month. How long until I have 200 000 USD?_

# First shot

Previously the tool's model was very basic, and answered the two questions as follows:

- _I want to pay myself 1000 USD per month. My stocks grow 4% per year. How much money do I need?_
  Well, the 4% you get per year should cover the yearly costs, so $1000 / (1.04 ^ {1/12} - 1) \approx306\ 000$ USD.
- _I have 100 000 USD and save 3000 USD per month. How long until I have the 306 000 USD that you said I need?_
  That I modelled linearly, with just $ (306000 - 100000) / (3000/\text{month}) \approx 69\ \text{months}$.

# Problems

# Assuming infinite retirement time

If you pay yourself a monthly salary of $ \\$1000 $ and your monthly interest is $ \\$1000 $, your money will last forever, beyond your (likely) lifespan. If you are fine with retiring with $ \\$0 $, you can pay yourself a bit more than just the $ \\$1000$ interest.

# Ignoring growth while saving

"Take how much money I need - how much I have, divide by monthly savings" ignores that the money I saved up so far also earn interest, before I'm done saving. It's too pessimistic.

# Stand aside, I know differential equations

Let's model the depletion of your money as a function $f$, which will map number of years since retirement to the amount of money. You start with some initial amount $f(0)$. If we pretend you withdraw the salary for a year and add interest once yearly, we'd get:

$$
f(x+1) = f(x) + i\cdot f(x) - c
$$

Where $i$ is the yearly interest rate and $c$ are the yearly costs. In the example above, $i=0.04$ and $c=12000$ USD.

Then:

$$
f(x+1) - f(x) = i\cdot f(x) - c
$$

If we instead pretend that everything is continuous and squint, this looks like a differential equation:

$$
f'(x) = i'\cdot f(x) - c'
$$

(Where $i'$ plays _sorta_ the same role as $i$ - except it's not equal to $i$. For now let's pretend it's some unknown variable. Its relationship to $i$ will eventually pop out.)

[Wikipedia's Ordinary differential equations article](https://en.wikipedia.org/wiki/Ordinary_differential_equation) says that if $dy/dx = F(y)$, then the solution is \\(x=\\int^{y}{\\frac {d\\lambda }{F(\\lambda )}}+C\\).
In our case, we have $F:x\mapsto ix-c'$, so:

$$x = \int^{f(x)}{\frac{1}{i'\lambda-c'} d\lambda}+C \stackrel{\text{Wolfram Alpha}}{=} \frac{\log(i'f(x)-c')}{i'} + C$$

Solving for $f(x)$:

$$
\log(i'f(x)-c') = i'(x-C) \\
i'f(x)-c' = \exp(i'(x-C)) \\
f(x) = \frac{\exp(i'(x-C)) + c'}{i'}
$$

So, magic happened and I pulled the general form of $f(x)$ out of a hat. We know what are the $i$ and $c$ values when we assumed interest and costs happen only once yearly.

What about $i'$?
Let's guess it.
If we had no yearly costs (so $c=c'=0$), we wanted to have $f$ growing at a constant rate, gaining $i$ in interest per year:

$$
f(x+1) / f(x) = 1+i
$$

Substituting in the above equation of $f$, we get: $$\exp(i'(x+1-C)) / \exp(i'(x-C)) = 1+i$$

When we simplify the fraction, we get $\exp(i') = 1+i$ and therefore $i'=\log(1+i)$. So, we have now successfully
guessed the right value for $i'$ :)

Now what's the right value of $c'$?

If we set interest to $i=0$, $f(x)$ should simplify to a nice linear equation losing $c$ per 1 unit of $x$.

$$x=\int^{f(x)} -\frac{1}{c'} d\lambda + C = -f(x)/c' + C$$

So:

$$
-f(x)/c' = x-C\\
-f(x)=c'(x-C)\\
f(x)=-c'(x-C)
$$

So the right value for $c'$ is exactly $c$.

So we have:

$$
f(x) = \frac{\exp(\log(1+i)(x-C)) + c}{\log(1+i)} = \frac{(1+i)^{x-C} + c}{\log(1+i)}
$$

$C$ mediates a multiplicative factor before $(1+i)^x$. $C$ is just _some constant that makes the function work with the $f(0)$ boundary condition_. Instead of wiggling the $C$, we can instead wiggle $C_2=(1+i)^-C$, which is the actual multiplicative factor, and relabel $C_2$ as $C$. (It's an abuse of notation, but an OK one. \*handwave\*)

$$
f(x) = C \cdot (1+i)^{x} + \frac{c}{\log(1+i)}
$$

The one remaining unknown variable is $C$, which we will get from $f(0)$ - which are the initial savings.

$$f(0) = C + \frac{c}{\log(1+i)}$$

So:

$$C = f_0 - \frac{c}{i'}$$

Okay this is a little bit ugly. Let's play.

```python
c = 12000  # yearly costs
f_0 = 100000  # initial savings
i = 0.04  # interest

from math import log, exp
i_prime = log(1+i)
print(f'i_prime={i_prime}')

C = f_0 - (c / i_prime)
print(f'C={C}')

def f(x):
  return C * (1+i) ** x + (c / i_prime)

for r in range(11):
  print("after", r, "years, got:", f(r))
```

And it gives:

    i_prime=0.03922071315328133
    C=-205960.78029234003
    after 0 years, got: 100000.0
    after 1 years, got: 91761.56878830638
    after 2 years, got: 83193.60032814502
    after 3 years, got: 74282.91312957724
    after 4 years, got: 65015.79844306671
    after 5 years, got: 55377.9991690958
    after 6 years, got: 45354.68792416598
    after 7 years, got: 34930.44422943902
    after 8 years, got: 24089.23078692297
    after 9 years, got: 12814.368806706276
    after 10 years, got: 1088.512347280921

Cool, it seems to be giving reasonable results. But our two questions were: _how much money do I need to pay myself a given salary_ and _how long until I save up the money I need_.

Let's instead first solve another question: _if I have 100 000 USD and spend 1000 USD per month, how long will it last me_.

For that, we just need to invert the familiar function:

$$
f(x) = C \cdot (1+i)^{x} + \frac{c}{\log(1+i)}
$$

We want to know the number of years $x$ at which we will run out of money (so $f(x)=0$)

$$
0 = C \cdot (1+i)^x + \frac{c}{\log(1+i)} \\
(1+i)^x = \frac{-c}{C \log(i+1)} \\
x = \frac{\log{\frac{-c}{C \cdot i'}}}{i'}
$$

And let's test it:

```
x = (log(-c / (C * i_prime))) / i_prime
print(x)
```

    10.090871103712766

Cool, this matches what the Python $f(x)$ predicted above - after 10 years, it was just dwindling at about 1088 USD.

# Answering the _how long_ question

To answer the question "if I now have 100 000 USD collecting 4% interest per year and put in 1000 USD per month, how long until I have 306 000 USD", we can use the same procedure - just plug in a target $f(x)=306\ 000$ instead of zero and set a negative $c$ to represent savings instead of costs. Details left as homework for the curious reader.

If you're curious about the Go code, see
[this commit](https://github.com/agentydragon/worthy/commit/c48ded40640cda8e3851fd0b2a9512f95ae89997).

# Answering the _how much_ question

As a reminder, the "how much" question asks: _if I want to pay myself a salary of 1000 USD per month, how much money do I need_. Previously, I solved that with saying "the interest should cover all the costs", which resulted in an investment that would last _forever_ (a _perpetuity_). But now have a function that models an investment under conditions of withdrawing (or saving) money, and we can use that to model with a finite time horizon, and get a better estimate.

Say that we know that we are 40 years old and want our money to run out on our 100th birthday. So, after $x=60$ years
of paying ourselves, say, 1000 USD per month (so the yearly costs $c=12000$), we want to have $f(x)=0$.
How much initial money $f(0)$ do we need for that stunt of precious timing?

Okay, from above, we know:

$$
f(x) = C (1+i)^{x} + \frac{c}{i'} = \left(f(0) - \frac{c}{i'}\right) \cdot (1+i)^{x} + \frac{c}{i'}
$$

So:

$$
f(x) = f(0)(1+i)^x - \frac{c}{i'}(1+i)^x + \frac{c}{i'} \\
-f(0)(1+i)^x = \frac{c}{i'} -f(x) - \frac{c}{i'}(1+i)^x
$$

Let's remember that we want $f(x)$ to be 0.

$$
-f(0)(1+i)^x = \frac{c}{i'} - \frac{c}{i'}(1+i)^x = \frac{c}{i'}(1-(1+i)^x) \\
f(0) = \frac{c}{i'}(1-(1+i)^{-x})
$$

Let's try it out:

```
c = 12000  # yearly costs
x = 60  # years for the investment to survive
i = 0.04  # interest
```

```
i_prime = log(1+i)
f0 = (c/i_prime) * (1-(1+i)**(-x))
print(f0)
```

    276876.0258210814

Cool!

Recalling the numbers in the first section, the first algorithm which assumed an infinite horizon prescribed 306 000 USD for that situation ("1000 USD per month at 4% interest rate"). This more precise estimate cut 30 000 USD from the number :)
