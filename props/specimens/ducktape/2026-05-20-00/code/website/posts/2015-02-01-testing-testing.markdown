---
title: Testing, testing...
---

Everything seems to be in order.

This is probably the 5th or 6th iteration of my website. Every about 2 years
or so, I find a new shiny and I decide to completely rewrite it - and do
it right this time.

Two iterations back, I had a website with a huge background image and a cool
MVC backend written in PHP with [Nette Framework](http://nette.org),
which was all the rage back when I was midway through high school.
The site was hosted on `http://mpokorny.eu`, which I sincerely thought
was a good domain back then. The website showed the various
small websites I built and it listed like 83 programming languages I vaguely
knew. It even had cool JavaScript moving thingies in the menu, which I had
been strangely proud of. I hope I have a backup laying somewhere.

<figure>
<img src="/images/nette-tracy.png" style="max-height: 25em;"
     title="What errors look like in an old Nette app" alt="Nette Tracy error debugger screenshot">
<div>What errors look like in an old Nette app</div>
</figure>

I like to return to old source code. It's like looking into the mind of
an earlier me, and it's interesting to recall the thoughts that shaped
the code into how it remained. Of course, it's less pleasant when it
breaks and it's 4:17 AM and someone's losing their head over people getting
`500 Server Error`'s when logging into an old PHP system you wrote 6 years ago
without any tests or documentation. Even indentation discipline would be
too much to ask for. Michael's old self should spend a few hours configuring
his editor, whatever it was. Probably Kate, if I remember correctly.
But I digress.

When I wrote the last version of my website, I did it because I wanted to try
this [XSLT](http://en.wikipedia.org/wiki/XSLT) thingie. It's basically
a templating functional language stored in XML. I had attended a talk on XSLT
by [Jirka Kosek](http://www.kosek.cz/) and I liked how it can be used
to elegantly transform static data into many representations.
This can be useful if you use XML as the "master data format" of everything
you need to present. Of course, there's the "XML is horrible" meme and
everything, but it has some nice ideas.
I decided to have a single-page website without bells and whistles, just
a paragraph or two, some project links and contact info. No JavaScript,
minimal CSS.

<figure>
```xml
<xsl:template name="web-link">
  <xsl:param name="href" />
  <xsl:param name="text" />
  <a class="icon-arrow-right">
    <xsl:attribute name="href">
      <xsl:value-of select="$href" />
    </xsl:attribute>
    <xsl:value-of select="$text" />
  </a>
</xsl:template>
```

<div>XSLT 101: Linking stuff.</div>
</figure>

A single-page personal website is, of course, something XSLT is absolutely
not the right tool for. Doesn't matter, learned something.

I was also lucky enough to grab the `rny.cz` domain, which lets me use the
`pok@rny.cz` mail address, which kind of looks like my last name.
On the other hand, an unexpected downside is that I need to spell it letter
by letter most of the time.

This time, I decided that I will finally do things right. It will be
a moderately simple website like one of those programming blogs that
the cool people have (no tongue-in-cheek intended). And I shall post new
interesting things with an iron regularity. This might also force me to
start writing something journal-like again. The multi-paragraph first
entries in my private journal reduced in time to sentences, mementos,
and finally, reaching perfection, nothing. I wish I could keep a habit better.

<figure>
<img src="/images/haskell-logo-400px.svg" style="width: 150px;"
     title="The Haskell logo" alt="Haskell programming language logo">
</figure>

[Haskell](https://wiki.haskell.org/Haskell) is, without question,
the best programming language in the world (possibly tied with F#).
I just wish I could append a line to a file inside a
`computeSomething :: Int -> Int -> Int` without understanding homeomorphic
endofunctors mapping submanifolds of Banach spaces.

Haskell works fine for my [XMonad](http://xmonad.org/) config.
I wish I could write every small Ruby script I need in Haskell just as quickly.

This website is built with [Hakyll](http://jaspervdj.be/hakyll),
a static site generator in Haskell. They have a neat
[tutorial](http://jaspervdj.be/hakyll/tutorials/01-installation.html)
on how to use it. I'll just try to get it running without breaking
everything. If all goes well, it will eventually force me to learn
my homeomorphic endofunctors, abadon inferior software development
practices like imperative programming, and archieve enlightenment.

<blockquote>"Real men just upload their important stuff on ftp, and
let the rest of the world mirror it!" --
<a href="http://www.webcitation.org/6P8EBZqQX">Linus Torvalds</a></blockquote>
