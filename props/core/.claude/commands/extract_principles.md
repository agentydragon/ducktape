# Generalize ad-hoc critiques into formal properties of good code

@README.md

## Your task

Read free-form-critique from specimen and to propose generalized principles that we
can extract from them and promote into formal positive properties that good code should have.

We have some already existing formal property definitions, and some sets of free-form critique specimens.
Your task is to propose how to add new property definitions (or edit existing ones) to make our formal definitions
account better for the free-form critiques.

### Existing property definitions

Find and read all `.md` files in `props/` to understand already defined formal properties of good code.

### Specimens

Specimens are in the external repository at `~/code/specimens/`. Read the specimens there to find examples of bad code with critique. Canonical ground‑truth is now recorded in
`issues/*.yaml` files. Use these to identify:

- Issues to flag (should_flag=true) — candidates to generalize into new/edited properties
- Canonical negatives (should_flag=false) — clarify exclusions or tighten definitions as needed.

## Process

Process each specimen in turn. For each of them, read its README.md file. Following the README file, find or obtain
the referenced source code.

Read issues noted in its README.md file and for those that do not yet clearly map to existing definition, read
the subject code together with the critique. Read all such issues, and progressively build up your idea of what
underlying generating principles and heuristics do the critiques come from.

Your goal is to make it so that our formal `props/` contain property definitions that together would generate
all issues in free-form critiques. If a particular critique is already well-covered by existing principles, do not
consider it for proposing a new definition (as it's already well-covered).

If free-form critiques conflict with definitions, consider if you can improve the definitions to fix the contradiction,
and propose your edit as part of your final output file.

Once you have processed and understood all un-formalized issues from all specimen, produce a Markdown file
with your _proposed definitions_ and possibly _proposed definition changes_.

### Goals for proposed definitions

Property definitions:

- Must be _broadly applicable_: they must realistically hit on reasonably common coding tasks/programs
- May be scoped to specific common libraries (e.g., frameworks, test libraries, etc.),
- But cannot be so narrow so as to be only ever hit in ~1 exact usecase (e.g., "when writing a custom CLI utility
  that lists worktrees and talking to the GitHub API, make sure to first collect...").

The goals is to have pre-existing property definitions + your proposed edits/additions together
be a set that would _generate_ all listed violations in specimens.

Aim is to produce properties that are _both_ high _precision_ and high _level_:

- High _precision_ properties: that are _narrowly targeted_ to prevent specific individual common problems,
  and that are very easy to check for compliance without room for interpretation - e.g.: "Python is indented
  with 4 spaces per level".
- High _level_ properties: that are _broadly applicable_ across many different contexts, and that
  capture _general principles_ of good code - e.g.: "code is modular - made from Legos you can take apart, individually
  reason about and build without having to keep the whole in mind", "code is easy to read and understand".

Ideally aim to find principles that are _both_ _high precision_ and _high level_ - those are very valuable,
as they can be widely applied and followed as an easy-to-check guideline. But these two goals are in tension.
Higher level principles have to handle more trade-offs and are harder to specify universally.
Aim to strike a good balance.

For redundancy, you can cover the same issue with multiple proposed properties - e.g., one prohibiting
a specific bad practice (e.g., "always use pytest's built-in fixtures for temporary dirs"), and another high-level
property/principle that _generates_ the prohibition as a side-effect (e.g., "don't reinvent the wheel"),
but can be harder to validate, or have hard-to-specify built-in tradeoffs (e.g., "when should I import
a library vs when should I write my own thing").

### Formatting

Start each definition as a section with a sub-heading, and include in it:

- How you would propose stating the principle as a definition of a positive property that good code should have.
  Use files in `props/` as a style guide.
- Insofar as it's possible, aim to write this definition as a precise definition that one can read and use to determine
  "does code X meet good property Y or not?". This will not always be fully possible - for example in cases where
  there are trade-offs.
- A couple high-signal examples of paired good/bad code - the more the more subtle, general or high-level the principle.
  Aim to cover breadth of the principle and important boundary cases.
- Cases there the property does not apply / circumstances which allow exceptions. In cases where this property
  trades off against other desired properties, where the boundary lies (e.g.: "require design pattern X but only if method
  is >Y lines long").
- References to specific instances from specimen from which you derived this principle, along with explanation of _why_
  you believe this is the correct generalization (i.e., why not narrower, why not tighter, why not more general, why
  not more specific).

### Example of a proposed definition

```markdown
## Names are be descriptive and unambiguous

A name together with associated scope (which context/class/function it exists in) and type information conveys a clear
unamiguous idea of what the named entity represents and how it is to be used.

### Bad example

\`\`\`python
def f(r):
return math.pi _ r _ r

id = get_current()

from foo_module.frontend import util
\`\`\`

### Good example

\`\`\`python
def compute*circle_area(radius: float) -> float:
return math.pi * radius \_ radius

current_user_id = get_current_user_id()

from foo_module.frontend import format_util # 'util' could be anything

for i in range(0, len(users), batch_size): # Traditional i/j/k/... index vars OK in short loops
batch_update_promo_eligibility(users[i:i+batch_size])
\`\`\`

### Exceptions

- Very short names are allowed in short inline lambdas (e.g. `result = parallel_map(inputs, lambda x: x+1)`), short loops.

### Reasoning

Multiple specimen (2000-01-02-bad-code-123, 2001-02-03-another-bad-code) criticize non-descriptive names, including
in variables, functions and module names (`src/frontend/utils.py` in `2000-01-02-bad-code-123`).
This is currently not covered by any existing definition, so I propose "Names are descriptive and unambiguous"
as a new good code property.

Critiques from specimen leave somewhat open how strictly should _all_ names be descriptive, and how to trade off
descriptiveness against brevity (in terms of name length).

**Single-letter names**: Critiques in specimen thoroughly analyze several methods including loop variables named
`i/j/k` and having inline lambdas with single-letter args, finding many problems surrounding these names, but
do not point these out as problematic - so implicitly by omission I believe they are OK.

#### Specimen references

- `specimens/2000-01-02-bad-code-123/README.md`:
  - multiple critiques of multiple naming issues in `src/backend/utils.py` (`f`, `c_id`, `new`);
    loop vars (`for i in ...` on lines 123, 130) not noted as problematic.
  - plus file `src/frontend/utils.py` itself noted as not named specifically and recommended to split into
    inidividual purposeful modules.
- `specimens/2001-02-03-another-bad-code/README.md`: `p` noted as poor parameter name in 5 methods in `lib/processor.js`
```

## Deliverable

Produce a Markdown file with your proposed definitions and reasoning.

In that file, order your proposed definitions by your confidence in them (i.e. that they would in fact
be endorsed as universal properties to be adopted), from highest confidence to lowest confidence.

Also include any edits you propose to pre-existing properties, along with your reasoning.
Your edits can be arbitrarily narrow or wide. If you find a way to improve the definition with a big
refactoring, do propose it.

Note in your doc any critiques from specimen you may not have yet found a way to generalize into
universal definitions.

Once you've written your output file, present your output (additions/edits) to the user and discuss any ambiguities.
Iterate with user on editing and improving definitions, aiming to distill them into adopted definitions.
