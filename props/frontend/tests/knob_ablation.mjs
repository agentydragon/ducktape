/**
 * Knob ablation test for visual regression rendering.
 *
 * Tests each hermetic rendering knob by toggling it off individually and
 * comparing the resulting screenshot against a baseline (all knobs on).
 * Produces a summary table showing which knobs actually affect rendering.
 *
 * Usage: node knob-ablation.mjs
 *
 * Environment variables (same as visual-regression.spec.js):
 *   HARNESS_PATH - path to harness JS bundle
 *   PUPPETEER_EXECUTABLE_PATH - path to Chromium binary
 *   FONTCONFIG_FILE - path to hermetic fonts.conf
 *   FREETYPE_PROPERTIES - FreeType property overrides
 */

import puppeteer from "puppeteer";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";
import { fileURLToPath } from "url";
import { dirname, join, extname } from "path";
import { createServer } from "http";
import { readFile } from "fs/promises";
import { execSync } from "child_process";

// Per-knob timeout (ms). Some flag combos hang Chrome indefinitely.
const KNOB_TIMEOUT_MS = 30_000;

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const OUTPUT_DIR =
  process.env.TEST_UNDECLARED_OUTPUTS_DIR ||
  join(__dirname, "knob_ablation_results");

// Test scenario - use DefinitionDetail as representative (text-heavy, tables).
// Also test DistributionChartRecall (chart rendering, worst observed diff).
const SCENARIOS = ["DefinitionDetail", "DistributionChartRecall"];

const CONTENT_TYPES = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

// ── Knob definitions ──────────────────────────────────────────────────
//
// Each knob has:
//   id: short identifier (used in filenames)
//   description: what it does
//   category: chrome_flag | css | env | viewport | media
//   apply: how to disable it (remove a flag, inject CSS override, etc.)

// Chrome flags that form the baseline
const ALL_CHROME_FLAGS = [
  "--no-sandbox",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
  "--single-process",
  "--font-render-hinting=none",
  "--disable-font-subpixel-positioning",
  "--disable-lcd-text",
  "--force-color-profile=srgb",
  "--disable-accelerated-2d-canvas",
  "--disable-gpu-compositing",
  "--disable-software-rasterizer",
  "--disable-skia-runtime-opts",
  "--disable-partial-raster",
  "--disable-backing-store-limit",
  "--use-gl=swiftshader",
  "--force-device-scale-factor=1",
  "--disable-features=CalculateNativeWinOcclusion,VizDisplayCompositor",
  "--disable-accelerated-video-decode",
  "--disable-canvas-aa",
  "--disable-2d-canvas-clip-aa",
  "--disable-webgl",
  "--disable-webgl2",
  "--blink-settings=imageAnimationPolicy=noAnimation",
  "--disable-smooth-scrolling",
  "--disable-threaded-animation",
  "--disable-threaded-scrolling",
  "--disable-checker-imaging",
];

// Flags that are just required to run (not rendering related) - skip ablation
const INFRA_FLAGS = new Set([
  "--no-sandbox",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--single-process",
]);

const knobs = [];

// Chrome flag knobs (each flag gets toggled off individually)
for (const flag of ALL_CHROME_FLAGS) {
  if (INFRA_FLAGS.has(flag)) continue;
  const id = flag
    .replace(/^--/, "")
    .replace(/=.*/, "")
    .replace(/[^a-zA-Z0-9]/g, "_");
  knobs.push({
    id: `flag_${id}`,
    description: `Chrome flag: ${flag}`,
    category: "chrome_flag",
    flagToRemove: flag,
  });
}

// CSS knobs
knobs.push({
  id: "css_font_smoothing",
  description: "CSS: -webkit-font-smoothing: none",
  category: "css",
  cssOverride:
    "*, :root, body { -webkit-font-smoothing: auto !important; -moz-osx-font-smoothing: auto !important; font-smooth: auto !important; }",
});

knobs.push({
  id: "css_text_rendering",
  description: "CSS: text-rendering: geometricPrecision",
  category: "css",
  cssOverride:
    "*, :root, body { text-rendering: auto !important; }",
});

knobs.push({
  id: "css_animation_disable",
  description: "CSS: animation/transition duration 0s",
  category: "css",
  cssOverride:
    "*, *::before, *::after { animation-duration: revert !important; animation-delay: revert !important; transition-duration: revert !important; transition-delay: revert !important; }",
});

knobs.push({
  id: "css_hermetic_font",
  description: "CSS: Force Inter font family everywhere",
  category: "css",
  // Remove the font override - let system fonts through
  cssOverride:
    "*, :root, body { font-family: revert !important; }",
});

// Environment variable knobs
knobs.push({
  id: "env_fontconfig",
  description: "Env: FONTCONFIG_FILE (hermetic fontconfig)",
  category: "env",
  envRemove: "FONTCONFIG_FILE",
});

knobs.push({
  id: "env_freetype",
  description: "Env: FREETYPE_PROPERTIES (no stem darkening)",
  category: "env",
  envRemove: "FREETYPE_PROPERTIES",
});

// Media feature knobs
knobs.push({
  id: "media_color_scheme",
  description: "Media: prefers-color-scheme=light",
  category: "media",
  mediaOverride: [{ name: "prefers-reduced-motion", value: "reduce" }], // keep reduced-motion, drop color-scheme
});

knobs.push({
  id: "media_reduced_motion",
  description: "Media: prefers-reduced-motion=reduce",
  category: "media",
  mediaOverride: [{ name: "prefers-color-scheme", value: "light" }], // keep color-scheme, drop reduced-motion
});

// Viewport knobs
knobs.push({
  id: "viewport_scale_factor",
  description: "Viewport: deviceScaleFactor=1 (test with 2)",
  category: "viewport",
  viewport: { width: 1200, height: 800, deviceScaleFactor: 2 },
});

// ── Server ────────────────────────────────────────────────────────────

async function startServer(harnessDir) {
  const testsDir = dirname(harnessDir);
  const server = createServer(async (req, res) => {
    let urlPath = req.url.split("?")[0];
    if (urlPath.endsWith("/")) urlPath += "index.html";
    const filePath = join(testsDir, urlPath);
    try {
      const content = await readFile(filePath);
      const ext = extname(filePath);
      res.setHeader(
        "Content-Type",
        CONTENT_TYPES[ext] || "application/octet-stream"
      );
      res.writeHead(200);
      res.end(content);
    } catch (err) {
      if (err.code === "ENOENT") {
        res.writeHead(404);
        res.end("Not found: " + urlPath);
      } else {
        res.writeHead(500);
        res.end("Server error: " + err.message);
      }
    }
  });

  return new Promise((resolve) => {
    server.listen(0, () => {
      resolve({ server, port: server.address().port });
    });
  });
}

// ── Screenshot + comparison ───────────────────────────────────────────

function compareImages(baselineBuffer, actualBuffer) {
  const baseline = PNG.sync.read(baselineBuffer);
  const actual = PNG.sync.read(actualBuffer);

  if (actual.width !== baseline.width || actual.height !== baseline.height) {
    return {
      match: false,
      reason: `dimensions: ${baseline.width}x${baseline.height} vs ${actual.width}x${actual.height}`,
      diffPixels: -1,
      diffPercent: -1,
    };
  }

  const diff = new PNG({ width: baseline.width, height: baseline.height });
  const numDiff = pixelmatch(
    baseline.data,
    actual.data,
    diff.data,
    baseline.width,
    baseline.height,
    { threshold: 0.1 } // stricter threshold than production to detect subtle changes
  );
  const total = baseline.width * baseline.height;
  const pct = (numDiff / total) * 100;

  return {
    match: numDiff === 0,
    diffPixels: numDiff,
    diffPercent: pct,
    diffPng: PNG.sync.write(diff),
  };
}

async function takeScreenshot(page, port, pageName) {
  const url = `http://127.0.0.1:${port}/harness/?page=${pageName}`;
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.waitForSelector("#app > *", { timeout: 5000 });
  await new Promise((r) => setTimeout(r, 200));
  const element = await page.$("#app");
  const data = await element.screenshot();
  return Buffer.isBuffer(data) ? data : Buffer.from(data);
}

// ── Main ──────────────────────────────────────────────────────────────

async function runAblation() {
  const harnessPath =
    process.env.HARNESS_PATH ||
    join(__dirname, "harness/dist/harness.js");
  const distDir = dirname(harnessPath);
  const harnessDir = distDir.endsWith("/dist") ? dirname(distDir) : distDir;

  if (!existsSync(join(harnessDir, "index.html"))) {
    console.error(`Harness index.html not found in: ${harnessDir}`);
    process.exit(1);
  }

  if (!existsSync(OUTPUT_DIR)) {
    mkdirSync(OUTPUT_DIR, { recursive: true });
  }

  const { server, port } = await startServer(harnessDir);
  console.log(`Server on port ${port}`);

  // Saved environment (for env knobs)
  const savedEnv = { ...process.env };

  const results = [];

  // ── Step 1: Baseline (all knobs on) ─────────────────────────────
  console.log("\n=== BASELINE (all knobs on) ===");
  const baselines = {};
  {
    const userDataDir = join(
      process.env.TEST_TMPDIR || "/tmp",
      "knob-ablation-baseline"
    );
    mkdirSync(userDataDir, { recursive: true });

    const launchOpts = {
      headless: true,
      userDataDir,
      args: [...ALL_CHROME_FLAGS],
    };
    if (process.env.PUPPETEER_EXECUTABLE_PATH) {
      let ep = process.env.PUPPETEER_EXECUTABLE_PATH;
      const pe = join(ep, "chrome-linux", "headless_shell");
      if (existsSync(pe)) ep = pe;
      launchOpts.executablePath = ep;
    }

    const browser = await puppeteer.launch(launchOpts);
    const page = await browser.newPage();
    await page.setViewport({ width: 1200, height: 800, deviceScaleFactor: 1 });
    await page.emulateMediaFeatures([
      { name: "prefers-color-scheme", value: "light" },
      { name: "prefers-reduced-motion", value: "reduce" },
    ]);

    for (const scenario of SCENARIOS) {
      const buf = await takeScreenshot(page, port, scenario);
      baselines[scenario] = buf;
      writeFileSync(join(OUTPUT_DIR, `baseline-${scenario}.png`), buf);
      console.log(`  ${scenario}: ${buf.length} bytes`);
    }

    await browser.close();
  }

  // ── Step 2: Ablate each knob ────────────────────────────────────
  for (const knob of knobs) {
    console.log(`\n--- Ablating: ${knob.id} (${knob.description}) ---`);

    // Build chrome flags for this run
    let flags = [...ALL_CHROME_FLAGS];
    if (knob.category === "chrome_flag") {
      flags = flags.filter((f) => f !== knob.flagToRemove);
    }

    // Set/unset env vars
    if (knob.envRemove) {
      delete process.env[knob.envRemove];
    }

    const userDataDir = join(
      process.env.TEST_TMPDIR || "/tmp",
      `knob-ablation-${knob.id}`
    );
    mkdirSync(userDataDir, { recursive: true });

    const launchOpts = {
      headless: true,
      userDataDir,
      args: flags,
    };
    if (process.env.PUPPETEER_EXECUTABLE_PATH) {
      let ep = process.env.PUPPETEER_EXECUTABLE_PATH;
      const pe = join(ep, "chrome-linux", "headless_shell");
      if (existsSync(pe)) ep = pe;
      launchOpts.executablePath = ep;
    }

    // Wrap the entire knob test in try-catch + timeout — some flag combos crash/hang Chrome
    let browser;
    const knobTimeout = new Promise((_, reject) =>
      setTimeout(() => reject(new Error("TIMEOUT")), KNOB_TIMEOUT_MS)
    );
    try {
      launchOpts.protocolTimeout = 15_000;
      browser = await Promise.race([puppeteer.launch(launchOpts), knobTimeout]);

      const page = await Promise.race([browser.newPage(), knobTimeout]);

      // Viewport
      const vp = knob.viewport || {
        width: 1200,
        height: 800,
        deviceScaleFactor: 1,
      };
      await page.setViewport(vp);

      // Media features
      const mediaFeatures = knob.mediaOverride || [
        { name: "prefers-color-scheme", value: "light" },
        { name: "prefers-reduced-motion", value: "reduce" },
      ];
      await page.emulateMediaFeatures(mediaFeatures);

      for (const scenario of SCENARIOS) {
        try {
          // Navigate (with knob timeout)
          const url = `http://127.0.0.1:${port}/harness/?page=${scenario}`;
          await Promise.race([
            page.goto(url, { waitUntil: "networkidle0" }),
            knobTimeout,
          ]);
          await Promise.race([
            page.waitForSelector("#app > *", { timeout: 5000 }),
            knobTimeout,
          ]);

          // Inject CSS override if applicable
          if (knob.cssOverride) {
            await page.addStyleTag({ content: knob.cssOverride });
            await new Promise((r) => setTimeout(r, 100));
          }

          await new Promise((r) => setTimeout(r, 200));

          const element = await page.$("#app");
          const screenshotBuf = await element.screenshot();
          const buf = Buffer.isBuffer(screenshotBuf)
            ? screenshotBuf
            : Buffer.from(screenshotBuf);

          writeFileSync(
            join(OUTPUT_DIR, `${knob.id}-${scenario}-actual.png`),
            buf
          );

          const cmp = compareImages(baselines[scenario], buf);

          if (cmp.diffPng && cmp.diffPixels > 0) {
            writeFileSync(
              join(OUTPUT_DIR, `${knob.id}-${scenario}-diff.png`),
              cmp.diffPng
            );
          }

          const status =
            cmp.diffPixels === 0
              ? "IDENTICAL"
              : cmp.diffPixels < 0
                ? "SIZE_MISMATCH"
                : `DIFFERS (${cmp.diffPixels}px, ${cmp.diffPercent.toFixed(3)}%)`;

          console.log(`  ${scenario}: ${status}`);

          results.push({
            knob: knob.id,
            category: knob.category,
            description: knob.description,
            scenario,
            diffPixels: cmp.diffPixels,
            diffPercent: cmp.diffPercent,
            error: null,
          });
        } catch (err) {
          console.error(`  ${scenario}: ERROR - ${err.message}`);
          results.push({
            knob: knob.id,
            category: knob.category,
            description: knob.description,
            scenario,
            diffPixels: -1,
            diffPercent: -1,
            error: err.message,
          });
        }
      }
    } catch (err) {
      // Browser launch or page setup failed entirely
      const shortErr = err.message.split("\n")[0].substring(0, 80);
      console.error(`  FAILED: ${shortErr}`);
      for (const scenario of SCENARIOS) {
        // Only push if not already pushed by inner loop
        const existing = results.find(
          (r) => r.knob === knob.id && r.scenario === scenario
        );
        if (!existing) {
          results.push({
            knob: knob.id,
            category: knob.category,
            description: knob.description,
            scenario,
            diffPixels: -1,
            diffPercent: -1,
            error: shortErr,
          });
        }
      }
    }

    // Clean up browser — SIGKILL first (browser.close() hangs on unresponsive Chrome)
    if (browser) {
      const proc = browser.process();
      if (proc && proc.pid) {
        try { process.kill(proc.pid, "SIGKILL"); } catch (_) {}
      }
      await browser.close().catch(() => {});
    }
    // Nuclear option: kill any leftover headless_shell processes
    try {
      execSync("pkill -9 -f headless_shell 2>/dev/null || true", {
        timeout: 5000,
      });
    } catch (_) {}
    await new Promise((r) => setTimeout(r, 500));

    // Restore env
    if (knob.envRemove && savedEnv[knob.envRemove]) {
      process.env[knob.envRemove] = savedEnv[knob.envRemove];
    }
  }

  server.close();

  // ── Step 3: Summary ─────────────────────────────────────────────
  console.log("\n\n" + "=".repeat(100));
  console.log("ABLATION SUMMARY");
  console.log("=".repeat(100));

  // Header
  const hdr = [
    "Knob".padEnd(45),
    "Category".padEnd(14),
    ...SCENARIOS.map((s) => s.padEnd(30)),
  ].join(" | ");
  console.log(hdr);
  console.log("-".repeat(hdr.length));

  for (const knob of knobs) {
    const cols = [knob.id.padEnd(45), knob.category.padEnd(14)];
    for (const scenario of SCENARIOS) {
      const r = results.find(
        (x) => x.knob === knob.id && x.scenario === scenario
      );
      if (!r) {
        cols.push("???".padEnd(30));
      } else if (r.error) {
        cols.push(`ERROR: ${r.error}`.substring(0, 30).padEnd(30));
      } else if (r.diffPixels === 0) {
        cols.push("IDENTICAL".padEnd(30));
      } else if (r.diffPixels < 0) {
        cols.push("SIZE MISMATCH".padEnd(30));
      } else {
        cols.push(
          `${r.diffPixels}px (${r.diffPercent.toFixed(3)}%)`.padEnd(30)
        );
      }
    }
    console.log(cols.join(" | "));
  }

  // Save JSON results
  writeFileSync(
    join(OUTPUT_DIR, "ablation-results.json"),
    JSON.stringify(results, null, 2)
  );

  // Save markdown summary
  let md = "# Knob Ablation Results\n\n";
  md += `Execution environment: ${process.env.EXECUTION_ENV || "unknown"}\n\n`;
  md += "Each row shows what happens when that knob is **removed** from the baseline.\n\n";
  md += "| Knob | Category | " + SCENARIOS.join(" | ") + " |\n";
  md += "| --- | --- | " + SCENARIOS.map(() => "---").join(" | ") + " |\n";
  for (const knob of knobs) {
    const cells = [knob.id, knob.category];
    for (const scenario of SCENARIOS) {
      const r = results.find(
        (x) => x.knob === knob.id && x.scenario === scenario
      );
      if (!r) {
        cells.push("?");
      } else if (r.error) {
        cells.push(`ERROR: ${r.error.substring(0, 50)}`);
      } else if (r.diffPixels === 0) {
        cells.push("IDENTICAL");
      } else if (r.diffPixels < 0) {
        cells.push("SIZE MISMATCH");
      } else {
        cells.push(`${r.diffPixels}px (${r.diffPercent.toFixed(3)}%)`);
      }
    }
    md += "| " + cells.join(" | ") + " |\n";
  }
  writeFileSync(join(OUTPUT_DIR, "ablation-results.md"), md);

  console.log(`\nResults saved to ${OUTPUT_DIR}/`);

  const hasChanges = results.some((r) => r.diffPixels !== 0 && !r.error);
  const identical = results.filter((r) => r.diffPixels === 0).length;
  const different = results.filter((r) => r.diffPixels > 0).length;
  const errors = results.filter((r) => r.error).length;
  const sizeMismatch = results.filter((r) => r.diffPixels < 0 && !r.error).length;

  console.log(`\nTotals: ${identical} identical, ${different} different, ${sizeMismatch} size mismatch, ${errors} errors`);
}

runAblation().catch((err) => {
  console.error("Ablation test failed:", err);
  process.exit(1);
});
