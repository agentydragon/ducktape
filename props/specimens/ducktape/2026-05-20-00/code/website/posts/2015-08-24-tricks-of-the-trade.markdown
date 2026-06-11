---
title: Tricks of the trade
---

In the last year or so, I've been doing some Serious Work at some Serious
Businesses (as opposed to single-digit-developer contract projects I did
before). A very valuable aspect of working in large and established
companies (I in particular went to Google and am now at Dropbox) is having
established procedures and experienced mentors.

Here are a few things I didn't appreciate enough before seeing them in
the proper context:

- Version control. It's more than just a list of versions which you can recover
  later. Proper version control separates the chaos of development into
  manageably-sized atomic changes: commit hygiene is good. If you don't
  know about Git's `-p` flag for `git add` and `checkout`, you should
  learn about it and use it. Always have a look at the diff you're committing
  before you commit it. Squash commits using `rebase -i` when it makes more
  sense. Use branches for separate features, even if you're a single-person
  team. Read your own diffs, at least before you merge into master.

  A nice commit should be a self-contained change. A history of nice commits
  is good for with others or with future you. Forcing your changes to
  be self-contained also requires you to separate your solution into
  independent logical pieces, which may give you new insights into
  the problem you are solving.

- Code review. This means that someone looks at your code and reads it.
  Code reviews are an excellent tool to learn new tricks in your language
  and codebase from more experienced team members. If you don't have anyone
  else to review your code, do it yourself. Actually, just always review your
  code before asking anyone to review it for you. Seeing a diff of the
  code you just changed is a bit like reading an essay you wrote backwards:
  it prevents code blindness.

- Using the right tools and making compromises. You may have learned about
  object-oriented programming and relational databases and microservices
  and you want to do The Right Thing on your next project. Guess what?
  Running this huge `SELECT` with 2 subqueries on every page access takes
  like 800 ms and if you want to get around that you'll have to violate
  2NF and cache it in a new column and you'll have to make your code much
  more complex and ugly to properly flush it. Maybe you had a lot of fun
  learning Angular on this project, but did you consider the data has 8 levels
  of nesting and that the JSON blob is 9 MB large? Go make yourself
  a coffee before you try scrolling to the bottom of the page.

  Most programming principles are just educated heuristics. Apply proper
  judgement when using them. Every rule is meant to be broken, and most
  of them are when used rigidly. Know when it's better to throw away your
  tools and just do a hack that works.
  (By the way, [Software and Mind](http://softwareandmind.com/) is a good
  book on the subject of CS snake oil.)

- Ask. Whenever in doubt. Whenever it takes less time to ask than look in the
  docs. Also, ask when the docs are if you don't know. Chances are someone
  else knows, and will be happy to share. Communicate early, communicate often.
  Ask others for help when debugging weird problems: those are usually typos
  on line 2.

- Use good tools. Continuously integrate (i.e., know when commits fail tests).
  Lint everything: check the syntax of your source code _and configuration_.
  Pick a well-respected style guide for your language and stick to it.
  Tell your linter to scream whenever someone tries to bend the rules.

  Especially if you use a compiled language, use a good build tool.
  If you're using any of the original Google language trinity (C++, Java, Python),
  consider trying [Bazel](http://bazel.io/). Bazel is the open-source
  version of the excellent internal build tool called Blaze. It's fast
  and it has a very elegant design. The configuration files have
  a simple syntax and they explicitly describe the dependencies within
  your project with almost zero boilerplate.

- Don't trust yourself. [Do terrible things to your code.](http://blog.codinghorror.com/doing-terrible-things-to-your-code/)
  Test everything that makes sense - preferably in code.
  Test on varying levels of abstraction: unit tests for small parts,
  integration tests for systems. For testing large systems, you may need
  to spin up several machines and wait an hour or two. It's still _so_ worth it.

- Fight complexity. Complexity is the mind-killer. Remove dead code (and dead
  flags, dead protobuf fields, etc.). The number of moving parts in your
  code exponentially increases the number of states your head must keep track
  of. This is especially painful if the moving parts are independent.
  Do not introduce abstraction unless it gives you a win somewhere.
  Add strict API boundaries between components and force them only to share
  data through it to avoid hidden dependencies.
  Sometimes, when faced with a large and complex problem, it's better to just
  focus on a small part that's important to you right now and just bail out
  when anything else happens.
