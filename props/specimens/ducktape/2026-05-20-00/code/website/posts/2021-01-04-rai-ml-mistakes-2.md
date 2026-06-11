---
title: Rai's ML mistakes, part 2 of ∞
---

Continuing my list of ML mistakes from [last
time](/posts/2020-12-31-cartpole-q-learning.html), here's:

# Rai's ML mistake #4: Too much autograd

So here I am, writing an agent for
[LunarLander-v2](https://gym.openai.com/envs/LunarLander-v2/).
I'm using Q-learning. I approximate $q_*(s,a)$ as
$\mathrm{\hat{q}}_w(s,a)$ with a neural network, taking a vector representing
the state, and outputting one approximate action value per output.
The neural network is trained to minimize squared TD error on the policy the
agent's running, which is $\varepsilon$-greedy with respect to
$\mathrm{\hat{q}}$:

$$
\begin{align*}
\require{extpfeil}
\ell(w) &= \mathop{\mathbb{E}}\limits_{(S \xrightarrow{A} R,S') \sim \mathrm{greedy}(\mathrm{\hat{q}}_w)}
\left[ \left(\mathrm{\hat{q}}_w(S, A) - (R + \gamma \max_{A'} \mathrm{\hat{q}}_w(S',A')) \right)^2 \right] \\
\text{output } &\arg\min_w \ell(w)
\end{align*}
$$

## Not quite off-policy

One note about the "policity" of this method.

Tabular Q-learning without function approximation is off-policy - you learn
about $\pi_*$ from experience $(S \rightarrow_A R,S')$ sampled from any
(sane&trade;) policy. You just keep updating $\mathrm{\hat{q}}(S,A)$ towards
$R+\gamma \cdot \max_{A'} \mathrm{\hat{q}}(S',A')$, and to $\max$ is there because you want
to learn about the optimal policy.

But note that in $\ell(w)$, the experience $(S \rightarrow_A R,S')$ is
sampled from the policy $\mathrm{greedy}(\mathrm{\hat{q}}_w)$.
We need to expect _over a policy_, because we're using function approximation,
so presumably we cannot learn a $w$ which would make $\mathrm{\hat{q}}_w$
exactly fit $q_*$. So we have to pick out battles for how well do we
approximate $q_*$ - we care about approximating it closely for
states and actions actually visited by the estimation policy.

Instead of assuming that we can sample $(S \rightarrow_A R,S')$ from
$\mathrm{greedy}(\mathrm{\hat{q}}_w)$ (so that we can approximate the expected
squared TD error over it), I guess you could use the general importance sampling
recipe to get rid of that:

$$
\mathop{\mathbb{E}}_\limits{X\sim \pi}[\mathrm{f}(X)] =
\mathop{\mathbb{E}}_\limits{X\sim b}\left[\mathrm{f}(X) \cdot \frac{\pi(X)}{b(X)}\right]
$$

## Semi-gradient

So, we want to minimize $\ell$.

Note that $\ell$ depends on $w$ (via $\mathrm{\hat{q}}_w$) in 3 places:

1. In $\mathrm{\hat{q}}_w(S,A)$, which we are trying to nudge to move to the
   right place,
2. in $R + \gamma \max_{A'} \mathrm{\hat{q}}_w(S',A')$, which is a sample
   from a distribution centered on
   $q_{\mathrm{greedy}(\mathrm{\hat{q}}_w)}(S,A)$,
3. and in the distribution we're taking the expectation on.

In practice, we hold (2) and (3) constant, and in one optimization step, we
wiggle $w$ only to move $\mathrm{\hat{q}}_w(S,A)$ closer to targets.
That means that in our gradient, we are ignoring the dependency of (2) and (3)
on the $w$ that we are optimizing, which makes this not a full gradient method,
but instead a _semi-gradient_ method.

## Experience replay

My first shot at this agent just learned from 1 step (sampled from
$\varepsilon$-greedy policy for $\mathrm{\hat{q}}_w$) at a time. It worked
in the sense that it ended up learning a policy close enough to "solving the
environment". (The environment says the "solved reward" is 200. I got maybe like
150-180 over 100 episodes, so not quite there, but it's close enough for me to
say "meh, I'll wiggle a few hyperparameters and get there".)

But to learn a fair policy, it took the agent about 10 000 episodes, and the
per-episode total reward over time made a spiky ugly graph:

<figure>
<img src="/static/2020-12-31-total_reward.svg" style="height: 400px;"
     title="Total reward per episode graph">
</figure>

I don't like that it takes all of 10 000 episodes, and I don't like how spiky
and ugly the chart is.

Experience replay means we store a bunch of experience
$(S \rightarrow_A R,S')_{1,2,\ldots}$ in a buffer, and instead of updating
$w$ by some gradient-based optimization method (I used ADAM) to minimize squared
TD error one step at a time, we update it to minimize squared TD error over the
whole buffer, a bunch of steps at a time.

Experience replay should make learning more sample-efficient (so it should need
less than 10 000 episodes). Also, it should reduce one source of "spikiness
and ugliness" in the chart, because the chart will be doing step updates on
a larger batch. Making the batch larger should reduce the variance of the
updates.

## Broken code

So, here's how I initially implemented one step of the update.
`self.experience_{prestates, actions, rewards, poststates, done}` holds
the experience buffer ($S, A, R, S'$ respectively for observed transition
$S\rightarrow_A R, S'$, plus flag to signal end of episode).

```python
@tf.function
def q_update(self):
  with tf.GradientTape() as tape:
    # \max_{A'} \hat{q}(S', A')
    best_next_action_value = tf.reduce_max(
        self.q_net(self.experience_poststates), axis=1)
    # If episode ends after this step, the environment will only give us
    # one step of reward and nothing more. Otherwise, the value of the next
    # state S' is best_next_action_value.
    next_state_value = tf.where(
        self.experience_done,
        tf.zeros_like(best_next_action_value),
        best_next_action_value)
    targets = self.experience_rewards + self.discount_rate * next_state_value

    # For all states S_i in the experience buffer, compute Q(S_i, *) for all
    # actions.
    next_action_values = self.q_net(self.experience_prestates)
    # Select Q(S_i, A_i) where A_i corresponds to the recorded experience
    # S_i --(A_i)--> R_i, S'_i, done_i.
    indices = tf.stack(
      (tf.range(self.experience_buffer_size), self.experience_actions),
      axis=1)
    values_of_selected_actions = tf.gather_nd(next_action_values, indices)

    loss = tf.keras.losses.MeanSquaredError()(
        values_of_selected_actions, targets)

  grad = tape.gradient(loss, self.q_net.trainable_variables)
  self.optimizer.apply_gradients(zip(grad, self.q_net.trainable_variables))
```

What's wrong here?

The symptom is that the policy is not improving. The total reward per episode
is just oscillating.

<figure>
<img src="/static/2020-sticker-hmm.png" title="Hmm">
</figure>

## The problem

Remember how I said it's a _semi-gradient_ method?

Here's the fix:

```python
@tf.function
def q_update(self):
  # \max_{A'} \hat{q}(S', A')
  best_next_action_value = tf.reduce_max(
      self.q_net(self.experience_poststates), axis=1)
  # If episode ends after this step, the environment will only give us
  # one step of reward and nothing more. Otherwise, the value of the next
  # state S' is best_next_action_value.
  next_state_value = tf.where(
      self.experience_done,
      tf.zeros_like(best_next_action_value),
      best_next_action_value)
  targets = self.experience_rewards + self.discount_rate * next_state_value

  with tf.GradientTape() as tape:
    # For all states S_i in the experience buffer, compute Q(S_i, *) for all
    # actions.
    next_action_values = self.q_net(self.experience_prestates)
    # Select Q(S_i, A_i) where A_i corresponds to the recorded experience
    # S_i --(A_i)--> R_i, S'_i, done_i.
    indices = tf.stack(
      (tf.range(self.experience_buffer_size), self.experience_actions),
      axis=1)
    values_of_selected_actions = tf.gather_nd(next_action_values, indices)

    loss = tf.keras.losses.MeanSquaredError()(
        values_of_selected_actions, targets)

  grad = tape.gradient(loss, self.q_net.trainable_variables)
  self.optimizer.apply_gradients(zip(grad, self.q_net.trainable_variables))
```

So, what was the problem?

The code calls the Q network twice: once to compute the targets ($R +
\gamma\cdot \max_{A'} \mathrm{\hat{q}}_w(S',A')$), once to compute
$\mathrm{\hat{q}}_w(S,A)$. Then, we will compute a loss, and we will take its
partial _"semi-derivative"_ with respect to $w$, and apply the gradient to
bring $\mathrm{\hat{q}}_w(S,A)$ closer to the target.

The problem was: I also put the target computation into `GradientTape` scope,
so the optimization was given the freedom to change not just
$\mathrm{\hat{q}}_w(S,A)$, but _also_ $\mathrm{\hat{q}}_w(S',A')$.
So the fix was just to move computing the targets out of the `GradientTape`
scope.

I looked at this code basically non-stop for 2 hours, and I realized the error
when I took a break and talked with a friend.

<figure>
<img src="/static/2020-sticker-ded.png" title="_(x.x)_   <-- ded">
</figure>

## Pet peeve #47: math typesetting

_The full list of previous 46 pet peeves will be provided on request, subject
to a reasonable processing fee._

### MathJax, `\hat` and `\mathrm`

$\mathrm{\hat{q}}$ is a function (of $w, S, A$), not a variable, so it
shouldn't be typeset in italic. I tried using `\hat{\mathrm{q}}_w`. I believe
that works in LaTeX but turns out that MathJax is not willing to render it
($\hat{\mathrm{q}}$). But `\mathrm{\hat{q}}` is perfectly fine:
$\mathrm{\hat{q}}$. But `\mathrm{\hat{q}}_w` is perfectly fine:
$\mathrm{\hat{q}}_w$.

### MathJax and inline `\xrightarrow`

Also, my MathJax doesn't seem to understand `\xrightarrow` in inline equations.
That's a shame, because `S \xrightarrow{A} R, S'` is more readable than
`S \rightarrow_A R, S'` ($S \rightarrow_A R, S'$), which I used here instead
(in inline equations). It looks like this:
$$S \xrightarrow{A} R, S'$$
Let me know if you know what's up with those MathJax things.
I wonder if it's MathJax being wrong, or me sucking at LaTeX.

### Why I'm a math typesetting snob

Typesetting things that aren't variables as if they were variables really bugs
me, because it makes the formulas really ugly. And the font you use to typeset
a math thing is a very useful hint for the reader about what sort of object it
is. I learned a bit about it when volunteering as a
[KSP](https://ksp.mff.cuni.cz/) organizer - KSP is full of math snobs. Compare:

$$
\begin{align*}
\mathrm{Loss}(w) = \sum_i (\mathrm{f}(x_i) - y_i)^2 \\
Loss(w) = \sum_i (f(x_i) - y_i)^2
\end{align*}
$$

In the second one, it takes a bit of processing to understand that $Loss$ is
not a multiplication ($L \cdot o \cdot s \cdot s$), and that $f(x_i)$ is
function application.

If you want to read more, you can take a look at [Typographical conventions in mathematical
formulae on Wikipedia](https://en.wikipedia.org/wiki/Typographical_conventions_in_mathematical_formulae).
Or maybe some LaTeX / TeX books or reference material might have a lot of
explanations, like "use this in these situations". And also good math books
often have a large table at the front which explains used conventions, like
"$\mathbf{w}$ is a vector, $\mathrm{X}$ is a matrix, $\mathrm{f}$ is a function,
..."

<figure>
<img src="https://imgs.xkcd.com/comics/kerning.png" style="height: 400px;"
     title="XKCD 1015 (Kerning)" alt="XKCD comic about kerning">
<div>
  <a href="https://xkcd.com/1015/">https://xkcd.com/1015/</a>
  <br>
  Now you know about ugly errors in math typesetting, and if you Google it,
  also about bad kerning. You're welcome, pass it along.
</div>
</figure>

<figure>
<img src="/static/2020-sticker-mlem.png" title="Mlem!" alt="Rai sticker with tongue out">
</figure>
