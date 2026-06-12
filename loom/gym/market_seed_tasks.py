"""Hand-curated tasks minted from top resolved binary markets on the live Manifold API.

Thirty-three live YES/NO binary markets (each with ≥ 20 unique bettors)
chosen from topical sweeps of the Manifold `search-markets` endpoint over
markets resolved between 2024-08 and 2026-05 — recent enough that every model
in `loom.gym.model_cutoffs` (including glm-4.5, knowledge cutoff 2024-06-30)
is admissible at every task's `as_of`. The curated set was 17 YES / 17 NO; the
China/Taiwan invasion market (a NO) is commented out below because CN-hosted
models refuse it at the API (moderation error 1301), leaving 17 YES / 16 NO
active. The panel spans US and non-US
politics, geopolitics, economic data, courts, space, public health, sports,
tech business, and entertainment. Probabilities at `as_of` mix mid-range
questions with calibration tails, including several markets that were
confidently wrong at `as_of` (Assad surviving 2024, a Canadian Conservative
majority, a 2025 US recession, 100+ US H5N1 cases) — the sharpest
discrimination tests. Question texts state only the proposition and its
resolution criterion (what resolves YES/NO, by when, judged how) — the
`as_of` world state (prices, poll standings, who is ahead) is deliberately
left out, since it is retrievable data: the contestant gets it from the
dossier, the source URLs, and the dated web proxy rather than being
hand-fed (and editorialized) in the prompt. Each `as_of` falls strictly
between market creation and resolution, at a point where the question was
genuinely open; `resolution_date` is the market's actual resolution time
(UTC date part).

The data lives as `MarketSeedRecord` rows minted into gym tasks — the shape
that mirror-harvested markets join, and the seed of the mirror's market-id
roster. Evidence is optional per task,
attached only where a cheap contemporaneous capture existed. Contestants see
the original page URLs as a titleless list (a `/data/sources.txt` file in the
container harness, a URL list in the bare-LLM prompt) — possible research
starting points they fetch through the proxy themselves; the title and the
pinned Wayback capture stay in the record (human note + "existed by then"
proof), never shown to the contestant.

Data source: the public Manifold Markets API (https://api.manifold.markets/v0,
fetched 2026-06-10). License: personal/academic/non-commercial use only;
commercial use (including AI training for commercial purposes) requires a
license from data@manifold.markets — see
s3://loom-gym/harvest/raw/manifold-20240706/README.md; the same terms apply to
API data. Caveat: the API serves the *current* question text and description,
which the creator may have edited after `as_of` (no edit history is exposed),
so curation prefers markets with stable, unambiguous titles; question texts
state the criterion the market actually resolved by.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from loom.gym.task import WAYBACK_PREFIX, BinaryOutcome, BinaryQuestion, EvidenceItem, Task

# The single API sweep all records came from, cited in outcome_source.
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
            "trades at or above $100,000 on major exchanges before the end of 2024."
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
    # Disabled: Zhipu/GLM (and likely other CN-hosted models) reject this prompt at the
    # API with content-moderation error 1301 ("potentially unsafe or sensitive content"),
    # so the China/Taiwan invasion market is not runnable across the model panel. Kept here
    # commented (not deleted) as a record of the curated set; re-enable if the panel drops
    # CN-hosted models or they stop refusing it.
    #   MarketSeedRecord(
    #       market_id="k4XTBQLFuDvBljexbalv",
    #       task_id="manifold-china-invades-taiwan-2024",
    #       as_of=date(2024, 9, 15),
    #       resolution_date=date(2025, 1, 1),
    #       resolved_yes=False,
    #       prob_at_as_of=0.0634,
    #       question=(... China invade mainland Taiwan by end of 2024 ...),
    #       evidence=(),
    #   ),
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
            "Hunter Biden resolves YES."
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
        market_id="M5HyCkqDZ3CdIRLa7fKp",
        task_id="manifold-verstappen-2024-f1-title",
        as_of=date(2024, 10, 1),
        resolution_date=date(2024, 11, 24),
        resolved_yes=True,
        prob_at_as_of=0.6646,
        question=("Will Max Verstappen win the 2024 Formula 1 World Drivers' Championship?"),
        evidence=(),
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
        market_id="ha8siwsis8",
        task_id="manifold-barnier-pm-on-2025-01-01",
        as_of=date(2024, 11, 10),
        resolution_date=date(2025, 1, 2),
        resolved_yes=False,
        prob_at_as_of=0.7250,
        question=(
            "Will Michel Barnier be the Prime Minister of France on 2025-01-01? Resolves NO if by that date "
            "he has resigned or been ousted — including if he stays on only as a caretaker ('démissionnaire') "
            "pending a successor."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="W6sxwGRpBmQOiooR560w",
        task_id="manifold-assad-in-power-end-2024",
        as_of=date(2024, 11, 20),
        resolution_date=date(2024, 12, 11),
        resolved_yes=False,
        prob_at_as_of=0.9239,
        question=(
            "Will Bashar al-Assad remain in power as President of Syria through 2024-12-31 (11:59 PM ET)? "
            "Resolves NO immediately if he ceases to hold the office of President for any reason before "
            "then, as confirmed by reliable news outlets."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="tSmI97iXzTCHLh61JPTs",
        task_id="manifold-ulbricht-clemency-before-march-2025",
        as_of=date(2024, 12, 15),
        resolution_date=date(2025, 1, 22),
        resolved_yes=True,
        prob_at_as_of=0.7031,
        question=(
            "Will Ross Ulbricht — founder of the Silk Road darknet marketplace, serving a double life "
            "sentence plus 40 years since 2015 — be pardoned or have his sentence commuted to time served "
            "before 2025-03-01? Any full pardon or commutation to time served, by whichever president, "
            "resolves YES."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="aRIE73wma9FTmrEL1tEs",
        task_id="manifold-canada-conservative-majority",
        as_of=date(2024, 12, 15),
        resolution_date=date(2025, 4, 29),
        resolved_yes=False,
        prob_at_as_of=0.8964,
        question=(
            "Will the Conservative Party of Canada win a majority government — more than half the seats in "
            "the House of Commons — at the 45th Canadian federal election, due on or before 2025-10-20? "
            "Winning the most seats but falling short of a majority resolves NO."
        ),
        evidence=(
            _capture(
                "https://338canada.com/",
                "20241213025406",
                "338Canada federal projection: Conservatives projected to win a large majority of seats",
            ),
            _capture(
                "https://newsinteractives.cbc.ca/elections/poll-tracker/canada/",
                "20241212195742",
                "CBC Poll Tracker: Conservatives lead Liberals by roughly 20 points in the federal polling average",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="O8HQvFthF03qYOSaUo9R",
        task_id="manifold-new-glenn-orbit-first-launch",
        as_of=date(2025, 1, 5),
        resolution_date=date(2025, 1, 25),
        resolved_yes=True,
        prob_at_as_of=0.7386,
        question=(
            "Will Blue Origin's New Glenn rocket achieve orbit on its first launch? An attempt counts as a "
            "launch only if the countdown completes and the hold-down clamps release (a scrub does not "
            "count); achieving orbit means accelerating the payload to orbital velocity."
        ),
        evidence=(),
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
            "Confirmation requires a Senate majority vote. Resolves YES if the Senate confirms her for the "
            "role, NO if her nomination is withdrawn, rejected, or otherwise fails."
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
        market_id="ti5toE64slB9MgfzSDBR",
        task_id="manifold-afd-beats-spd-2025",
        as_of=date(2025, 1, 15),
        resolution_date=date(2025, 2, 24),
        resolved_yes=True,
        prob_at_as_of=0.8432,
        question=(
            "Will the far-right Alternative für Deutschland (AfD) receive more votes than the Social "
            "Democrats (SPD) at Germany's next federal election, held early on 2025-02-23 after the "
            "governing coalition collapsed in November 2024? Resolves by the official second-vote "
            "(Zweitstimme) totals."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="CkcepPGTqdaOmeYWcWgj",
        task_id="manifold-h5n1-100-us-cases-by-2026",
        as_of=date(2025, 1, 15),
        resolution_date=date(2026, 1, 1),
        resolved_yes=False,
        prob_at_as_of=0.9084,
        question=(
            "Will there be 100 or more cumulative confirmed human cases of H5N1 avian influenza in the "
            "United States by the end of 2025, as reported on the CDC's bird-flu situation summary page?"
        ),
        evidence=(
            _capture(
                "https://www.cdc.gov/bird-flu/situation-summary/index.html",
                "20250108230311",
                "CDC H5N1 situation summary: 66 confirmed human cases in the US since April 2024; first US death",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="r7CIGIOnWlD8OTe0XPHZ",
        task_id="manifold-capital-one-discover-merger",
        as_of=date(2025, 2, 15),
        resolution_date=date(2025, 5, 25),
        resolved_yes=True,
        prob_at_as_of=0.7855,
        question=(
            "Will Capital One's announced $35 billion acquisition of Discover Financial Services be "
            "completed? Resolves YES if the merger closes; resolves NO if the deal is blocked, abandoned, "
            "or still unconsummated when the market closes in mid-May 2025."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="glI2NUEE2A",
        task_id="manifold-labor-wins-2025-australian-election",
        as_of=date(2025, 3, 1),
        resolution_date=date(2025, 5, 3),
        resolved_yes=True,
        prob_at_as_of=0.3900,
        question=(
            "Will the Australian Labor Party win the 2025 Australian federal election — that is, will Labor "
            "supply the Prime Minister of the 48th Parliament of Australia?"
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="gk05ncy0lw",
        task_id="manifold-minecraft-outgrosses-sonic-3",
        as_of=date(2025, 3, 15),
        resolution_date=date(2025, 5, 10),
        resolved_yes=True,
        prob_at_as_of=0.6497,
        question=(
            "Will 'A Minecraft Movie' (Warner Bros., releasing 2025-04-04) earn more at the worldwide box "
            "office than 'Sonic the Hedgehog 3' (Paramount, released 2024-12-20), comparing lifetime "
            "worldwide grosses per Box Office Mojo?"
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="4gdkzbfvtt",
        task_id="manifold-russia-ukraine-ceasefire-aug-2025",
        as_of=date(2025, 3, 15),
        resolution_date=date(2025, 8, 4),
        resolved_yes=False,
        prob_at_as_of=0.6634,
        question=(
            "Will Ukraine and Russia reach a ceasefire — in agreement or in practice — by 2025-08-01? "
            "Resolves YES if either (a) Ukraine, Russia, and the United States all announce, at roughly the "
            "same time, a ceasefire, a winding-down of military activity, or a desire to negotiate peace, "
            "or (b) there is a roughly 30-day period with no change of territorial control and very few "
            "casualties."
        ),
        evidence=(
            _capture(
                "https://www.state.gov/joint-statement-on-the-united-states-ukraine-meeting-in-jeddah/",
                "20250313140136",
                "US-Ukraine joint statement (Jeddah): Ukraine ready to accept an immediate interim 30-day ceasefire",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="yZpALnnSSy",
        task_id="manifold-celtics-win-2025-nba-finals",
        as_of=date(2025, 4, 15),
        resolution_date=date(2025, 5, 17),
        resolved_yes=False,
        prob_at_as_of=0.3100,
        question=(
            "Will the Boston Celtics win the 2025 NBA Championship? Resolves YES only if the Celtics win "
            "the 2025 NBA Finals, per official NBA results."
        ),
        evidence=(),
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
            "count."
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
            "do not count."
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
        market_id="D5o5fIGpQnjANdl2DxdU",
        task_id="manifold-us-recession-in-2025",
        as_of=date(2025, 5, 1),
        resolution_date=date(2025, 12, 26),
        resolved_yes=False,
        prob_at_as_of=0.6000,
        question=(
            "Will the US economy enter a recession in 2025, defined as two consecutive quarters of negative "
            "real GDP growth with both quarters falling within calendar 2025, judged by the BEA's initial "
            "(advance) estimate for each quarter?"
        ),
        evidence=(
            _capture(
                "https://www.bea.gov/news/2025/gross-domestic-product-1st-quarter-2025-advance-estimate",
                "20250430221201",
                "BEA advance estimate: US real GDP decreased at a 0.3% annual rate in Q1 2025",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="3ns0hmvp6i",
        task_id="manifold-sweden-wins-eurovision-2025",
        as_of=date(2025, 5, 10),
        resolution_date=date(2025, 5, 19),
        resolved_yes=False,
        prob_at_as_of=0.4400,
        question=(
            "Will Sweden win the 2025 Eurovision Song Contest, whose grand final takes place in Basel on 2025-05-17?"
        ),
        evidence=(
            _capture(
                "https://eurovisionworld.com/odds/eurovision",
                "20250508184432",
                "Eurovisionworld bookmaker aggregate: Sweden's KAJ the clear favorite to win Eurovision 2025",
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
            "(e.g. Lean)."
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
        market_id="q59su0Cs5l",
        task_id="manifold-mangione-murder-conviction-2025",
        as_of=date(2025, 6, 1),
        resolution_date=date(2026, 1, 1),
        resolved_yes=False,
        prob_at_as_of=0.1829,
        question=(
            "Will Luigi Mangione be convicted of murder (any degree, whether by verdict or guilty plea) in "
            "connection with the December 2024 killing of UnitedHealthcare CEO Brian Thompson before "
            "2026-01-01? A murder conviction in any court — New York state or federal — counts."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="s5sgqZ9qQ5",
        task_id="manifold-powell-out-before-2026",
        as_of=date(2025, 7, 15),
        resolution_date=date(2026, 1, 6),
        resolved_yes=False,
        prob_at_as_of=0.1753,
        question=(
            "Will Jerome Powell cease to hold the office of Chair of the US Federal Reserve before "
            "2026-01-01 (Eastern Time)? Resolves YES if he is no longer Fed Chair for any reason — removal, "
            "resignation, or death — before that moment."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="2z2Q9zdN60",
        task_id="manifold-fed-cuts-september-2025",
        as_of=date(2025, 8, 1),
        resolution_date=date(2025, 9, 17),
        resolved_yes=True,
        prob_at_as_of=0.4010,
        question=(
            "Will the Federal Reserve cut interest rates at its September 2025 FOMC meeting (scheduled for "
            "2025-09-16/17)? Any reduction of the federal funds target range announced at that meeting "
            "resolves YES."
        ),
        evidence=(
            _capture(
                "https://www.federalreserve.gov/newsevents/pressreleases/monetary20250730a.htm",
                "20250731105528",
                "FOMC statement: Fed holds the federal funds target range at 4.25-4.50% at the July 2025 meeting",
            ),
        ),
    ),
    MarketSeedRecord(
        market_id="sPqzu6CUPZ",
        task_id="manifold-trump-2025-nobel-peace-prize",
        as_of=date(2025, 9, 15),
        resolution_date=date(2025, 10, 10),
        resolved_yes=False,
        prob_at_as_of=0.0400,
        question=(
            "Will Donald Trump win the 2025 Nobel Peace Prize, to be announced by the Norwegian Nobel "
            "Committee on 2025-10-10?"
        ),
        evidence=(),
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
            "count, nor would a shutdown that began in 2024."
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
        market_id="n6llpZNcUU",
        task_id="manifold-uk-wealth-tax-2025",
        as_of=date(2025, 10, 15),
        resolution_date=date(2025, 12, 19),
        resolved_yes=False,
        prob_at_as_of=0.0610,
        question=(
            "Will the United Kingdom introduce a wealth tax by the end of 2025 — a new tax on holdings of "
            "wealth, such as an annual net-wealth tax, an unrealized-capital-gains tax, or a recurring "
            "wealth tax limited to real estate — passed by Parliament by 2025-12-18, when Parliament rises? "
            "Changes to existing property taxes (e.g. council tax), a land-value tax, or higher rates on "
            "realized income or gains do not count."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="NscOsEu2qs",
        task_id="manifold-yoon-insurrection-conviction",
        as_of=date(2025, 11, 1),
        resolution_date=date(2026, 2, 19),
        resolved_yes=True,
        prob_at_as_of=0.5006,
        question=(
            "Will former South Korean president Yoon Suk Yeol be convicted of insurrection over his "
            "2024-12-03 martial-law declaration? Resolves YES on a guilty verdict on the insurrection "
            "charge at his criminal trial."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="lqsLQ9NPnC",
        task_id="manifold-scotus-upholds-trump-tariffs",
        as_of=date(2026, 1, 10),
        resolution_date=date(2026, 2, 20),
        resolved_yes=False,
        prob_at_as_of=0.2445,
        question=(
            "Will the US Supreme Court rule that the President had statutory authority under the "
            "International Emergency Economic Powers Act (IEEPA) to impose the 2025 'reciprocal' and "
            "'trafficking' tariffs challenged in Learning Resources v. Trump / V.O.S. Selections v. Trump? "
            "Resolves YES if by 2026-06-30 a SCOTUS merits decision (5+ justices) holds the challenged "
            "tariffs were lawful; a decision striking them down, or no decision by then, resolves NO."
        ),
        evidence=(),
    ),
    MarketSeedRecord(
        market_id="LfRsm10GqnSzXQVeAon3",
        task_id="manifold-artemis-2-crew-returns-alive",
        as_of=date(2026, 1, 15),
        resolution_date=date(2026, 4, 11),
        resolved_yes=True,
        prob_at_as_of=0.9159,
        question=(
            "Will NASA's Artemis II mission — the first crewed flight of the SLS rocket and Orion capsule, "
            "carrying four astronauts around the Moon and back — return to Earth with all of its crew "
            "alive? Resolves YES when the crew returns alive, NO if a crew member dies during the mission; "
            "the question is void if the mission is scrapped before launch."
        ),
        evidence=(
            _capture(
                "https://www.nasa.gov/mission/artemis-ii/",
                "20251220055958",
                "NASA Artemis II mission page: four astronauts to fly around the Moon on the first crewed Artemis flight",
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
            "prime minister?"
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
            "Will Italy qualify for the 2026 FIFA World Cup? Resolves YES only if Italy secures a place in "
            "the World Cup finals tournament."
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
