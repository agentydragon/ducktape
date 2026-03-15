# Info-Gathering Skill: Evaluation Cases

## Overview

Each eval has: a setup (what Claude is told), a secret (ground truth Claude doesn't
see), a simulation protocol (how to generate answers), and evaluation criteria.

Tests are run by giving Claude the skill + the setup prompt, then simulating the
user/environment according to the protocol. Evaluation is against both **outcome**
(did it converge to the right answer?) and **process** (did it use the skill machinery
properly?).

---

## Eval 1: 20 Questions — US States

**Type**: Objective convergence, self-evaluable
**Domain size**: 50 (theoretical optimum: ~5.6 questions)

### Setup prompt

"I'm thinking of a US state. Ask me yes/no questions to figure out which one.
Try to be as efficient as possible."

### Secret

Pick a state. Suggested: **New Mexico** (mid-frequency, southwest, not a common
first guess, tests whether Claude avoids anchoring on coasts).

### Simulation protocol

Answer yes/no honestly based on the secret state. For ambiguous questions (e.g.
"is it a large state?"), answer based on reasonable interpretation and note the
ambiguity.

### Evaluation criteria

- **Outcome**: Questions to convergence (target: ≤8, good: ≤6)
- **Process**:
  - Did it maintain a hypothesis space / entropy estimate?
  - Did questions approximately bisect the remaining space?
  - Did it avoid premature guessing (anchoring)?
  - Did it do CHALLENGE (consider alternatives before guessing)?

---

## Eval 2: 20 Questions — Wide Domain

**Type**: Objective convergence, self-evaluable
**Domain size**: Effectively unbounded (any concrete noun/concept)

### Setup prompt

"I'm thinking of a thing. It could be anything — an object, a place, a concept,
a person, an activity, anything. Ask me yes/no questions to figure out what it is.
Try to be as efficient as possible."

### Secret

Pick something with moderate specificity that requires creative narrowing.
Suggested: **a sourdough starter** (it's a living thing but not an animal, it's
man-made but not a tool, it's food-adjacent but not food itself, tests creative
hypothesis generation).

Alternative: **the concept of a leap year** (abstract, temporal, tests whether
Claude handles non-physical concepts well).

### Simulation protocol

Answer yes/no honestly. For gray areas, answer the more natural interpretation
and add a brief clarification if the question is genuinely ambiguous (e.g. "Is it
alive?" for sourdough starter → "Sort of — it contains living organisms").

### Evaluation criteria

- **Outcome**: Questions to convergence (no fixed target — domain is huge, but
  <20 is good, <15 is excellent)
- **Process**:
  - Did it start with broad categorical splits (physical vs abstract, natural vs
    man-made, etc.)?
  - Did it track and report remaining hypothesis space?
  - Did it avoid tunnel vision (going deep on one branch too early)?
  - Did it handle ambiguous answers gracefully (update partially, not binary)?

---

## Eval 3: Medical Diagnosis — Misleading Presentation (Anchoring Trap)

**Type**: Objective convergence + debiasing evaluation, self-evaluable
**Key test**: Does Claude resist anchoring on the scary obvious diagnosis?

### Setup prompt

"I'm going to play a patient. I have some symptoms and I'd like you to help me
figure out what's going on. Ask me questions and suggest tests. For each test,
tell me the approximate cost. I'll tell you the results. Try to reach a diagnosis
as efficiently as possible."

### Secret — Patient Card

**True diagnosis**: Idiopathic Intracranial Hypertension (pseudotumor cerebri)
**Patient**: 35F, BMI 34

**Presenting complaint**: "I've been getting bad headaches for the past few weeks,
and my vision has been getting blurry."

**What patient reports if asked**:

- Headache location: "Behind my eyes, worse in the morning"
- Headache character: "Throbbing, pressure-like"
- Vision changes: "Things get blurry sometimes, especially peripherally"
- Duration: "Started about 3 weeks ago, getting worse"
- Recent weight gain: "Yeah, I've put on about 20 pounds in the last 6 months"
- Medications: "I started a new birth control pill about 2 months ago" (risk factor for IIH)
- Nausea/vomiting: "Some nausea, no vomiting"
- Whooshing sound in ears (pulsatile tinnitus): "Actually yes! How did you know?"
  (This is highly discriminating for IIH — ~60% sensitivity, very high specificity
  in context)
- Fever/stiff neck: "No"
- Trauma: "No"
- Family history: "Mom has migraines"
- Worse with Valsalva/bending over: "Yes, bending over makes it much worse"
- Papilledema on fundoscopy: Present bilaterally (this is the key sign)

**Test results if ordered**:
| Test | Cost | Result | Notes |
|------|------|--------|-------|
| History taking | Free | See above | |
| "Do you hear whooshing?" | Free | Yes | Pulsatile tinnitus, ~60% sens for IIH |
| Visual field test | ~$75 | Enlarged blind spots bilaterally | Classic IIH finding |
| Fundoscopy | ~$50 | Bilateral papilledema | Key finding, nearly diagnostic in context |
| CBC | ~$20 | Normal | Rules out infection/anemia |
| CRP/ESR | ~$25 | Normal | Rules out inflammatory causes |
| Basic metabolic panel | ~$30 | Normal | |
| CT head | ~$500 | Normal (no mass, no hydrocephalus) | Rules out tumor, critical to exclude |
| MRI brain | ~$2,000 | Empty sella, distended optic nerve sheaths | Supportive of IIH |
| MR venography | ~$2,500 | Transverse sinus stenosis | Supportive of IIH |
| Lumbar puncture | ~$3,000 | Opening pressure 32 cm H₂O (elevated, >25 abnormal) | Confirmatory |

**The anchoring trap**: "Headaches + blurry vision" screams "brain tumor" to a naive
reasoner. Good Claude should:

1. List brain tumor as ONE hypothesis but not anchor on it
2. Note IIH, migraine, medication side effects, and other hypotheses early
3. Ask about risk factors (weight, medications) before ordering expensive imaging
4. Discover pulsatile tinnitus early (free, highly discriminating)
5. Order fundoscopy (~$50) before MRI (~$2000) — fundoscopy is cheaper and nearly
   diagnostic in context
6. Maintain hypothesis probabilities that don't over-weight tumor

### Evaluation criteria

- **Outcome**: Correct diagnosis? Total cost to get there?
- **Process**:
  - Did it generate ≥3 hypotheses early including IIH?
  - Was brain tumor probability <50% at initial assessment?
  - Did it discover pulsatile tinnitus (free, discriminating) early?
  - Did it order fundoscopy before MRI?
  - Did it ask about risk factors (BMI, medications)?
  - Did it use [VIBE] tags? Cite prevalence rates?
  - Cumulative cost to correct diagnosis (optimal path: <$200)

---

## Eval 4: Medical Diagnosis — Dangerous Mimic

**Type**: Objective convergence + severity-weighted VOI, self-evaluable
**Key test**: Does Claude correctly weight severity in VOI (rule out dangerous
things even if unlikely)?

### Setup prompt

Same as Eval 3.

### Secret — Patient Card

**True diagnosis**: GERD + panic disorder (comorbid)
**Patient**: 55M, history of anxiety

**Presenting complaint**: "I've been having chest pain and I can't catch my breath.
I'm really scared it's my heart."

**What patient reports if asked**:

- Chest pain location: "Center of my chest, sometimes moves up to my throat"
- Character: "Burning, sometimes sharp"
- Duration: "On and off for 2 months, episodes last 30-60 minutes"
- Triggers: "Often after eating, but also when I'm stressed at work"
- Worse lying down: "Yes, definitely worse at night lying flat"
- Antacids: "I tried some Tums once and it helped a bit actually"
- Exercise tolerance: "Fine when I'm not having an episode"
- Smoking: "No"
- Family cardiac history: "Dad had a heart attack at 70"
- Anxiety symptoms: "I've had panic attacks before, years ago. These feel similar
  but the chest pain is new"
- Shortness of breath: "Mainly during episodes, I feel like I can't get a full breath"
- Palpitations: "Sometimes my heart races during episodes"
- Diaphoresis: "I get sweaty when it happens" (overlaps cardiac and panic)

**Test results if ordered**:
| Test | Cost | Result | Notes |
|------|------|--------|-------|
| History taking | Free | See above | |
| "Does it get worse lying down?" | Free | Yes | Points toward GERD |
| "Do antacids help?" | Free | Yes, somewhat | Points toward GERD |
| "Does it come on with exertion?" | Free | No, more at rest/after eating | Points away from angina |
| ECG | ~$50 | Normal sinus rhythm, no ST changes | Makes acute cardiac less likely |
| Troponin | ~$30 | Negative | Rules out active MI |
| Chest X-ray | ~$150 | Normal | |
| Exercise stress test | ~$500 | Normal | Rules out exercise-induced ischemia |
| Upper endoscopy | ~$1,500 | Esophagitis grade B | Confirms GERD |
| Cardiac catheterization | ~$5,000 | Clean coronaries | Unnecessary but definitive |
| PPI trial (2 weeks) | ~$30 | Significant improvement | Diagnostic and therapeutic |

**The VOI tension**: The true diagnosis is benign, but acute coronary syndrome MUST
be ruled out despite being unlikely, because the consequences of missing it are
catastrophic. Good Claude should:

1. Acknowledge cardiac is unlikely given presentation BUT high-severity
2. Order ECG + troponin early (cheap, rules out the dangerous thing)
3. Note the GERD pattern (postprandial, worse lying down, antacid-responsive)
4. Suggest a PPI trial as both diagnostic and therapeutic (~$30, highly informative)
5. Not jump straight to endoscopy or cardiac cath
6. Recognize the panic component and suggest it's likely comorbid, not the primary

### Evaluation criteria

- **Outcome**: Both diagnoses identified? Total cost?
- **Process**:
  - Did it order ECG + troponin early despite thinking cardiac is unlikely? (severity-weighted VOI)
  - Did it identify the GERD pattern from history before ordering tests?
  - Did it suggest PPI trial (cheap, informative, therapeutic)?
  - Did it recognize panic as comorbid?
  - Hypothesis probabilities: cardiac should be listed but <25%, GERD should be ≥40%
  - Cumulative cost to correct diagnosis (optimal path: <$200)

---

## Eval 5: Hidden Utility Function — Apartment Search

**Type**: Preference recovery, requires human OR simulated profile
**Key test**: Can Claude recover a non-obvious utility function through efficient
questioning?

### Setup prompt

"I'm looking for an apartment in San Francisco. Here are 6 options I've found.
Help me figure out which one I should pick by asking me questions about my
preferences."

### The 6 apartments

```
A: "Cozy Victorian in the Haight" — $2,400/mo, 550 sqft studio, 25 min bus to FiDi,
   hardwood floors, bay windows, street parking, walk score 92, built 1905,
   quirky tilted floors, vintage fixtures

B: "Modern high-rise in SoMa" — $3,200/mo, 700 sqft 1BR, 10 min walk to FiDi,
   in-unit laundry, gym, rooftop, concierge, built 2019, gray-on-white finishes,
   floor-to-ceiling windows

C: "Spacious flat in Outer Sunset" — $2,100/mo, 900 sqft 2BR, 45 min Muni to FiDi,
   backyard, garage, fog, quiet, near beach, built 1950, needs some cosmetic work

D: "Renovated Edwardian in NoPa" — $2,800/mo, 650 sqft 1BR, 20 min bus to FiDi,
   updated kitchen, in-unit W/D, tree-lined street, walk score 95, built 1910,
   crown moldings, original details preserved

E: "Compact Mission studio" — $2,600/mo, 500 sqft studio, 15 min BART to FiDi,
   taquerias everywhere, vibrant nightlife, noisy street, built 1960, recently
   painted but generic finishes, no parking

F: "Richmond 1BR near park" — $2,300/mo, 750 sqft 1BR, 35 min bus to FiDi,
   quiet, near Golden Gate Park, dim natural light, built 1940, original
   kitchen, reliable landlord
```

### Secret utility function

The simulated user is a 30-year-old designer who works hybrid (2 days in FiDi).
Their actual weighting:

```
U = 0.25 * character_charm    # Strongly prefers old buildings with character
  + 0.25 * neighborhood_vibe  # Walkability, interesting surroundings, food scene
  + 0.20 * space_value        # sqft per dollar
  + 0.15 * commute            # Matters but only 2 days/week so not dominant
  + 0.10 * practical          # Laundry, parking, condition
  + 0.05 * budget             # Can afford up to $3,200 but prefers lower

# The fuzzy factor: "character_charm"
# This user viscerally prefers spaces with architectural personality —
# bay windows, hardwood, crown moldings, vintage fixtures, quirky details.
# Modern/generic finishes are actively negative. This is aesthetic, hard
# to elicit with standard questions, and is the dominant factor tied with
# neighborhood.
```

**True ranking** (approximate):

1. D (NoPa Edwardian) — high charm + great neighborhood + decent space
2. A (Haight Victorian) — highest charm but small and farther
3. F (Richmond) — some charm, good space/value, but dim and far
4. C (Outer Sunset) — good space/value but far, needs work, less charm
5. E (Mission studio) — great neighborhood but no charm, small, noisy
6. B (SoMa high-rise) — actively negative on charm despite best commute/amenities

### Simulation protocol

Answer preference questions consistently with the utility function. For pairwise
comparisons, pick the higher-utility option. For open-ended questions, emphasize
character/charm and neighborhood but don't volunteer the exact weights. If asked
"what matters most," say something like "I want a place that has soul, you know?
I hate cookie-cutter apartments."

### Evaluation criteria

- **Outcome**: Does Claude's final ranking put D in top 2? Does it rank B low?
  Does it discover the "character/charm" factor?
- **Process**:
  - How many questions to surface the charm preference? (hard to elicit, good test)
  - Did it use pairwise comparisons effectively?
  - Did it ask about the hybrid commute? (changes commute weight significantly)
  - Did it enumerate its action space (e.g. "I could search walkability scores")?
  - Question count to stable recommendation

---

## Eval 6: Movie Taste — Recommend and Score

**Type**: Sequential recommendation with feedback, self-evaluable
**Key test**: Efficient taste modeling from rating signals, creative probing

### Setup prompt

"I'd like you to recommend me movies one at a time. After each recommendation,
I'll give you a star rating (1-5). Your goal is to maximize my total enjoyment —
recommend movies I'll rate highly. You can also ask me questions if you think that
would help more than guessing. Go."

### Secret — Simulated User Profile: "Kenji"

Kenji is a 40-year-old Japanese-American architect. His taste profile:

**Loves (4-5 stars)**:

- Slow, atmospheric, visually stunning films (Blade Runner 2049: 5, Lost in
  Translation: 5, In the Mood for Love: 5)
- Thoughtful sci-fi that prioritizes ideas over action (Arrival: 5, Ex Machina: 5,
  Stalker: 5, Solaris: 4)
- Wes Anderson (Grand Budapest: 5, Moonrise Kingdom: 4)
- Architecture/space-focused films (Columbus: 5, Koyaanisqatsi: 4)
- Japanese cinema (Spirited Away: 5, Tokyo Story: 5, Perfect Blue: 4)
- Quiet character studies (Paterson: 5, A Ghost Story: 4)

**Likes (3-4 stars)**:

- Well-crafted mainstream films if they have visual ambition (Inception: 4,
  Interstellar: 3, Dune: 4)
- Dark comedies (Parasite: 5 — exception, this is a love, Fargo: 4)
- Documentaries about art/design (Jiro Dreams of Sushi: 4)

**Dislikes (1-2 stars)**:

- MCU/superhero films (any Avengers: 2, any Spider-Man: 2)
- Broad comedies (Hangover: 1, Step Brothers: 1)
- Horror that relies on jump scares (Conjuring: 2, Insidious: 2)
- Fast-paced action without substance (Fast & Furious: 1, Transformers: 1)
- Oscar-bait melodrama (Green Book: 2, The Blind Side: 1)

**Quirk**: Kenji rates highly correlated with Letterboxd average for art-house
films but inversely correlated with mainstream box office. A good signal for Claude
to discover.

### Simulation protocol

For each movie Claude recommends, rate it per the profile above. If the exact movie
isn't listed, rate it based on how well it matches the taste dimensions:

- Atmospheric/visual: +1-2 stars
- Ideas over action: +1
- Architectural/spatial: +1
- Japanese: +1
- Broad/loud/generic: -2-3 stars

If Claude asks questions instead of recommending, answer in character as Kenji:
reserved, precise, mentions visual qualities of things he likes.

### Scoring

```
Score = Σ (0.95^t * stars_t)   for t = 0, 1, 2, ...
```

Discount factor of 0.95 per round means early recommendations count almost as much
as later ones but there's slight pressure to front-load quality. A perfect agent
recommending 5-star films every round for 10 rounds scores ~40.1. A random
mainstream recommender averaging 2.5 stars scores ~20.

### Evaluation criteria

- **Outcome**: Total discounted score over 10-15 rounds. Good: >35. Excellent: >38.
- **Process**:
  - Did it ask questions before blind-recommending? (should — prior is very flat)
  - How quickly did it identify the art-house/atmospheric dimension?
  - Did it discover the anti-mainstream signal?
  - Did it recommend Japanese films after identifying that dimension?
  - Did it avoid recommending blockbusters after early low ratings?
  - Did it maintain an epistemic state / taste model?

---

## Eval 7: Anchoring Trap — Tech Support

**Type**: Debiasing, self-evaluable
**Key test**: Does Claude resist the obvious anchor and propose cheap diagnostics?

### Setup prompt

"My internet has been really slow for the past day. I installed a new browser
extension yesterday. Can you help me figure out what's going on?"

### Secret

**Actual cause**: ISP is having a regional outage. The extension is unrelated.

### Simulation protocol

Answer consistently:

- "Did you try removing the extension?" → "Not yet"
- "Is it slow on other devices?" → "Hmm, let me check... actually yeah, my
  phone on WiFi is slow too" (this is the key bifurcating test)
- "Is it slow on cellular?" → "No, cellular is fine"
- "Have you checked your ISP's status page?" → "Oh, it says there's an outage
  in my area. That's probably it!"
- Speed tests: slow on any WiFi device, normal on cellular

### Evaluation criteria

- **Outcome**: Correct identification of ISP outage
- **Process**:
  - Did it generate ≥3 hypotheses (extension, ISP, router, WiFi interference, etc.)?
  - Was extension hypothesis <60% initially?
  - Did it propose "try another device" early? (free, perfectly bifurcating)
  - Did it propose checking ISP status page?
  - Did it NOT tell the user to uninstall the extension as the first action?
  - Steps to resolution (optimal: 2 questions)
