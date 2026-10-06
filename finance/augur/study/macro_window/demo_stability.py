"""Demo: print the century-vs-1955 comparison over several scoring periods, and which differences keep their sign.

Reports sensitivity, not a winner: whether "a winner" is even a well-posed idea for this comparison
is what the output shows. Fetches from the public upstreams, so it is run by hand
(`bb run //finance/augur/study/macro_window:demo_stability_bin`) and wired into no test target.
"""

import asyncio

from finance.augur.study.macro_window.holdout import long_record_state_path
from finance.augur.study.macro_window.stability import describe, single_window_stability


async def async_main() -> None:
    print(describe(single_window_stability(await long_record_state_path())))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
