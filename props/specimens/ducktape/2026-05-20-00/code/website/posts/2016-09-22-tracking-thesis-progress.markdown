---
title: Tracking thesis progress
---

I'm currently writing my master's thesis. I'd like to have some idea of how to
answer questions like: How well am I doing this week? Have I been stalled for
some time? How fast can I write when I'm in the zone? How long until I'm
finished?

To help me answer those, I'm gathering data about it using a cron script that
logs the number of pages of my the PDF of my thesis and the number of lines of
code I've written every hour.

This is a technique I copied from a video from the excellent screencast series
[Destroy All Software](https://www.destroyallsoftware.com/screencasts).
The author uses it in several videos. One variant of this data collection idea
he uses is going over the Git history of a project and collecting data
ex post by checking out every historic revision and running a data collection
script over it.

My use of this technique lets me produce a pretty graph that looks like this:

<figure>
<img src="/static/2016-09-22-thesis-tracking-graph.png" alt="Thesis progress tracking graph">
</figure>

Other really nice uses of this technique include asking:

- How many tests were failing in each revision?
- Which authors commited how many lines of code, over time?
- How has the speed of a POST request to `/clients/create` evolved over time?
  Which changes affected it positively and negatively?
- How has the speed of my website's rendering evolved over time?

My crontab line looks like this:

```
0 * * * * log-pagecount
```

The `log-pagecount` script in question is dead simple:

```
#!/bin/bash
set -e

cd /home/prvak/master/text
make >/dev/null
COUNT=`pdfinfo /home/prvak/master/text/thesis.pdf | grep Pages: | cut -d\  -f11`
DATE=`date +%Y%m%d%H%M`
echo $DATE $COUNT >> /home/prvak/misc/master-pagecount-log.log

SLOC_LOGFILE=/home/prvak/misc/master-sloc-log.log
echo "---- CUT ----" >> $SLOC_LOGFILE
echo $DATE >> $SLOC_LOGFILE
cloc /home/prvak/master/code >> $SLOC_LOGFILE
```

The lines-of-code counting is done by the
[cloc](https://github.com/AlDanial/cloc) tool
([AlDanial/cloc](https://github.com/AlDanial/cloc) on GitHub). In Arch Linux,
you can install it by running `pacman -S cloc`.

Then, in `~/misc/sloc-count-to-log.py`, I have a simple script that changes the
format of the lines-of-count logfile:

```
#!/usr/bin/python
with open('master-sloc.tsv', 'w') as f:
    for line in open('master-sloc-log.log'):
        line = line.strip()
        if line.startswith('SUM'):
            f.write("%s %s\n" % (date, line.split(' ')[-1]))
        if line.startswith('2016'):
            date = line
```

Then, I have this Gnuplot script in `~/misc/plot-thesis-stats.gnuplot`:

```
set datafile separator ' '
set xdata time
set y2tics
set timefmt "%Y%m%d%H%M"
set key right bottom
plot \
 "master-pagecount-log.log" u 1:2 w lines axes x1y1 title "Pages", \
 "master-sloc.tsv" u 1:2 w lines axes x1y2 title "SLOC"
```

To see the pretty graph, I do this:

```
~ $ cd ~/misc
~/misc $ gnuplot
(...)
gnuplot> load 'plot-thesis-stats.gnuplot'
```

I have enjoyed procrastinating by writing this post more than looking at
my script munching away training samples for my classifiers.

Enjoy!
