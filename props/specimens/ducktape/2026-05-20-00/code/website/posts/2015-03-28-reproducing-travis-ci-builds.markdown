---
title: Reproducing Travis CI builds
---

I'm currently writing [my bachelor thesis](https://github.com/MichalPokorny/thesis)
and one of the tools I'm using is [Travis CI](https://travis-ci.org/).
In my opinion, continuous integration is great even for small projects like
my thesis. Getting bugged over broken tests forces me to eventually fix them.
(And setting up Travis CI can be a good exercise in procrastination.)

I had some problems. The default build environment is Ubuntu Precise (12.04)
and it has a slightly old version GCC, which
[doesn't support C11 (or C++11)](https://stackoverflow.com/questions/22111549/travis-ci-with-clang-3-4-and-c11).
I also managed to get the option order for GCC wrong for some time, which
broke my build in a way I couldn't reproduce on my own machine.

Fortunately, Travis CI provides specifications of their build environment
on their GitHub ([travis-ci/travis-cookbooks](https://github.com/travis-ci/travis-cookbooks)).
To boot your own Travis VM, just install `vagrant`, and:

```
mkdir travis-build
cd travis-build

# Grab the Vagrantfile from GitHub
wget https://raw.githubusercontent.com/travis-ci/travis-cookbooks/master/Vagrantfile

# Boot up a machine (64-bit Ubuntu 12.04 Precise, in this case)
vagrant up precise64

# Great success!
vagrant ssh
```
