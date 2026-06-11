# Visual Regression Testing Alternatives: Exploration

**Context**: We have Svelte components (props/frontend) and want to generate PNG snapshots for visual regression testing. Current setup uses Storybook + Playwright, but Playwright has module identity issues in our Bazel + pnpm workspace setup (see [known-problems.md](known-problems.md)).

**Goal**: Explore viable alternatives that can work with Bazel, pnpm workspaces, and Svelte components.

---

## 1. Headless Browser Tools for Screenshots

### Playwright

**Status**: Currently deployed but broken in Bazel

**Rendering**: Headless browser (Chromium, Firefox, WebKit)
**Component support**: Needs Storybook or custom HTML harness
**Bazel integration**: rules_playwright available, works but has module identity issues with pnpm workspaces
**Svelte support**: Yes, via Storybook or custom harness
**Module identity issue**: YES - different module instances break test context tracking
**Performance**: ~2-5 sec per screenshot (full browser startup)

**Known issues in our setup**:

- pnpm workspace creates separate node_modules per project
- Playwright loads from different paths in runner vs test file
- Different module instances cause "Playwright Test did not expect test() to be called here" crash
- Attempted fixes (moving to root package.json, NODE_PATH, etc.) all failed

**Mitigation options**:

- Use Playwright in a single-package setup (flatten workspace) - but breaks other tools' module resolution
- Use Playwright outside Bazel for manual testing only
- Switch to different approach

---

### Puppeteer

**Rendering**: Headless Chrome only
**Component support**: Needs Storybook or custom HTML harness
**Bazel integration**: No official rules, requires custom setup
**Svelte support**: Yes, via Storybook or custom harness
**Module identity issue**: Possibly less severe (Google maintains it) but no proven Bazel solution
**Performance**: ~2-5 sec per screenshot (Chrome startup)

**Advantages**:

- Simpler than Playwright (Chrome only, no cross-browser complexity)
- Lighter weight
- Better maintained by Google

**Disadvantages**:

- Chrome only (no Firefox, WebKit)
- No native snapshot comparison
- Requires jest-image-snapshot or similar for comparisons
- Still requires headless browser (slower than alternatives)

**Viability**: Possible but not significantly better than Playwright for our use case

---

### Cypress

**Rendering**: Headless browser (Chrome, Firefox, Edge)
**Component support**: Cypress Component Testing
**Bazel integration**: No official rules, requires custom setup
**Svelte support**: Limited - Cypress focuses on React/Vue
**Module identity issue**: Unknown for Svelte in Bazel
**Performance**: ~2-5 sec per screenshot

**Advantages**:

- Developer-friendly UI
- Time-travel debugging

**Disadvantages**:

- Not designed for Svelte components
- Limited visual testing (needs plugins like Percy)
- Heavyweight for just screenshots

**Viability**: Not suitable for our Svelte focus

---

## 2. Storybook Alternatives for Component Rendering

Current setup: Storybook 8.x + SvelteKit integration

### Ladle

**Description**: Vite-based lightweight alternative to Storybook, React-only
**Component support**: React components only
**Rendering**: Vite dev server + custom UI
**Bazel integration**: Possible but no existing rules
**Svelte support**: NO - explicitly React-only
**Performance**: 1.2s cold start, <500ms hot reload (much faster than Storybook)

**Quote from Ladle founder**: "Ladle has no plans to add support for other frameworks than React since they are a React-only shop. If you are looking for Vue/Svelte Vite based alternatives, you should try Histoire."

**Viability**: Not viable for Svelte

---

### Histoire

**Description**: Vite-based simpler alternative to Storybook
**Component support**: Vue, Svelte support
**Rendering**: Vite dev server
**Bazel integration**: No existing rules
**Svelte support**: YES - but with caveats
**Performance**: ~2s cold start, <1s hot reload (better than Storybook)
**Known issues**: Development is slow, doesn't work well with Svelte 5 yet

**Advantages**:

- Fast startup
- Svelte support
- Simpler configuration
- Smaller bundle

**Disadvantages**:

- Slow development cycle
- Svelte 5 compatibility issues
- No visual regression testing built-in (would need external tool)
- Less ecosystem/plugins

**Viability**: Possible but uncertain due to Svelte 5 issues and development pace

---

### Kitbook

**Description**: SvelteKit-based Storybook alternative, Svelte-only
**Rendering**: SvelteKit itself
**Component support**: Svelte components
**Bazel integration**: No existing rules
**Svelte support**: YES - designed for Svelte
**Performance**: SvelteKit dev server speed

**Advantages**:

- Built specifically for Svelte
- Leverages SvelteKit machinery

**Disadvantages**:

- Smaller ecosystem
- No built-in visual regression testing
- SvelteKit dependency (may complicate setup)

**Viability**: Possible but smaller ecosystem

---

### Sveltescape

**Description**: Storybook alternative made with Svelte for Svelte
**Component support**: Svelte components
**Bazel integration**: No existing rules
**Svelte support**: YES
**Performance**: Unknown

**Viability**: Very experimental, limited adoption

---

## 3. Direct Component Rendering (No Browser)

### Satori + resvg-js + Svelte SSR

**Description**: Server-side rendering without headless browser
**Rendering approach**:

1. Import Svelte component on Node.js server
2. Call component's `.render()` method to get HTML string
3. Convert HTML to SVG using Satori
4. Convert SVG to PNG using resvg-js
5. No browser process needed

**Performance**: 5-100x faster than headless browser (no browser startup)
**Svelte support**: YES - Svelte components have `.render()` method
**Bazel integration**: Via node_modules (Satori + resvg-js npm packages)
**Module identity issue**: NO - pure Node.js, no Playwright/module identity problems

**Limitations**:

- Satori only supports subset of CSS (see Satori CSS Guidelines)
- Must use `<svelte:options css="injected" />` in components (required for Svelte 5)
- No JavaScript execution (CSS-only rendering)
- No browser events or interactions
- Works well for static snapshots only

**Example workflow**:

```bash
# Programmatically in Node.js test
const Component = require('./Button.svelte').default;
const html = Component.render({ variant: 'primary' }).html;
const svg = await Satori(html, { width: 800, height: 600, ... });
const png = await resvg.render(svg).asPng();
fs.writeFileSync('button-primary.png', png);
```

**Advantages**:

- NO browser process = much faster
- NO module identity issues = works with Bazel + pnpm
- Simple Node.js script = easy Bazel integration
- Can be parallelized efficiently
- Deterministic (same output every time)

**Disadvantages**:

- Limited CSS support (Satori limitations)
- No JavaScript/interactivity
- Components must be styled carefully
- Not good for complex responsive layouts

**Viability**: HIGHLY VIABLE if components use standard CSS, no JS-heavy interactions

---

### ssrender or similar SSR tools

**Description**: Generic server-side component rendering tools
**Viability**: Could work similarly to Satori approach, but less battle-tested

---

## 4. Visual Regression Testing Tools (Standalone)

These tools can work with any rendering method:

### Chromatic

**Type**: SaaS visual testing
**Cost**: Paid (per snapshot)
**Integration**: Cloud-based, Git-aware
**Bazel integration**: External webhook/API
**Svelte support**: Yes, via Storybook integration
**Module identity issue**: Not applicable (cloud-based)

**Pros**: Powerful visual diff tools, automatic indexing
**Cons**: Paid, external dependency, not self-contained

---

### Lost Pixel

**Type**: Both open-source and SaaS
**Cost**: Open-source free, SaaS has paid tiers
**Integration**: Works with Storybook, SvelteKit, e2e tests
**Bazel integration**: External tool (runs snapshot comparison)
**Svelte support**: Yes

**Viability**: Could complement any rendering approach

---

### Percy (BrowserStack)

**Type**: SaaS visual testing
**Cost**: Paid
**Viability**: Similar to Chromatic, but Bazel integration is external

---

## 5. Custom Harness Approaches

### Skip Storybook, Build Component Harness

**Idea**: Create minimal HTML harness that loads Svelte components directly without Storybook

**Example**:

```html
<!-- harness.html -->
<link rel="stylesheet" href="src/app.css" />
<div id="app"></div>
<script type="module">
  import Button from "./src/Button.svelte";
  new Button({
    target: document.getElementById("app"),
    props: { label: "Click" },
  });
</script>
```

**Advantages**:

- Removes Storybook complexity
- Smaller dev server
- Can use any screenshot tool (Playwright, Puppeteer, Satori)

**Disadvantages**:

- Manual harness maintenance for each component
- Less feature-rich than Storybook (no addons, etc.)
- Requires careful component setup

**Viability**: Viable if we're willing to drop Storybook ecosystem benefits

---

## Comparison Matrix

| Tool/Approach      | Rendering        | Svelte? | Bazel Ready?  | Speed     | Module Identity Issue? | Ecosystem        |
| ------------------ | ---------------- | ------- | ------------- | --------- | ---------------------- | ---------------- |
| **Playwright**     | Headless browser | Yes     | Via Storybook | Slow      | YES ❌                 | Excellent        |
| **Puppeteer**      | Headless Chrome  | Yes     | Custom        | Slow      | Maybe                  | Good             |
| **Cypress**        | Headless browser | Limited | No            | Slow      | Unknown                | Good (JS)        |
| **Ladle**          | Vite dev         | NO      | No            | Fast      | N/A                    | Minimal          |
| **Histoire**       | Vite dev         | Yes     | No            | Fast      | No                     | Small            |
| **Kitbook**        | SvelteKit        | Yes     | No            | Medium    | No                     | Minimal          |
| **Satori + resvg** | SSR + conversion | Yes     | Excellent     | VERY FAST | NO ✅                  | Growing          |
| **Chromatic**      | Cloud            | Yes     | External      | Varies    | N/A                    | Excellent (SaaS) |
| **Lost Pixel**     | Any              | Yes     | External      | Varies    | N/A                    | Good             |

---

## Recommendations

### Option A: Switch to Satori + resvg-js (Recommended)

**Best for**: Fast, deterministic snapshots without browser complexity

**Setup**:

1. Create Node.js test script that imports Svelte components
2. Use Svelte's `.render()` method to generate HTML
3. Pipe through Satori → resvg-js to generate PNG
4. Store snapshots in git
5. Create Bazel rule wrapping the Node.js script

**Advantages**:

- NO module identity issues
- 5-100x faster than Playwright
- Works perfectly with Bazel + pnpm workspaces
- Deterministic output
- Lightweight dependencies

**Disadvantages**:

- CSS subset limitations (Satori)
- No JavaScript interactions
- Need to refactor complex components

**Effort**: Medium (component migration + test infrastructure)

---

### Option B: Keep Playwright but Fix Module Identity

**Best for**: If cross-browser testing is critical

**Approaches tried that failed**:

- Moving @playwright/test to root
- NODE_PATH manipulation
- Adding explicit data deps

**Untried approaches**:

- Use Playwright outside Bazel (local testing only)
- Flatten workspace completely (breaks other tools)
- Use Playwright as external tool (like Lost Pixel does)

**Viability**: Low - root cause is fundamental to pnpm workspace design

---

### Option C: Migrate to Histoire + External Tool

**Best for**: Keeping component library tool without Storybook overhead

**Setup**:

1. Migrate components to Histoire (Vite-based, faster than Storybook)
2. Run custom screenshot tool (Playwright, Puppeteer, or custom) against Histoire dev server
3. Use Lost Pixel or similar for visual comparison

**Challenges**:

- Svelte 5 compatibility issues with Histoire
- Slow development pace on Histoire

**Viability**: Medium - depends on Histoire updates

---

### Option D: Custom Harness + Playwright

**Best for**: Minimal setup, full Playwright power without Storybook

**Setup**:

1. Create simple HTML harness for components
2. Use Playwright to screenshot the harness
3. Module identity issue might still occur

**Viability**: Medium - still has Playwright module issue

---

## Next Steps

### Immediate (Low Risk)

1. **Profile Satori approach**:
   - Create proof-of-concept with 2-3 props/frontend components
   - Measure rendering speed vs Playwright
   - Assess CSS limitations
   - Document required component changes

2. **Investigate Histoire + Svelte 5**:
   - Check current state of Svelte 5 support
   - Review open issues and PRs
   - Determine timeline for fixes

### Medium-term

1. If Satori PoC succeeds: Migrate props/frontend components to SSR-friendly style
2. Create Bazel rules for Satori-based screenshot testing
3. Add snapshot comparison (jest-image-snapshot or custom)

### Long-term

1. Evaluate Histoire maturity for Svelte 5
2. Consider whether to keep Storybook or migrate entire ecosystem

---

## References and Research

### Visual Testing Tools

- [Best Mobile Visual Testing Tools 2025 - BrowserStack](https://www.browserstack.com/guide/best-mobile-visual-testing-tools)
- [Ultimate Guide to Visual Regression Testing - Lost Pixel](https://www.lost-pixel.com/blog/ultimate-visual-regression-testing-tools-guide)
- [Playwright Visual Testing alternatives - Chromatic](https://www.chromatic.com/compare/playwright)

### Storybook Alternatives

- [Storybook 10 vs Ladle vs Histoire - DEV Community](https://dev.to/saswatapal/storybook-10-why-i-chose-it-over-ladle-and-histoire-for-component-documentation-2omn)
- [Visual regression testing with Ladle - Lost Pixel](https://www.lost-pixel.com/blog/visual-regression-testing-with-ladle)
- [Ladle v3 blog](https://ladle.dev/blog/ladle-v3/)
- [Kitbook - SvelteKit Storybook alternative](https://github.com/jacob-8/kitbook)
- [Sveltescape - Storybook alternative for Svelte](https://github.com/AlessioGr/sveltescape)

### Headless Rendering

- [svelte-component-to-image - GitHub](https://github.com/StephenGunn/svelte-component-to-image)
- [Generating dynamic social images with Satori - DEV](https://dev.to/theether0/dynamic-og-image-with-satori-4438)
- [Satori + resvg-js with SvelteKit - Lee Reamsnyder](https://www.leereamsnyder.com/dynamic-social-media-images-with-sveltekit-and-resvg-js)
- [Create dynamic social card images with Svelte - Geoff Rich](https://geoffrich.net/posts/svelte-social-image/)

### Bazel + JavaScript Integration

- [rules_playwright - Bazel Central Registry](https://registry.bazel.build/modules/rules_playwright)
- [rules_playwright examples - GitHub](https://github.com/mrmeku/rules_playwright)
- [rules_js documentation - aspect-build](https://github.com/aspect-build/rules_js)
- [Using Bazel with Vite and Vitest - Medium](https://medium.com/@yanirmanor/using-bazel-with-vite-and-vitest-c75b133f4707)

### Puppeteer / Playwright Comparisons

- [Playwright vs Puppeteer comparison - BrowserStack](https://www.browserstack.com/guide/playwright-vs-puppeteer)
- [Cypress vs Playwright comparison - Katalon](https://katalon.com/resources-center/blog/playwright-vs-cypress)
- [Visual Regression Testing with Puppeteer - BrowserStack](https://www.browserstack.com/guide/visual-regression-testing-with-puppeteer)
