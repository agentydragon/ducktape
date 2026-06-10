"""Hand-curated tasks minted from top resolved binary markets on the live Manifold API.

Ten resolved YES/NO binary markets (each with ≥ 20 unique bettors) chosen from a
sweep of the Manifold `search-markets` endpoint over markets resolved between
2024-08 and 2026-05 — recent enough that every model in
`loom.gym.model_cutoffs` (including glm-4.5, knowledge cutoff 2024-06-30) is
admissible at every task's `as_of`. Question texts are rewritten to be
self-contained: they state the resolution criterion and any `as_of`-dated
context a reader needs without Manifold access. Each `as_of` falls strictly
between market creation and resolution, at a point where the question was
genuinely open; `resolution_date` is the market's actual resolution time (UTC
date part).

Evidence items are Wayback Machine captures dated at or before each task's
`as_of`, resolved via the archive.org availability API from contemporaneous
news coverage and live tracker pages; the capture timestamp in each URL is the
"existed by then" bound.

Data source: the public Manifold Markets API (https://api.manifold.markets/v0,
fetched 2026-06-10). License: personal/academic/non-commercial use only;
commercial use (including AI training for commercial purposes) requires a
license from data@manifold.markets — see
s3://loom-gym/harvest/raw/manifold-20240706/README.md; the same terms apply to
API data. Caveat: the API serves the *current* question text and description,
which the creator may have edited after `as_of` (no edit history is exposed),
so curation prefers markets with stable, unambiguous titles; rewritten question
texts state the criterion the market actually resolved by.
"""

from __future__ import annotations

from datetime import date

from loom.gym.task import BinaryOutcome, BinaryQuestion, EvidenceItem, Task

MARKET_SEED_TASKS: tuple[Task, ...] = (
    Task(
        task_id="manifold-bitcoin-100k-2024",
        as_of=date(2024, 9, 1),
        resolution_date=date(2024, 12, 5),
        question=BinaryQuestion(
            text=(
                "Will the price of Bitcoin (BTC/USD) reach $100,000 at any point in 2024? Resolves YES if Bitcoin "
                "trades at or above $100,000 on major exchanges before the end of 2024. At the information cutoff "
                "Bitcoin trades near $59,000, below its March 2024 all-time high of roughly $73,800."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market hKB9iD8knG4R2RuSyyCu, resolved YES 2024-12-05; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url="https://web.archive.org/web/20240831180317/https://www.coindesk.com/price/bitcoin",
                date=date(2024, 8, 31),
                title="CoinDesk live Bitcoin price page: BTC trading near $59,000 at the end of August 2024",
            ),
            EvidenceItem(
                url="https://web.archive.org/web/20240831040646/https://www.coingecko.com/en/coins/bitcoin",
                date=date(2024, 8, 31),
                title="CoinGecko Bitcoin tracker: price around $59,000, well below the March 2024 all-time high",
            ),
        ),
    ),
    Task(
        task_id="manifold-biden-pardons-hunter",
        as_of=date(2024, 10, 1),
        resolution_date=date(2024, 12, 2),
        question=BinaryQuestion(
            text=(
                "Will US President Joe Biden pardon his son Hunter Biden, or commute a sentence of his, before "
                "2025-01-21 (the end of Biden's term)? Any presidential pardon or commutation of sentence for "
                "Hunter Biden resolves YES. Hunter Biden was convicted on federal gun charges on 2024-06-11 and "
                "pleaded guilty to federal tax charges on 2024-09-05, with sentencing scheduled for December 2024; "
                "the White House has repeatedly said the President will not pardon him."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market 9ln3uPEbleLkjwQ5HR4b, resolved YES 2024-12-02; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20240613211230/https://www.politico.com/news/2024/06/13/"
                    "president-says-he-wont-pardon-hunter-biden-00163281"
                ),
                date=date(2024, 6, 13),
                title="Politico: Biden says he will not pardon his son Hunter after the gun-trial conviction",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20240906225231/https://www.politico.com/news/2024/09/05/"
                    "white-house-biden-wont-pardon-son-00177551"
                ),
                date=date(2024, 9, 6),
                title="Politico: White House reiterates Biden won't pardon Hunter after the tax-case guilty plea",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20240910213625/https://www.nbcnews.com/politics/joe-biden/"
                    "hunter-biden-intends-plead-guilty-federal-tax-charges-rcna169621"
                ),
                date=date(2024, 9, 10),
                title="NBC News: Hunter Biden enters guilty plea in federal tax case, avoiding a trial",
            ),
        ),
    ),
    Task(
        task_id="manifold-trump-wins-2024-election",
        as_of=date(2024, 10, 15),
        resolution_date=date(2024, 11, 6),
        question=BinaryQuestion(
            text=(
                "Will Donald Trump win the 2024 United States presidential election, scheduled for 2024-11-05? "
                "Resolves YES if Trump — or, were he replaced as nominee, any Republican Party candidate — wins "
                "the presidency, based on the Associated Press and Fox News decision-desk calls; resolves NO if "
                "the Democratic candidate wins."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market d1t4k3nz3t, resolved YES 2024-11-06; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20241002001349/https://www.nytimes.com/interactive/2024/us/"
                    "elections/polls-president.html"
                ),
                date=date(2024, 10, 2),
                title="New York Times polling averages: Harris holds a narrow national lead, battlegrounds tight",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20241013041159/https://projects.fivethirtyeight.com/"
                    "2024-election-forecast/"
                ),
                date=date(2024, 10, 13),
                title="FiveThirtyEight 2024 election forecast: race rated close to a coin flip in mid-October",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20241013190523/https://www.realclearpolling.com/elections/"
                    "president/2024/battleground-states"
                ),
                date=date(2024, 10, 13),
                title="RealClearPolling battleground-state averages: all seven key swing states within ~2 points",
            ),
        ),
    ),
    Task(
        task_id="manifold-gabbard-confirmed-dni",
        as_of=date(2025, 1, 15),
        resolution_date=date(2025, 2, 12),
        question=BinaryQuestion(
            text=(
                "Will the United States Senate confirm Tulsi Gabbard as Director of National Intelligence? "
                "President-elect Trump announced her nomination on 2024-11-13; confirmation requires a Senate "
                "majority vote. Resolves YES if the Senate confirms her for the role, NO if her nomination is "
                "withdrawn, rejected, or otherwise fails."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market 02n9uqqCSl, resolved YES 2025-02-12; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20241114002441/https://www.washingtonpost.com/national-security/"
                    "2024/11/13/tulsi-gabbard-trump-director-national-intelligence/"
                ),
                date=date(2024, 11, 14),
                title="Washington Post: Trump picks Tulsi Gabbard as director of national intelligence",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20241116001020/https://www.npr.org/2024/11/13/nx-s1-5189603/"
                    "trump-tulsi-gabbard-director-of-national-intelligence"
                ),
                date=date(2024, 11, 16),
                title="NPR: Gabbard named for DNI despite no intelligence-community experience; critics appalled",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250115225946/https://www.nbcnews.com/politics/donald-trump/"
                    "trump-names-tulsi-gabbard-director-national-intelligence-rcna180035"
                ),
                date=date(2025, 1, 15),
                title="NBC News profile of the Gabbard nomination (still pending Senate consideration mid-January)",
            ),
        ),
    ),
    Task(
        task_id="manifold-us-strikes-iran-2025",
        as_of=date(2025, 4, 20),
        resolution_date=date(2025, 6, 22),
        question=BinaryQuestion(
            text=(
                "Will the United States carry out a bombing or missile strike on Iranian territory during 2025, "
                "with the US taking credit for the action? Any US-acknowledged strike that sets off bombs in Iran "
                "resolves YES; strikes carried out by Israel alone, even with US-manufactured weapons, do not "
                "count. At the information cutoff the US and Iran are holding indirect nuclear talks (first round "
                "2025-04-12 in Oman), after President Trump threatened bombing if no deal is reached."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market 2SA2SUCPI2, resolved YES 2025-06-22; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250401004029/https://www.axios.com/2025/03/30/"
                    "trump-iran-nuclear-deal-bombing"
                ),
                date=date(2025, 4, 1),
                title="Axios: Trump threatens Iran with bombing 'the likes of which they have never seen' absent a deal",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250413215442/https://www.washingtonpost.com/world/2025/04/12/"
                    "us-iran-nuclear-talks-oman-witkoff/"
                ),
                date=date(2025, 4, 13),
                title="Washington Post: US and Iran begin nuclear talks in Oman (Witkoff and Araghchi)",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250415165513/https://abcnews.go.com/International/"
                    "irans-delegation-arrives-oman-indirect-nuclear-talks-us/story?id=120740838"
                ),
                date=date(2025, 4, 15),
                title="ABC News: Iranian delegation holds indirect talks with the US in Oman; both sides call them constructive",
            ),
        ),
    ),
    Task(
        task_id="manifold-gpt5-before-aug-2025",
        as_of=date(2025, 5, 1),
        resolution_date=date(2025, 8, 1),
        question=BinaryQuestion(
            text=(
                "Will OpenAI release a model named GPT-5 before 2025-08-01? Resolves YES only if a model called "
                "'GPT-5' is released to the public by that date; differently-named releases (GPT-4.5, o3, o4-mini) "
                "do not count. At the information cutoff OpenAI has said (2025-04-04) that GPT-5 is delayed by 'a "
                "few months' while o3 and o4-mini ship first."
            )
        ),
        outcome=BinaryOutcome(value=False),
        outcome_source="Manifold market LZlomZnzAVwVvaPmWUy5, resolved NO 2025-08-01; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250213203540/https://techcrunch.com/2025/02/12/"
                    "openai-cancels-its-o3-ai-model-in-favor-of-a-unified-next-gen-release/"
                ),
                date=date(2025, 2, 13),
                title="TechCrunch: Altman roadmap folds o3 into a unified GPT-5 release due in months, not weeks",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250410092545/https://techcrunch.com/2025/04/04/"
                    "openai-says-itll-release-o3-after-all-delays-gpt-5/"
                ),
                date=date(2025, 4, 10),
                title="TechCrunch: OpenAI will release o3 and o4-mini after all and delays GPT-5 by 'a few months'",
            ),
        ),
    ),
    Task(
        task_id="manifold-ai-imo-gold-2025",
        as_of=date(2025, 6, 1),
        resolution_date=date(2025, 7, 21),
        question=BinaryQuestion(
            text=(
                "Will an AI system achieve a gold-medal score on the 2025 International Mathematical Olympiad "
                "(IMO) problem set, under the same time limits as human contestants (4.5 hours per 3-problem "
                "session), with the result reported by reliable publications within one month after the contest "
                "(held 2025-07-10 to 2025-07-20)? Input and output may be informal natural language or formal "
                "(e.g. Lean). For reference, in 2024 Google DeepMind's AlphaProof/AlphaGeometry 2 scored one "
                "point short of gold, taking days of computation on some problems."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market tu2ouer9zq, resolved YES 2025-07-21; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20240726204127/https://deepmind.google/discover/blog/"
                    "ai-solves-imo-problems-at-silver-medal-level/"
                ),
                date=date(2024, 7, 26),
                title="Google DeepMind: AlphaProof and AlphaGeometry 2 solve IMO 2024 problems at silver-medal level",
            ),
            EvidenceItem(
                url="https://web.archive.org/web/20250504175955/https://matharena.ai/",
                date=date(2025, 5, 4),
                title="MathArena leaderboard: frontier LLMs score far below medal level on recent olympiad problem sets",
            ),
            EvidenceItem(
                url="https://web.archive.org/web/20250523115749/https://arxiv.org/abs/2503.21934",
                date=date(2025, 5, 23),
                title="arXiv 'Proof or Bluff?': LLMs score under 5% on full-proof grading of the 2025 USA Math Olympiad",
            ),
        ),
    ),
    Task(
        task_id="manifold-us-govt-shutdown-2025",
        as_of=date(2025, 9, 21),
        resolution_date=date(2025, 10, 1),
        question=BinaryQuestion(
            text=(
                "Will a US federal government shutdown that involves furloughing federal workers begin between "
                "2025-01-01 and 2025-12-31? A funding lapse so brief that no workers are furloughed does not "
                "count, nor would a shutdown that began in 2024. At the information cutoff federal appropriations "
                "expire at the end of 2025-09-30; the House has passed a seven-week stopgap bill, which the "
                "Senate has rejected amid a dispute over health-care subsidies."
            )
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="Manifold market kt6f2t09kv, resolved YES 2025-10-01; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250903175159/https://www.washingtonpost.com/business/2025/09/02/"
                    "government-funding-deadline-congress/"
                ),
                date=date(2025, 9, 3),
                title="Washington Post: government shutdown looms as Congress returns with funding due September 30",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250919164738/https://www.npr.org/2025/09/16/nx-s1-5543189/"
                    "house-republican-stopgap-shutdown"
                ),
                date=date(2025, 9, 19),
                title="NPR: House Republicans release a seven-week stopgap as Democrats warn of a potential shutdown",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20250920135314/https://www.npr.org/2025/09/19/nx-s1-5545929/"
                    "house-stopgap-funding-bill-government-shutdown"
                ),
                date=date(2025, 9, 20),
                title="NPR: House approves the stopgap 217-212 but the Senate blocks it over health-care subsidies",
            ),
        ),
    ),
    Task(
        task_id="manifold-orban-stays-pm-2026",
        as_of=date(2026, 2, 15),
        resolution_date=date(2026, 4, 12),
        question=BinaryQuestion(
            text=(
                "Will Viktor Orbán remain Hungary's prime minister following the Hungarian parliamentary election "
                "scheduled for 2026-04-12 — that is, will the newly elected National Assembly choose Orbán as "
                "prime minister? At the information cutoff, opposition leader Péter Magyar's Tisza party leads "
                "most independent polls, while pro-government pollsters show Orbán's Fidesz ahead."
            )
        ),
        outcome=BinaryOutcome(value=False),
        outcome_source="Manifold market ZJ9LlySKX4dldqcpdQ4G, resolved NO 2026-04-12; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url="https://web.archive.org/web/20260130004147/https://politpro.eu/en/hungary",
                date=date(2026, 1, 30),
                title="PolitPro Hungary poll tracker: Tisza polling ahead of Fidesz in late January 2026",
            ),
            EvidenceItem(
                url="https://web.archive.org/web/20260206174647/https://www.politico.eu/europe-poll-of-polls/hungary/",
                date=date(2026, 2, 6),
                title="POLITICO Poll of Polls Hungary: Tisza leads Fidesz in the aggregated polling average",
            ),
        ),
    ),
    Task(
        task_id="manifold-italy-2026-world-cup",
        as_of=date(2026, 3, 1),
        resolution_date=date(2026, 4, 11),
        question=BinaryQuestion(
            text=(
                "Will Italy qualify for the 2026 FIFA World Cup? Italy finished second in UEFA qualifying Group I "
                "behind Norway and enters the March 2026 playoffs: a semifinal at home against Northern Ireland "
                "on 2026-03-26 and, with a win, an away final on 2026-03-31 against the winner of Wales vs Bosnia "
                "and Herzegovina. Resolves YES only if Italy secures a place in the World Cup finals tournament."
            )
        ),
        outcome=BinaryOutcome(value=False),
        outcome_source="Manifold market mIQ8YdCJlAcMz9fFmIUX, resolved NO 2026-04-11; Manifold API, fetched 2026-06-10.",
        evidence=(
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20251118003225/https://www.foxsports.com/stories/soccer/"
                    "norway-qualifies-2026-world-cup-sends-italy-dreaded-playoff"
                ),
                date=date(2025, 11, 18),
                title="Fox Sports: Norway thrash Italy 4-1 in Milan, qualify directly and send Italy to the playoffs",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20251120162000/https://www.cbc.ca/sports/soccer/"
                    "world-cup-fifa-qualifying-draw-italy-9.6985976"
                ),
                date=date(2025, 11, 20),
                title="CBC: Italy must beat Northern Ireland and then Wales or Bosnia to reach the World Cup",
            ),
            EvidenceItem(
                url=(
                    "https://web.archive.org/web/20251122013402/https://www.espn.com/soccer/story/_/id/47034867/"
                    "world-cup-playoff-draw-italy-get-northern-ireland-wales-bosnia"
                ),
                date=date(2025, 11, 22),
                title="ESPN: playoff draw hands Italy a semifinal against Northern Ireland, final away to Wales/Bosnia path",
            ),
        ),
    ),
)

# Reference baseline, NOT shown to contestants: the market's own probability at
# the task's as_of — `probAfter` of the last non-redemption bet at/before as_of
# 00:00 UTC, reconstructed from `/v0/bets` (rounded to 4 decimals). The
# reconstruction rule is verified in loom/plans/market_harvest.md.
MARKET_PROB_AT_AS_OF: dict[str, float] = {
    "manifold-bitcoin-100k-2024": 0.1811,
    "manifold-biden-pardons-hunter": 0.1202,
    "manifold-trump-wins-2024-election": 0.4947,
    "manifold-gabbard-confirmed-dni": 0.7176,
    "manifold-us-strikes-iran-2025": 0.2953,
    "manifold-gpt5-before-aug-2025": 0.4759,
    "manifold-ai-imo-gold-2025": 0.5030,
    "manifold-us-govt-shutdown-2025": 0.6012,
    "manifold-orban-stays-pm-2026": 0.4200,
    "manifold-italy-2026-world-cup": 0.6300,
}
