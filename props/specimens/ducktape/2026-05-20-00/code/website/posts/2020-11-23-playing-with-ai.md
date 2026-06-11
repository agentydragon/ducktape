---
title: Playing with AI
---

<style>
.highlight-on .generated {
  background-color: yellow;
}
</style>

<div id="playing-with-ai-toggle-highlight">
The last about 2 weeks I have taken some time to finally get practical AI experience, so I'm running TensorFlow and all, and making lots of Anki cards.

## Sidenote: Anki for programming is awesome

By the way, Anki cards are so useful for learning how to use libraries fluently without looking things up it's ridiculous.

For example, thanks to a bunch of Clozes, if you give me a CSV dataset, I can define, train and evaluate a network using the Keras functional API, without looking up anything. It means I get quickly to the interesting stuff, and don't waste 80% of my time looking up things I already looked up before. If I see that I'm looking something up for, say, the 3rd time, I just make the cards that would have helped me, and that might be the last time I look that up.

## Can a computer now write similarly well to me?

I saw very interesting examples of how well modern language model can generate text, and I'm wondering how close can I be emulated at this point. Depending on how good a language model is, it could replace the copywriting industry and make a 1000% profit margin on top. And I sort of wonder how close it is to me. Though I'm not sure what would I do if I learned my writing can be replaced. I guess I would continue enjoying it, because it feels like I'm "expressing my soul", and also it's useful to sharpen my thoughts. If my understanding of something is foggy, when I write things down, I can much more easily spot where exactly I'm confused, or what's a question I can't answer, or to realize "hey actually I made a logical error, I no longer believe the conclusion I wanted to justify".

But I guess I could then just as well automate this whole website thing. I put too little stuff on it anyway - if I just got myself to do the work and put my ideas into polished-ish writing, I would write so much more.

I guess I'd still write also because there's some pleasure in someone telling me "oh by the way I read your article the other day, thanks for that tip". And I'd not get that from text I had a Transformer write. Even if it did manage to write a thing as good as me or better, so that people would compliment me for "hey nice thing on your website", it would still make me go a bit "nice!", but it would ring a little hollow, I guess. Praise for work that isn't mine. But then, did the Transformer really "work" for it? Also the work coming up with the architecture and implementing it and making [Write With Transformer](https://transformer.huggingface.co/doc/distil-gpt2) belongs to many many other people.

## Experiment

So I'm going to try it in this article. I will start slowly replacing words by top suggestions ([Write With Transformer distil-gpt2](https://transformer.huggingface.co/doc/distil-gpt2)). I'll start with maybe 90% me, 10% Transformer, and by the time I finish writing this, it'll be 100% Transformer. And I won't, at least for now, tell you which parts are me and which are the generator. That way I won't have just a test of "I can / cannot be replaced by a Transformer", but by asking people which sentences were from me and which were from the Transformer, I'll get more gradual information about the point at which today's Transformers can replace me. From what I read, models like GPT-3 are able to convincingly copy "surface style", and they are able to make simple inferences, but they might make mistakes.

By the way, the footer of [Write With Transformer](https://transformer.huggingface.co/) says: "It is to writing what calculators are to calculus." And that's a nice sentence in that, on a shallow reading, it sounds like a positive comparison. "It is to writing what [things for doing mathematics] are to [field of mathematics]." But I never had a calculator that was any good for calculus. I never saw a calculator with a "derive by x" or "indefinite integral dx". Though now I also wonder why no calculator has it. It would be so useful, and not that hard to implement. Mathematica can integrate most of what you throw at it! And algorithms for integrating broad classes of functions are also totally a thing in literature!

"It is to writing what Mathematica is to calculus"? Sure. That sounds useful. A
tool that can solve 90% of practical problems. Neat. "It is to writing what
calculators are to calculus"? AAA, <span class="generated">you know what? I've been</span>
in many situations where you can have all the calculators you want, and they won't save you from an ugly enough integral.

It sounds like one of those "proverb insults", like "you must be at the top of the Gauss curve".

Also the test of making the Transformer generate parts of the article can show
if it could be useful as a computer-assisted authoring tool.

<span class="generated">I wonder what a problem is</span> there with making the
jobs of some people much easier with AI like this. For example, I have a virtual assistant, and I think it should be possible to augment them with a Transformer. You train the Transformer on chats of the highest rated virtual assistants with customers, and annotate the conversations with times when the virtual assistant had to do something. Then you integrate that Transformer into, say, Google Chat, and add some quick shortcuts, like "Tab" for "autocomplete". I fully expect we should be able to mostly automate conversation like "hello, I hope you're having a nice day".

## Motivation to do my own thing

By the way, the other day I stumbled on [Better Explained](https://betterexplained.com/), and the author has a great article: Surviving (and thriving) on your own: Know Thyself.

And this <span class="generated">is the best of</span> sources of motivation to do my own thing that I've
seen in a while. This article made me realize that yes, there are actually things I want to do. I can just look at my TODO list in my Roam Research database. If I only had the most productive ~8 hours of my time available for all the things I want to learn and make.

So I've been considering going part-time at Google. <span class="generated">For some time I've found
that I</span> just can't muster the energy to be consistently productive after work. And switching contexts is expensive, and gets much more expensive when you switch to a context you haven't seen for a couple days. Like what might happen if one day I do 4 hours of Colab experimentation, then the next week I don't have spare energy after work, and then a week later I open the notebook again, read and go 🤨. It helps to keep notes, and not half-ass the code style too badly, but there's a trade-off between "help future-me quickly get back to productivity on this task" and "spend energy making progress".

Also, with stocks having recovered from COVID and with Bitcoin currently going through Another One Of Those, I've been closely watching how much longer until financial independence.

I am not at the point of "I'll be able to live on this on my current standard
forever". But at a lower standard? Like moving back to Czech Republic? For some
values of "lower standard", yes. And at some some point, the marginal expected
gains from a higher monthly budget will become dominated by the gains of having
more free time. And that <span class="generated">makes</span> it less and less rational to trade financial security for freedom.

And it's not like I can't make money doing things I want to do, either. There's
such a huge spectrum. I can career-steer at Google more boldly, or go part-time
to do my own thing. Or even <span class="generated">if it's a little less expensive</span> It might not be just the same freedom as reducing my hours, or working with a company that's more closely aligned with my interests, or even quitting and doing my thing.

## Still experimenting

By the way, I'm still doing the GPT-3 <span class="generated">of this</span> article thing, with slowly
increasing density. And as I increase the density, I expect <span
class="generated">it will be more
"efficient" to</span> just output tons of filler text. Filler is used in more
articles than crisp sentences that have only narrow meaning. If GPT-3 outputs
"What gives?" after any of my sentences, it will probably get a higher reward
than if it outputs "an endomorphism that is an isomorphism is an automorphism",
just because it's <span class="generated">all too hard to get some extra filler</span> into a place where it would not plausibly fit. What gives?

So expect this piece of writing to slowly degrade from saying something to
saying nothing in so many words. <span class="generated">And I'm doing it in my mind</span> expecting
some successive Tab press to generate either nonsense, or bullshit. I <span
class="generated">m
going to continue to keep it in</span> line with making sense, as far as I'm able to express myself through filler text.

<span class="generated">As a result, I think this will</span> sometimes end up stating things I don't
actually endorse or believe. If that happens, I think I'll also release the
"spoiler" (annotating the sections of this text that are generated) immediately,
so that I don't accidentally say something like "I for one welcome our AI
overlords". Well, <span class="generated">I am sure I will</span> As long as we as a species don't fail the exam.

<span class="generated">As far as I'm concerned, I will continue to put together articles on</span>
whatever interests me, to write code for problems I want solved, and to try to
improve my habits.
If the worldview and "values-view" and "lifestyle" that I want to implement
sticks, <span class="generated">then the same can be said for every</span> positive change it's brought
the past couple weeks. <span class="generated">So, what's really causing this</span> to slip away slowly?
Why have previous times when I held similar viewpoints slipped back into
routine? Maybe it's just because it's been the default for so long for me to
work <span class="generated">through the things I like</span> Slowly, <span class="generated">slowly</span> because of all the panic from ideas like "maybe I might have to leave work or start burning money instead of making it".

<span class="generated">And that will change with time</span> Or so would I hope. <span class="generated">Advertisements</span>
Popular media. <span class="generated">I will be keeping track</span> of all those attention-eaters. I don't want to have a week where all my energy goes into a black hole.

<span class="generated">I just want to keep going.</span> <span class="generated">I don't want to get bored.</span>
Curiosity and learning things and solving problems for people is life.
<span class="generated">I just want to have something that will change and</span> <span class="generated">change and improve my
life.</span> In the direction of more aliveness and generating meaning.
<span class="generated">And to keep doing that I can only have one way or another</span>

<span class="generated">The bottom line is I don't want to do things I want to do in the way that I
want to do.</span> Not with the current default setting where "want" tends to fall back.
<span class="generated">I want to do everything that I can to get my attention. But not with the
current default setting where "want" tends to fall back. I don't want to get
bored. It's just a matter of how much I like it.</span> In the moment.

<span class="generated">
 And for those of you out there who are interested in reading, please, like me, subscribe to my Facebook page and share your thoughts about the situation with me . (By email or to your friends , subscribe to my blog <a href="/">here</a>.) I will be taking the time and effort I have put into writing to make it easier to make things better for you.

And for those of you who aren't interested in reading, please , like me, subscribe to my Facebook page and share your thoughts about the situation with me. (By email or to your friends, subscribe to my blog <a href="/">here</a>.) I will be taking the time and effort I have put into writing to make it easier to make things better for you.
</span>

So <span class="generated"> I'm going to be publishing the article in the second week and I'll be
posting the article in the second week and I 'll be posting the article in the
second week and I'll be posting the article in the second week and I</span> am
<span class="generated">posting the article in the second week and I will be posting the</span> rest <span class="generated">of
the post and I will be posting</span> to RSS and Atom and Hacker News maybe and
<span class="generated">maybe on the same page. If you like the content you see in</span> this website,
thanks, I really <span class="generated">appreciate you! You know the best way to make your next book
available is to check out my Blog</span>

I might be repeating the experiment with different language models, to see which ones can keep going to a higher density without devolving into meaninglessness.
<span class="generated">But if you do it this way, and have more questions, I'll post it again, and I
'll be posting it</span> guess in which week. From what I gather from this experiment, looks like I might not be 100% obsolete just yet. Walk on warm sands.

</div>

<button onclick="document.getElementById('playing-with-ai-toggle-highlight').classList.toggle('highlight-on'); return true;">Highlight generated text by AI</button>
