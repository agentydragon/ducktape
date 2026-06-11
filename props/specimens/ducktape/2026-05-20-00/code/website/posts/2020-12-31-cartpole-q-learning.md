---
title: Cartpole Q-learning
---

Recently I've been working on skilling up on reinforcement learning,
particularly practice. I'm currently on the last course of the
[Reinforcement Learning specialization](https://www.coursera.org/specializations/reinforcement-learning)
from University of Alberta on Coursera. The last piece of the course is about
solving the [Lunar Lander](https://gym.openai.com/envs/LunarLander-v2/)
environment. I've been trying to solve it on my own first before going through
the labs, so that I can learn things deeper and experiment.

I've tried implementing an actor-critic agent. The actor is a feed-forward
neural network specifying a parameterized policy $\pi_\theta$. The network's
input is a representation of the state, and it has one output per action.
The policy is a softmax over these outputs. I tried a critic for predicting
both $\hat{v}_w$, and $\hat{q}_w$.

I've not had good luck getting this to work so far. At one point I got the agent
to fly above the surface (without landing), but then later I edited the code
somewhat, aaaaand it was gone.

I stared a bunch into my update equations, but have not been able to find any
obvious errors. I used TensorFlow's
[HParams](https://www.tensorflow.org/tensorboard/hyperparameter_tuning_with_hparams)
to try to tune all the hyperparameters, like actor learning rate, critic
learning rate, and learning rate for the average reward. (I wrote it attempting
to use the continuing average reward formulation.)

I decided to first try a simpler environment, [CartPole](https://gym.openai.com/envs/CartPole-v0/).

In the end, I managed to solve it a couple hours back.

In the implementation, I've made a couple mistakes and observations, which I
want to note down.

## Colab notebook with my CartPole agent

Here's my notebook if you want to play around:

<a href="https://colab.research.google.com/github/agentydragon/agentydragon.github.io/blob/devel/notebooks/2020-12-31-cartpole-q-learning.ipynb" target="_parent">
  <img src="https://colab.research.google.com/assets/colab-badge.svg" alt="Open In Colab">
</a>

## Rai's ML mistake #1: Short episodic environments can use high &gamma;

I initially wrote my code to use a discount rate of 0.9. On
[n1try's solution](https://gym.openai.com/evaluations/eval_EIcM1ZBnQW2LBaFN6FY65g/)
that I found on [the leaderboard](https://gym.openai.com/envs/CartPole-v0/)
(unfortunately not sure which one it was), the discount rate was actually set
to 1.

I suspect I might have set the discount rate too low. The CartPole environment
has episodes which have length of only up to ~500, with 1 unit of reward per
step.

If you have a discount rate &gamma; and an average per-step reward of $r$,
then in an infinite environment, the value of a state will be something like:
$$ \frac{r}{1-\gamma} = r + r \cdot \gamma + r \cdot \gamma^2 $$
Knowing this, I was a worried that if I set $\gamma$ too high, the targets
for the Q network to learn would have a high variance. But I forgot that the
environment had only like ~500 steps, so setting $\gamma=1$ would be alright
in this case.

_Lesson learned_: I need to keep in mind the environment's characteristics, in
particular how long are the episodes and how high total rewards can I expect.

## Rai's ML mistake #2: Too little exploration

The algorithm that ended up working for me was Q-learning (with function
approximation by a small neural network). I selected actions
$\varepsilon$-greedily, with $\varepsilon$ set to 0.02, so ~2% chance of
random moves.

Looking at some solutions of the environment that I found, they had much higher
exploration rates. Some that I saw had 100% random actions initially, and had
it then decay. And the particular solution I was looking at set the minimal
exploration rate, after all the decay, to _10%_ - 5x more than I had.

I think my code found a policy that "solves" the environment faster when I put
in 10% exploration.

## Rai's ML mistake #3: Evaluation interfering with training

I ran my algorithm on 10k episodes, and every 1k episodes, I ran the code in
"greedy mode" (i.e., no random actions) and recorded average performance on 100
episodes. I did that because my Q-learning implementation was executing an
$\varepsilon$-soft policy, which might be worse than the greedy policy that
it's learning. I don't know how "fragile" the CartPole environment is (i.e.,
how much worse the total reward per episode gets if I force an agent to take
some amount of random actions), but I wanted to rule it out as a source of
errors.

I implemented the evaluation by just adding a flag `train: bool = True` to
the agent's functions. If `train` was `False`, I'd skip all the update steps
and select actions greedily.

Unfortunately, I ended up making a mistake and I forgot to add the condition
around one branch of code - updating the Q network after a final step (i.e.,
when the agent receives a last reward and the episode ends).

As a result, the agent ended up executing ~100 incorrect updates (based on
an incorrect last action and state, towards the final reward), one incorrect
update per evaluation episode.

**Lesson learned**: Double check my code? Maybe even test my code? Not sure
how to learn from this :/

## _Forgetting when learned too much‽‽_

<figure>
<img src="/static/2020-12-31-total_reward.svg" style="height: 400px;"
     title="Total reward per episode graph" alt="Graph of total reward per episode">
<div>
[*𝄞 ♫ Look at this graaaph♫  𝄻*](https://www.youtube.com/watch?v=sIlNIVXpIns)
</div>
</figure>

So, until episode maybe 400 or so, nothing much is happening.
Then until about step 1800, it's sorta doing something but could be better.
Then at around step 1800, it finds a good policy, and returns are nice.
Basically, problem solved.

Then I train it for a bit longer, and at episode ~2.2k... for some reason
performance goes WAY down for about 300 or so episodes. It's as bad as if the
network forgot everything it learned before.

Then, after a while (at about 2700), it quickly climbs back up to good
performance. On the full graph with 10k episodes, this cycle would repeat maybe
every 2000-3000 episodes. Wat.

I have no idea what's going on here. Maybe one sort-of ideas, but I have not
tested it, and I have a relatively low degree of confidence in it.

Maybe for some reason the critic might overfit in some way that makes it behave
badly on some early action. Maybe it's trying to learn some relatively "late
game" stuff, and the update that happens ends up screwing some early behavior,
so the agent then has to spend a bunch of episodes learning the right early
behavior again. The policy changes in a non-continuous way, so if some early
decision switches to do the wrong thing, the agent will then have to follow a
bunch of wrong trajectories until the one-step Q updates end up bubbling up to
the initial wrong decision. I guess this might be somewhat mitigated by using
eligibility traces so that updates bubble up faster, or by using actor-critic
with soft policies.

Another potential reason for having a really bad episode might be if the agent
happens to pick a random action (with probability $\varepsilon$) at an early
point where the pole is unstable and very easy to mess up. And then it can't
recover from that error. But that explanation isn't supported by how wide these
areas of low episode returns are. It might explain maybe one sporadic bad
episode, but not a whole bunch of them after each other.

## Next steps

Now that I got a CartPole agent running, I'll come back to the Lunar Lander
environment. I'll first try solving it again with a Q network. I could probably
similarly get away with not discounting rewards at all ($\gamma = 1$).

Also I'd like to implement experience replay to make this more sample-efficient.

If that ends up working, I still want to get actor-critic working.

Obligatory scoot-scoot:

<video controls loop autoplay>
    <source src="/static/2020-12-31-cartpole.mp4" type="video/mp4">
</video>
