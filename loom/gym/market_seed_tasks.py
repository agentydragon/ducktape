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

The data lives as `MarketSeedRecord` rows minted into gym tasks — the shape
that mirror-harvested markets (see `loom/plans/manifold_mirror.md`) will join,
and the seed of the mirror's market-id roster. Evidence items carry the
original page URL (what prompts show, and what contestants fetch themselves
once the wayback proxy of `loom/plans/wayback_proxy.md` lands — content is
never pre-downloaded) plus the pinned Wayback capture proving the page existed
by its date.

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

from dataclasses import dataclass
from datetime import date

from loom.gym.task import WAYBACK_PREFIX, BinaryOutcome, BinaryQuestion, EvidenceItem, Task

# The single API sweep all ten records came from, cited in outcome_source.
_API_FETCH_DATE = date(2026, 6, 10)


def _capture(url: str, timestamp: str, title: str) -> EvidenceItem:
    """EvidenceItem from an original URL and its Wayback capture timestamp (YYYYMMDDhhmmss)."""
    return EvidenceItem(
        url=url,
        archived_url=f"{WAYBACK_PREFIX}{timestamp}/{url}",
        date=date(int(timestamp[:4]), int(timestamp[4:6]), int(timestamp[6:8])),
        title=title,
    )


@dataclass(frozen=True)
class MarketSeedRecord:
    """One curated market in mintable form; `market_id` is the Manifold contract id."""

    market_id: str
    task_id: str
    as_of: date
    resolution_date: date
    resolved_yes: bool
    # The market's own probability at as_of — `probAfter` of the last
    # non-redemption bet at/before as_of 00:00 UTC, reconstructed from
    # `/v0/bets` (rule verified in loom/plans/market_harvest.md). Reference
    # baseline for scoring; never shown to contestants.
    prob_at_as_of: float
    question: str
    evidence: tuple[EvidenceItem, ...]


MARKET_SEED_RECORDS: tuple[MarketSeedRecord, ...] = (
    MarketSeedRecord(
        market_id="hKB9iD8knG4R2RuSyyCu",
        task_id="manifold-bitcoin-100k-2024",
        as_of=date(2024, 9, 1),
        resolution_date=date(2024, 12, 5),
        resolved_yes=True,
        prob_at_as_of=0.1811,
        question=(
            "Will the price of Bitcoin (BTC/USD) reach $100,000 at any point in 2024? Resolves YES if Bitcoin "
            "trades at or above $100,000 on major exchanges before the end of 2024. At the information cutoff "
            "Bitcoin trades near $59,000, below its March 2024 all-time high of roughly $73,800."
        ),
        evidence=(
            _capture(
                "https://www.coindesk.com/price/bitcoin",
                "20240831180317",
                "CoinDesk live Bitcoin price page: BTC trading near $59,000 at the end of August 2024",
            ),
            _capture(
                "https://www.coingecko.com/en/coins/bitcoin",
                "20240831040646",
                "CoinGecko Bitcoin tracker: price around $59,000, well below the March 2024 all-time high",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="9ln3uPEbleLkjwQ5HR4b",
        task_id="manifold-biden-pardons-hunter",
        as_of=date(2024, 10, 1),
        resolution_date=date(2024, 12, 2),
        resolved_yes=True,
        prob_at_as_of=0.1202,
        question=(
            "Will US President Joe Biden pardon his son Hunter Biden, or commute a sentence of his, before "
            "2025-01-21 (the end of Biden's term)? Any presidential pardon or commutation of sentence for "
            "Hunter Biden resolves YES. Hunter Biden was convicted on federal gun charges on 2024-06-11 and "
            "pleaded guilty to federal tax charges on 2024-09-05, with sentencing scheduled for December 2024; "
            "the White House has repeatedly said the President will not pardon him."
        ),
        evidence=(
            _capture(
                "https://www.politico.com/news/2024/06/13/president-says-he-wont-pardon-hunter-biden-00163281",
                "20240613211230",
                "Politico: Biden says he will not pardon his son Hunter after the gun-trial conviction",
            ),
            _capture(
                "https://www.politico.com/news/2024/09/05/white-house-biden-wont-pardon-son-00177551",
                "20240906225231",
                "Politico: White House reiterates Biden won't pardon Hunter after the tax-case guilty plea",
            ),
            _capture(
                "https://www.nbcnews.com/politics/joe-biden/hunter-biden-intends-plead-guilty-federal-tax-charges-rcna169621",
                "20240910213625",
                "NBC News: Hunter Biden enters guilty plea in federal tax case, avoiding a trial",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="d1t4k3nz3t",
        task_id="manifold-trump-wins-2024-election",
        as_of=date(2024, 10, 15),
        resolution_date=date(2024, 11, 6),
        resolved_yes=True,
        prob_at_as_of=0.4947,
        question=(
            "Will Donald Trump win the 2024 United States presidential election, scheduled for 2024-11-05? "
            "Resolves YES if Trump — or, were he replaced as nominee, any Republican Party candidate — wins "
            "the presidency, based on the Associated Press and Fox News decision-desk calls; resolves NO if "
            "the Democratic candidate wins."
        ),
        evidence=(
            _capture(
                "https://www.nytimes.com/interactive/2024/us/elections/polls-president.html",
                "20241002001349",
                "New York Times polling averages: Harris holds a narrow national lead, battlegrounds tight",
            ),
            _capture(
                "https://projects.fivethirtyeight.com/2024-election-forecast/",
                "20241013041159",
                "FiveThirtyEight 2024 election forecast: race rated close to a coin flip in mid-October",
            ),
            _capture(
                "https://www.realclearpolling.com/elections/president/2024/battleground-states",
                "20241013190523",
                "RealClearPolling battleground-state averages: all seven key swing states within ~2 points",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="02n9uqqCSl",
        task_id="manifold-gabbard-confirmed-dni",
        as_of=date(2025, 1, 15),
        resolution_date=date(2025, 2, 12),
        resolved_yes=True,
        prob_at_as_of=0.7176,
        question=(
            "Will the United States Senate confirm Tulsi Gabbard as Director of National Intelligence? "
            "President-elect Trump announced her nomination on 2024-11-13; confirmation requires a Senate "
            "majority vote. Resolves YES if the Senate confirms her for the role, NO if her nomination is "
            "withdrawn, rejected, or otherwise fails."
        ),
        evidence=(
            _capture(
                "https://www.washingtonpost.com/national-security/2024/11/13/tulsi-gabbard-trump-director-national-intelligence/",
                "20241114002441",
                "Washington Post: Trump picks Tulsi Gabbard as director of national intelligence",
            ),
            _capture(
                "https://www.npr.org/2024/11/13/nx-s1-5189603/trump-tulsi-gabbard-director-of-national-intelligence",
                "20241116001020",
                "NPR: Gabbard named for DNI despite no intelligence-community experience; critics appalled",
            ),
            _capture(
                "https://www.nbcnews.com/politics/donald-trump/trump-names-tulsi-gabbard-director-national-intelligence-rcna180035",
                "20250115225946",
                "NBC News profile of the Gabbard nomination (still pending Senate consideration mid-January)",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="2SA2SUCPI2",
        task_id="manifold-us-strikes-iran-2025",
        as_of=date(2025, 4, 20),
        resolution_date=date(2025, 6, 22),
        resolved_yes=True,
        prob_at_as_of=0.2953,
        question=(
            "Will the United States carry out a bombing or missile strike on Iranian territory during 2025, "
            "with the US taking credit for the action? Any US-acknowledged strike that sets off bombs in Iran "
            "resolves YES; strikes carried out by Israel alone, even with US-manufactured weapons, do not "
            "count. At the information cutoff the US and Iran are holding indirect nuclear talks (first round "
            "2025-04-12 in Oman), after President Trump threatened bombing if no deal is reached."
        ),
        evidence=(
            _capture(
                "https://www.axios.com/2025/03/30/trump-iran-nuclear-deal-bombing",
                "20250401004029",
                "Axios: Trump threatens Iran with bombing 'the likes of which they have never seen' absent a deal",
            ),
            _capture(
                "https://www.washingtonpost.com/world/2025/04/12/us-iran-nuclear-talks-oman-witkoff/",
                "20250413215442",
                "Washington Post: US and Iran begin nuclear talks in Oman (Witkoff and Araghchi)",
            ),
            _capture(
                "https://abcnews.go.com/International/irans-delegation-arrives-oman-indirect-nuclear-talks-us/story?id=120740838",
                "20250415165513",
                "ABC News: Iranian delegation holds indirect talks with the US in Oman; both sides call them constructive",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="LZlomZnzAVwVvaPmWUy5",
        task_id="manifold-gpt5-before-aug-2025",
        as_of=date(2025, 5, 1),
        resolution_date=date(2025, 8, 1),
        resolved_yes=False,
        prob_at_as_of=0.4759,
        question=(
            "Will OpenAI release a model named GPT-5 before 2025-08-01? Resolves YES only if a model called "
            "'GPT-5' is released to the public by that date; differently-named releases (GPT-4.5, o3, o4-mini) "
            "do not count. At the information cutoff OpenAI has said (2025-04-04) that GPT-5 is delayed by 'a "
            "few months' while o3 and o4-mini ship first."
        ),
        evidence=(
            _capture(
                "https://techcrunch.com/2025/02/12/openai-cancels-its-o3-ai-model-in-favor-of-a-unified-next-gen-release/",
                "20250213203540",
                "TechCrunch: Altman roadmap folds o3 into a unified GPT-5 release due in months, not weeks",
            ),
            _capture(
                "https://techcrunch.com/2025/04/04/openai-says-itll-release-o3-after-all-delays-gpt-5/",
                "20250410092545",
                "TechCrunch: OpenAI will release o3 and o4-mini after all and delays GPT-5 by 'a few months'",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="tu2ouer9zq",
        task_id="manifold-ai-imo-gold-2025",
        as_of=date(2025, 6, 1),
        resolution_date=date(2025, 7, 21),
        resolved_yes=True,
        prob_at_as_of=0.5030,
        question=(
            "Will an AI system achieve a gold-medal score on the 2025 International Mathematical Olympiad "
            "(IMO) problem set, under the same time limits as human contestants (4.5 hours per 3-problem "
            "session), with the result reported by reliable publications within one month after the contest "
            "(held 2025-07-10 to 2025-07-20)? Input and output may be informal natural language or formal "
            "(e.g. Lean). For reference, in 2024 Google DeepMind's AlphaProof/AlphaGeometry 2 scored one "
            "point short of gold, taking days of computation on some problems."
        ),
        evidence=(
            _capture(
                "https://deepmind.google/discover/blog/ai-solves-imo-problems-at-silver-medal-level/",
                "20240726204127",
                "Google DeepMind: AlphaProof and AlphaGeometry 2 solve IMO 2024 problems at silver-medal level",
            ),
            _capture(
                "https://matharena.ai/",
                "20250504175955",
                "MathArena leaderboard: frontier LLMs score far below medal level on recent olympiad problem sets",
            ),
            _capture(
                "https://arxiv.org/abs/2503.21934",
                "20250523115749",
                "arXiv 'Proof or Bluff?': LLMs score under 5% on full-proof grading of the 2025 USA Math Olympiad",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="kt6f2t09kv",
        task_id="manifold-us-govt-shutdown-2025",
        as_of=date(2025, 9, 21),
        resolution_date=date(2025, 10, 1),
        resolved_yes=True,
        prob_at_as_of=0.6012,
        question=(
            "Will a US federal government shutdown that involves furloughing federal workers begin between "
            "2025-01-01 and 2025-12-31? A funding lapse so brief that no workers are furloughed does not "
            "count, nor would a shutdown that began in 2024. At the information cutoff federal appropriations "
            "expire at the end of 2025-09-30; the House has passed a seven-week stopgap bill, which the "
            "Senate has rejected amid a dispute over health-care subsidies."
        ),
        evidence=(
            _capture(
                "https://www.washingtonpost.com/business/2025/09/02/government-funding-deadline-congress/",
                "20250903175159",
                "Washington Post: government shutdown looms as Congress returns with funding due September 30",
            ),
            _capture(
                "https://www.npr.org/2025/09/16/nx-s1-5543189/house-republican-stopgap-shutdown",
                "20250919164738",
                "NPR: House Republicans release a seven-week stopgap as Democrats warn of a potential shutdown",
            ),
            _capture(
                "https://www.npr.org/2025/09/19/nx-s1-5545929/house-stopgap-funding-bill-government-shutdown",
                "20250920135314",
                "NPR: House approves the stopgap 217-212 but the Senate blocks it over health-care subsidies",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="ZJ9LlySKX4dldqcpdQ4G",
        task_id="manifold-orban-stays-pm-2026",
        as_of=date(2026, 2, 15),
        resolution_date=date(2026, 4, 12),
        resolved_yes=False,
        prob_at_as_of=0.4200,
        question=(
            "Will Viktor Orbán remain Hungary's prime minister following the Hungarian parliamentary election "
            "scheduled for 2026-04-12 — that is, will the newly elected National Assembly choose Orbán as "
            "prime minister? At the information cutoff, opposition leader Péter Magyar's Tisza party leads "
            "most independent polls, while pro-government pollsters show Orbán's Fidesz ahead."
        ),
        evidence=(
            _capture(
                "https://politpro.eu/en/hungary",
                "20260130004147",
                "PolitPro Hungary poll tracker: Tisza polling ahead of Fidesz in late January 2026",
            ),
            _capture(
                "https://www.politico.eu/europe-poll-of-polls/hungary/",
                "20260206174647",
                "POLITICO Poll of Polls Hungary: Tisza leads Fidesz in the aggregated polling average",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="mIQ8YdCJlAcMz9fFmIUX",
        task_id="manifold-italy-2026-world-cup",
        as_of=date(2026, 3, 1),
        resolution_date=date(2026, 4, 11),
        resolved_yes=False,
        prob_at_as_of=0.6300,
        question=(
            "Will Italy qualify for the 2026 FIFA World Cup? Italy finished second in UEFA qualifying Group I "
            "behind Norway and enters the March 2026 playoffs: a semifinal at home against Northern Ireland "
            "on 2026-03-26 and, with a win, an away final on 2026-03-31 against the winner of Wales vs Bosnia "
            "and Herzegovina. Resolves YES only if Italy secures a place in the World Cup finals tournament."
        ),
        evidence=(
            _capture(
                "https://www.foxsports.com/stories/soccer/norway-qualifies-2026-world-cup-sends-italy-dreaded-playoff",
                "20251118003225",
                "Fox Sports: Norway thrash Italy 4-1 in Milan, qualify directly and send Italy to the playoffs",
            ),
            _capture(
                "https://www.cbc.ca/sports/soccer/world-cup-fifa-qualifying-draw-italy-9.6985976",
                "20251120162000",
                "CBC: Italy must beat Northern Ireland and then Wales or Bosnia to reach the World Cup",
            ),
            _capture(
                "https://www.espn.com/soccer/story/_/id/47034867/world-cup-playoff-draw-italy-get-northern-ireland-wales-bosnia",
                "20251122013402",
                "ESPN: playoff draw hands Italy a semifinal against Northern Ireland, final away to Wales/Bosnia path",
            ),
        ),
    ),
)


def _task(record: MarketSeedRecord) -> Task:
    return Task(
        task_id=record.task_id,
        as_of=record.as_of,
        resolution_date=record.resolution_date,
        question=BinaryQuestion(text=record.question),
        outcome=BinaryOutcome(value=record.resolved_yes),
        outcome_source=(
            f"Manifold market {record.market_id}, resolved {'YES' if record.resolved_yes else 'NO'} "
            f"{record.resolution_date}; Manifold API, fetched {_API_FETCH_DATE}."
        ),
        evidence=record.evidence,
    )


MARKET_SEED_TASKS: tuple[Task, ...] = tuple(_task(record) for record in MARKET_SEED_RECORDS)

# Reference baseline, NOT shown to contestants (see MarketSeedRecord.prob_at_as_of).
MARKET_PROB_AT_AS_OF: dict[str, float] = {record.task_id: record.prob_at_as_of for record in MARKET_SEED_RECORDS}
