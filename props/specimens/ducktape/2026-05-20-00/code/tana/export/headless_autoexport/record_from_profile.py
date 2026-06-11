import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE = Path("~/.config/tana/Default").expanduser()


async def main():
    async with async_playwright() as p:
        #        ctx = await p.chromium.launch_persistent_context(
        #            PROFILE,
        #            channel="chrome",
        #            headless=False      # headful while iterating
        #        )
        ctx = await p.chromium.launch_persistent_context(
            PROFILE,
            channel="chrome",
            headless=False,  # test headful first
            args=[
                "--password-store=gnome",  # same backend as Electron
                "--disable-features=UsePasswordManagerService",  # keep prompts silent
            ],
        )
        # https://app.tana.inc
        page = ctx.pages[0] or await ctx.new_page()
        await page.pause()  # opens code-gen/inspector UI


asyncio.run(main())
