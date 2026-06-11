---
title: Stock correlation calculator
---

I have some stocks and I try to keep my portfolio reasonably balanced.
One thing you may want to know about is how much do your assets _correlate_.
The correlation of stocks A and B is just the correlation of the daily returns
of A with the daily returns of B, or, in other words, how much do the two
stocks tend to move in the same daily direction.

Usually, high correlation means that the two stocks are affected by the same
type of news. For example, you could reasonably expect AAPL (Apple) and GOOG
(Google) to be highly correlated, because both are technology mamooths.

One reason why you might want to keep the correlation of the stocks in your
portfolio as low as possible is safety against bad news. If China bans
computers, everything related to computers goes out of the window, but e.g.
manufacturers of chemicals might remain more or less unaffected.

There are some online tools that can calculate the correlation of stocks, but
I didn't like any of these enough. Some of them only let you compare two
stocks at a time, but I actually want to see the whole correlation _matrix_
of everything I have. Some only let you make N queries and then they tell you
to go give them your money. So, I wrote my own. It's at
[https://stockton-prvak.rhcloud.com](https://stockton-prvak.rhcloud.com).

It prints correlation matrices. I also played around with visualizing the
stocks as "planets" that are attracted or repulsed based on their correlation.
The visualization is still pretty buggy, but fun to look at.
If you'd like to see how I did this, have a look at
[https://github.com/MichalPokorny/stockton](https://github.com/MichalPokorny/stockton).
