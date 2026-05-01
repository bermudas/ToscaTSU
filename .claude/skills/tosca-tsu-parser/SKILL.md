---
name: tosca-tsu-parser
description: 'Parse Tricentis Tosca .tsu test-case exports into a structured JSON manifest, an interactive HTML locator-quality report, and a runnable Playwright project (page objects, .env.example with CP-parameter traceability, per-spec test.use baseURL pinning). Use whenever the user mentions .tsu files, Tosca exports, the parse_tsu.py script, converting Tosca tests to Playwright, or analyzing Tosca modules and locators. Covers the .tsu entity graph (Modules, XParams, ControlFlow, ParameterLayer), Tosca value tokens and composite patterns, ActionMode dispatch, locator priority from SelfHealingData, encrypted-credential handling, and the stabilization loop for hardening auto-generated specs against the live app via browser MCP or Playwright codegen. Self-improving — extend the script and this skill together when new patterns surface.'
---

# Tosca .tsu Parser

The script lives at `scripts/parse_tsu.py` inside this skill. In the original repo it also exists at the project root for direct CLI use; the bundled copy is for portability when the skill is installed elsewhere. **Both copies are byte-identical** — point users to whichever is more convenient in their layout.

`parse_tsu.py` decodes a gzipped Tosca `.tsu` export and produces three artefacts. The **JSON manifest is the canonical product** — everything else (HTML, Playwright spec) is derived from it. When working with .tsu files, route questions through the manifest first; only re-read the raw .tsu when a structural question can't be answered from the manifest.

> **The auto-generated spec is a starting point, not a finished test.** Tosca records against a specific snapshot of the app (DOM, locale, routing, auth state). The live app drifts. Use the **stabilization loop** below to harden the spec against current reality — don't expect a green run on first try.

## CLI

```bash
python3 parse_tsu.py <file.tsu>                    # HTML report only
python3 parse_tsu.py <file.tsu> --steps-json       # + JSON manifest
python3 parse_tsu.py <file.tsu> --playwright       # + Playwright project
python3 parse_tsu.py <file.tsu> --all              # all three
python3 parse_tsu.py <file.tsu> --all --force      # also overwrite hand-edited spec/config
```

Outputs land in the same directory as the .tsu:
- `<stem>_report.html` — interactive locator-quality review (open in browser)
- `<stem>_steps.json` — the canonical structured manifest
- `playwright-test/` — `playwright.config.ts`, `package.json`, `pages/*.page.ts`, `tests/<stem>.spec.ts`

`--force` re-overwrites the spec and config (page objects always regenerate). Without `--force`, hand-edits to the spec are preserved across re-runs.

## Single-TC vs multi-TC `.tsu` (per-case isolation)

A `.tsu` may contain a single test case or many (real-world exports often bundle 10–50 cases). The parser auto-detects and adapts the output layout:

**Single-TC `.tsu`** — flat layout for back-compat:
```
out/<stem>/
├── <stem>_steps.json
├── <stem>_report.html
└── playwright-test/
    ├── playwright.config.ts, package.json, .env.example
    ├── pages/*.page.ts          (every XModule used by the test)
    └── tests/<stem>.spec.ts
```

**Multi-TC `.tsu`** — shared playwright project + per-case audit reports:
```
out/<stem>/
├── playwright-test/             ← SHARED across all cases
│   ├── playwright.config.ts     ← single config (env-overridable baseURL)
│   ├── package.json
│   ├── .env.example             ← UNION of every env var across all cases
│   ├── pages/                   ← deduplicated union (each module appears once)
│   └── tests/<area>/<tc_id>_<name>.spec.ts
└── cases/<tc_id>_<name>/        ← per-case audit outputs
    ├── steps.json               ← JSON manifest filtered to this case's closure
    └── report.html              ← HTML report listing only this case's modules
```

Each case is **isolated by transitive closure** — for a given TestCase, the parser walks `Items / TestStepValues / SubValues / ModuleAttribute / Module / Properties / ParentAttribute / ReusedItem / ParameterLayer*`, recursively following RTB references (an RTB calling another RTB pulls both into the closure). The HTML "Module Catalogue" tab and the per-case JSON manifest only include entities in that closure, so a case with 1300 reachable entities doesn't see the other 4500.

The shared `pages/` dir naturally deduplicates: each per-TC pipeline writes only its own modules; over multiple cases the same module file just gets the same content written twice (idempotent). `.env.example` is written once after all cases are processed, with the union of every `process.env.X` reference any spec uses — plus the CP[*] traceback hints showing which Tosca config-parameter each var corresponds to.

The `<area>` folder under `tests/` comes from the Tosca TCFolder hierarchy (immediate parent folder name, sanitized; generic workspace folders like `TestCases`/`Library` are skipped). When all cases share a parent, they all land in `tests/<area>/`. When TCs span multiple folders, the layout naturally splits.

## Generated spec, ready-to-run scaffolding

Each generated `tests/<stem>.spec.ts` is **self-contained**:

- `test.use({ baseURL: process.env.BASE_URL || '<recorded URL>' })` at the top — pins the recorded URL for that specific .tsu, but lets a `BASE_URL=…` env override target a different environment without editing the spec.
- All credential references resolve to `process.env.<NAME>` (sourced from Tosca `{CP[X]}` config-parameter chains).
- A `playwright-test/.env.example` is written next to the project listing every env var the spec references, with **traceability hints** showing the Tosca `CP[*]` source. Example:
  ```
  ALCROB2BURL=     (from CP[AlcroB2BURL] / Url)
  USERPWD=         (from CP[UserPWD] / Password)
  EMAIL_ADDRESS_USERNAME= (from CP[UserName] / Email Address)
  ```
- Locator chains use `.first()` after `.or()` fallbacks and on existence-style assertions (`toBeVisible`/`toBeHidden`/`toBeAttached`) to avoid strict-mode collisions out of the box.
- Tosca XPath `id('X')/path` is normalized to standard `//*[@id='X']/path` so Playwright's injected script can evaluate it.

The user copies `.env.example` to `.env`, fills real values, and runs.

## Stabilization loop (hardening the auto-gen spec against the live app)

The auto-generated spec runs the structure recorded in Tosca, but the live app may have drifted (DOM changes, locale, dynamic IDs, removed banners, OAuth flow updates). Treat the first run as a discovery probe — then iterate:

1. **Read the HTML report first.** `<stem>_report.html` flags fragile locators (red badges: dynamic XPath, Angular auto-classes, fragile-parent, class-only, generic-heading). High-density red zones predict where the spec will break before you even run it.

2. **First run.** `npm test -- <spec>`. Expect failures — note the *category*:
   - **Strict mode collision** ("resolved to N elements") — locator too broad.
   - **Element not found / timeout** — DOM drifted (renamed, restructured, removed).
   - **Wrong page / OAuth bounce** — auth flow changed; usually session/cookie state.
   - **Localization mismatch** — recorded English text vs live Swedish (or vice versa).
   - **Strange `.fill("x")` on a button** — Tosca's "set value=x" recorded against an element that's now a button; should be a click.

3. **Use a browser MCP / Playwright codegen** to explore the failure point in a real browser:
   - **Playwright's official MCP** (`@playwright/mcp`) lets you `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type` against the live app and see the actual DOM. Inspect what the failing locator was supposed to match; pick a tighter selector from the snapshot.
   - **`npx playwright codegen <URL>`** — record clicks against the live app to get up-to-date selectors for the steps that broke.
   - **`npx playwright test --debug` / `--ui`** — step through the failing spec, hover the failing locator, see what Playwright sees right before failure.

4. **Patch the spec, not the .tsu.** Hand-edits survive re-runs as long as you don't pass `--force` to `parse_tsu.py`. Common patches:
   - Replace a fragile XPath with a tighter `data-test-id` or `role+name` selector.
   - Wrap a "may-or-may-not-appear" element (cookie banner, intro modal, MFA prompt) in a tolerant click: `.click({ timeout: 3000 }).catch(() => {})`.
   - Add app-specific waits where the recorded test had implicit timing assumptions: `await page.waitForLoadState('networkidle')`, `await page.waitForResponse(/api/)`.
   - For auth flows: replace the inline `signInName/password.fill()` with a stored-state pattern (`storageState` in `playwright.config.ts`) to skip login entirely on subsequent runs.

5. **Re-run, narrow the failure window.** Each iteration should make the test pass *further*. Track which step is failing now vs the previous run — if the failure point isn't moving forward, the patch isn't working; back out and try a different angle.

6. **When the spec is green, freeze the patches.** Don't run `--force` until the next .tsu re-export. The spec file is now your source of truth for current app behavior; the .tsu is the historical Tosca recording.

7. **If the same kind of fix keeps appearing**, fold it into `parse_tsu.py` so future spec generations don't need it — that's the "self-improving" loop. (Examples already folded in: `.first()` after `.or()`, XPath quote-stripping + `id()` normalization, per-spec `test.use({baseURL})`.)

When working with this skill: **default to running the spec and iterating**, not to manually editing the .tsu. The .tsu is rarely the right thing to fix — Tosca authors edit it in their tool, and you can always re-export.

## Browser MCP setup tips

When the user has Playwright MCP available, the typical loop looks like:

```
1. browser_navigate     → app's recorded URL
2. browser_snapshot     → grab current DOM as YAML
3. <inspect snapshot>   → find the element the spec failed to locate
4. <patch spec>         → replace failing locator with a tighter one from snapshot
5. <re-run spec>        → confirm it passes that step
```

Other browser-style tools (browser_verify, headless screenshot CLIs, Selenium IDE recordings) work the same way — the goal is to reduce the gap between *what the spec expects* and *what the live app shows*. Don't speculate about DOM drift; observe it directly.

## Optional config: `parse_tsu.config.json`

Place next to the .tsu. Defaults are sensible — use this only to handle apps that:
- name their test attribute differently (`data-cy`, `data-qa`, `data-testid` instead of `data-test-id`)
- have additional Tosca-only modules to skip in spec generation

```json
{
  "test_attributes": ["data-test-id", "data-testid", "data-test", "data-cy", "data-qa"],
  "skip_modules":    ["TBox Set Buffer", "TBox Window Operation", "TBox Start Program",
                      "TBox Evaluation Tool", "TBox Dialog", "TBox Buffer"]
}
```

The first matching `test_attribute` in your .tsu wins highest locator priority.

## .tsu entity model (what to query)

A `.tsu` is gzip-compressed JSON: `{"Entities": [{Surrogate, ObjectClass, Attributes, Assocs}, …]}`. Entities reference each other by `Surrogate` (UUID). The parser walks this graph; you rarely need to.

Key classes the parser uses:

| ObjectClass | Role |
| --- | --- |
| `TestCase` / `TCProject` | Top-level test container |
| `TestStepFolder` | Folder hierarchy under a test |
| `TestStepFolderReference` | Call site for a `ReuseableTestStepBlock` — carries a `ParameterLayerReference` |
| `ReuseableTestStepBlock` | Shared step block reused across tests (`Assocs.Items` → its body) |
| `XTestStep` | Leaf test step, `Assocs.Module` → an `XModule` |
| `XModule` | Catalog of related elements (e.g. "Login page", "Cart Page") |
| `XModuleAttribute` | Single addressable element. `Assocs.Module` for top-level; `Assocs.ParentAttribute` for nested children |
| `XParam` | Locator hint on an attribute (Tag, Id, ClassName, XPath, attributes_*, SelfHealingData, RelativeId, …) |
| `XTestStepValue` | An action+value on a step. `Assocs.SubValues` contains nested SVs (recurse!) |
| `TestCaseControlFlowItem` | If/Loop. `Assocs.ControlFlowFolders` → `[Condition, Then, Else, Loop]` folders |
| `TestCaseControlFlowFolder` | Branch container; `Assocs.Items` → branch body |
| `Parameter` / `ParameterLayer` / `ParameterLayerReference` / `ParameterReference` | Per-call parameter wiring |

Five non-obvious invariants the parser depends on:

1. **`XTestStepValue.SubValues` is recursive.** A top-level SV often references a *container* attribute with `Value="{NULL}"`; the actual click target lives in a deeper leaf. The parser walks the whole SV tree and emits one action per leaf.
2. **`XModuleAttribute.ParentAttribute`** chains form a tree. Children (e.g. `P-CHECKBOX-1` inside `TERMS-AND-CONDITIONS` inside `APP-CART-TOTAL-SUMMARY`) only have `Assocs.ParentAttribute`, not a direct `Assocs.Module`. The parser walks upward until it finds a Module-bearing ancestor and indexes the child under that module.
3. **`ParameterLayer` per call site.** Each `TestStepFolderReference` carries a layer mapping the block's `{PL[X]}` parameters to outer values like `{B[Y]}`. The parser pushes/pops a layer stack as it descends `ReusedItem` bodies, so `{PL[X]}` resolves to the correct outer literal per call.
4. **`TestCaseControlFlowFolders` are named** `Condition` / `Then` / `Else` / `Loop`. The Condition branch contains a `TBox Evaluation Tool` step whose SV `Value` is the boolean expression (e.g. `{PL[ContinueOrCheckout]}=="1"`). The parser evaluates this when both sides are constants and elides the losing branch in the spec.
5. **`SelfHealingData` is JSON-in-XParam.** Each `XParam` named `SelfHealingData` has a JSON `Value` like `{"HealingParameters":{"$values":[{"Name":"Id","Value":"…","Weight":1.0}, …]}}`. The parser blends these weights with XParam type to rank locators.

## ActionMode dispatch

| Code | Mode key | Playwright equivalent |
| --- | --- | --- |
| `1`   | `input`       | `.fill(...)` (or composite gestures) |
| `37`  | `set`         | `.fill(...)`, `.click()`, `.press(...)` (depends on value) |
| `69`  | `verify`      | `await expect(...)` |
| `101` | `waitFor`     | `await expect(...).toBe...` |
| `165` | `bufferRead`  | `const x = await loc.textContent()` |
| `517` | `optionalSet` | as `set` but suffixed `.catch(() => {})` |

Unknown ActionModes log to `meta.unmapped` and emit `// TODO unmapped (unknown ActionMode 'N')`.

## Tosca value tokens (recognised)

Single-token: `{Click}`, `{DOUBLECLICK}`, `{RIGHTCLICK}`, `{CLICKDOWN}`, `{CLICKUP}`, `{HOVER}`, `{SCROLL}`, `{SCROLLINTOVIEW}`, `{FOCUS}`, `{BLUR}`, plus key tokens `{ENTER}`, `{TAB}`, `{ESCAPE}`, `{SPACE}`, `{HOME}`, `{END}`, `{BACKSPACE}`, `{DELETE}`, `{ARROWUP/DOWN/LEFT/RIGHT}`. `{NULL}` is silently skipped.

Token-with-arg: `{KEY[Ctrl+A]}` → `.press("Control+a")` (chord aliases handled — CTRL/CMD/META/WIN/OPT/DEL/ESC/PGUP/PGDN/INS/RETURN, single-char keys auto-lowercased), `{SELECT[3]}` → `selectOption({ index: 2 })`, `{SELECT[Option Name]}` → `selectOption("Option Name")`, `{WAIT[1500]}` → `page.waitForTimeout(1500)`.

Reference values (resolved through PL stack and buffer map):
- `{PL[X]}` → parameter (resolved per call site through the layer stack)
- `{B[X]}` → buffer (resolved through `buffer_map` populated by `TBox Set Buffer` steps + their `value_resolved` chain)
- `{CP[X]}` → emits `process.env.X`
- `{XL[X]}` → emits a TODO

Composite (concatenated) — `_compose_action()` decomposes:
- `{B[X]} {TAB}` → `fill(resolved_X)` + `press("Tab")`
- `{click}{sendkeys[{B[X]}]}` → `click()` + `fill(resolved_X)`
- `{KEYDOWN[CTRL]}{KEYPRESS[A]}{KEYUP[CTRL]}{KEYPRESS[DELETE]}{TEXTINPUT[X]}` — coalesces the modifier triple (KEYDOWN+KEYPRESS+KEYUP with matching modifier on outside) into a single `press("Control+a")`, then emits the rest in order
- `{SENDKEYS[X]}` / `{TYPE[X]}` / `{TEXTINPUT[X]}` — all map to `fill(X)`
- Any unrecognised fragment becomes `// TODO unmapped fragment: {…}` and the action lands in `meta.unmapped`

## ActionProperty assertions (`ASSERT_PROPS`)

| Property | True → | False → |
| --- | --- | --- |
| `Visible`  | `toBeVisible()`     | `toBeHidden()` |
| `Enabled`  | `toBeEnabled()`     | `toBeDisabled()` |
| `Disabled` | `toBeDisabled()`    | `toBeEnabled()` |
| `Checked` / `Selected` | `toBeChecked()` | `not.toBeChecked()` |
| `Focused`  | `toBeFocused()`     | `not.toBeFocused()` |
| `Editable` | `toBeEditable()`    | `not.toBeEditable()` |
| `ReadOnly` | `not.toBeEditable()` | `toBeEditable()` |
| `Exists`   | `toBeAttached()`    | `not.toBeAttached()` |

Plus value-bearing props: `Count` (numeric → `toHaveCount(N)`), `Value` (`toHaveValue(...)`), `InnerText` / unset (`toContainText(...)`, with leading/trailing `*` wildcards stripped).

## Locator priority

`collect_candidates()` builds a scored list, ranks descending, takes top as `primary` + next as `fallback`:

| Kind | Base score |
| --- | --- |
| `attributes_data-test-id` (or whatever's first in `test_attributes`) | 100 + SH weight |
| `Id` | 90 |
| Other `attributes_*` (generic handler, any test attribute name) | 70 |
| `AriaLabel` / `attributes_aria-label` | 65 |
| `getByRole + name` (when Tag maps to a role and we have visible text) | 60 |
| `Href` (links) | 50 |
| `Src` (images, non-CDN) | 45 |
| Text (`getByText`) | 40 |
| Stable `ClassName` (Angular `ng-tns-c…` stripped) | 25 |
| Custom-tag (web component, e.g. `ppg-product-card`) | 20 |
| Static XPath | 15 |
| Dynamic XPath (`pn_id_*`, `p-menubarsub_*`) | 5 |

If a `ConstraintIndex` XParam exists, the locator gets `.nth(N-1)` appended (Tosca is 1-based).

After ranking, if the attribute has a custom-tag ancestor in its `ParentAttribute` chain, the primary is wrapped: `page.locator('parent-tag').locator('...')`. This is the parent-scoping fix for strict-mode collisions.

## JSON manifest structure

```json
{
  "meta": {
    "test_name": "...",
    "project": "...",
    "base_url": "https://…",
    "all_urls": [...],
    "config": { "test_attributes": [...], "skip_modules": [...] },
    "unmapped": [
      {"step": "...", "element": "...", "mode": "set", "property": "",
       "value": "{…}", "reason": "..."}
    ]
  },
  "test_data": {
    "ProductNameTinted": "Milltex Aqua Matt Täckfärg",
    "ContinueAfterAddTinedProduct": "1",
    ...
  },
  "steps": [
    {"type":"folder", "name":"Preconditions", "depth":0},
    {"type":"block_start", "name":"Login...", "parameters":{"UserName":"{CP[UserName]}", ...}},
    {"type":"step", "name":"Click on Submit Order button", "module":"...",
     "actions":[{
       "mode":"set",
       "element":"Submit Order btn",
       "value":"{DOUBLECLICK}",
       "locator":{
         "primary":"page.locator('app-cart-total-summary').locator('[data-test-id=\"reviewOrder.submit.button\"]')",
         "fallback":"page.locator('p-button')",
         "raw":{
           "Tag":"P-BUTTON",
           "attributes_data-test-id":"reviewOrder.submit.button",
           "_self_healing":[{"name":"…","value":"…","weight":1.0}, ...],
           "_parent_chain":[{"name":"APP-CART-TOTAL-SUMMARY","tag":"APP-CART-TOTAL-SUMMARY"}]
         }
       }
     }]},
    {"type":"if", "name":"ContinueOrCheckout",
     "condition":"{PL[ContinueOrCheckout(fill 1 or 2)]}==\"1\"",
     "condition_resolved":"1==\"1\""},
    {"type":"then_start"}, {"type":"step", "...":"..."}, {"type":"then_end"},
    {"type":"else_start"}, {"type":"step", "...":"..."}, {"type":"else_end"},
    {"type":"if_end"},
    {"type":"folder_end"}
  ]
}
```

Top-level node types in `steps`: `folder` / `folder_end`, `block_start` / `block_end`, `step`, `if` / `if_end`, `then_start`/`then_end`, `else_start`/`else_end`, `loop_start`/`loop_end`.

## Tosca-encrypted credentials (what's accessible vs not)

Tosca encrypts password values in the .tsu using a **workspace-bound symmetric key**. Format: `<UUID prefix><base64 ciphertext>`. The ciphertext is short (typically 32 bytes — AES-CBC, two 16-byte blocks). The key lives in the user's Tosca workspace, not in the .tsu.

**Implications:**
- We **cannot decrypt** the blob from outside Tosca. The plaintext must be supplied by the user via env var (`process.env.X` in the spec).
- `parse_tsu.py` detects encrypted values by regex (`TOSCA_ENCRYPTED_RE`) and emits `await loc.fill(process.env.<DERIVED> ?? '');  // ⚠ Tosca-encrypted` — the comment is a flag for the user.
- The **CP[*] traceability** in `.env.example` shows which Tosca config-parameter the env var corresponds to (e.g. `USERPWD= (from CP[UserPWD] / Password)`), so users know which Tosca workspace value to look up.
- If login fails after providing the plaintext, the issue is at the auth-server / B2C / OAuth tenant level — not something parse_tsu can address. The framework's job ends at "form filled with correct value."

## Common workflows

**Convert a TSU end-to-end:**
```bash
python3 parse_tsu.py file.tsu --all
```
Then immediately check `meta.unmapped` — if empty, the parser handled every action; if populated, those are the actions you'll need to hand-fix or extend the parser for.

**Inspect a specific step's action:**
```bash
jq '.steps[] | select(.type=="step" and .name=="Click on Submit Order button") | .actions' file_steps.json
```

**List every test-attribute name the .tsu actually uses:**
```bash
jq -r '.steps[]?.actions[]?.locator.raw // {} | keys[] | select(startswith("attributes_"))' file_steps.json | sort -u
```

**Find all unmapped:**
```bash
jq '.meta.unmapped' file_steps.json
```

**Find which buffers the test depends on:**
```bash
jq '.test_data | keys[]' file_steps.json
```

**Re-run after editing config or extending the parser:**
```bash
python3 parse_tsu.py file.tsu --all --force
```

**Stabilize an auto-generated spec against the live app (the canonical loop):**
```bash
# 1. Generate
python3 parse_tsu.py myTest.tsu --all

# 2. Inspect quality
open myTest_report.html        # red badges = locator hot zones

# 3. Set up creds + URL
cp playwright-test/.env.example playwright-test/.env
$EDITOR playwright-test/.env   # fill in the listed env vars

# 4. First run
cd playwright-test && npx playwright test myTest.spec.ts --reporter=list --workers=1

# 5. For each failure: open the live app via browser MCP / codegen,
#    inspect the actual DOM at that step, patch the spec.
#    DO NOT run --force after this — preserve hand-edits.

# 6. Optional: target a different env without editing the spec
BASE_URL='https://other-env.example.com' npx playwright test myTest.spec.ts
```

## Architecture pointers (parse_tsu.py)

Line ranges are approximate and will drift as the script grows — when you extend the parser, refresh these references in the same edit.

When extending or debugging, the relevant zones:

- **Lines 50-100** — Tosca constants: `ACTION_MODE`, `CLICK_TOKENS`, `KEY_TOKENS`, `ASSERT_PROPS`, `META_XPARAMS`, regexes (`TOSCA_REF_RE`, `TOSCA_TOKEN_RE`, `TOSCA_TOKEN_ARG_RE`, `TOSCA_EXPR_RE`, `TOSCA_ENCRYPTED_RE`).
- **Lines 100-180** — entity indexing (`xparam_by_attr`, `sv_top_by_step`, `sv_children`, `attr_to_module_dir`, `attr_parent_attr`, `attr_top_module`, `parent_attr_chain`, `walk_svs`).
- **Lines 180-220** — per-call ParameterLayer index (`plr_by_call`).
- **Lines 220-410** — locator builder: `xparams`, `self_healing`, `relative_ctx`, `collect_candidates`, `build_locator`.
- **Lines 410-460** — `resolve_value` (whole-string + embedded ref resolution, bounded recursion).
- **Lines 460-560** — `step_actions` (walks SubValues recursively); `resolve_steps` (handles `XTestStep`, `TestStepFolder`, `TestStepFolderReference`, `TestCaseControlFlowItem`).
- **Lines 680-830** — `gen_action_line` (dispatch-or-log) + `_compose_action` (composite token decomposer).
- **Lines 850-1000** — spec state machine (top-level folder/block → `test.step()`; branch elision when `condition_resolved` is constant).
- **Lines 1000-1200** — JSON manifest serializer + HTML report renderer.
- **Lines 1200-end** — Playwright project generation (page objects, config, package.json, spec write).

## Closing gaps (self-improving loop)

This skill is allowed — and expected — to evolve as new Tosca exports reveal patterns the parser hasn't seen. When `meta.unmapped` is non-empty after running on a new .tsu, treat it as a backlog of small parser fixes:

1. **Inspect what's unmapped.** Each entry has `step`, `element`, `mode`, `property`, `value`, `reason`. Group by reason — repeated patterns are higher value to fix first.
2. **Decide where the gap belongs:**
   - **New value token** (e.g. `{LONGPRESS}`, `{DRAG_TO[X]}`) → extend the `set/optionalSet/input` dispatch in `gen_action_line` or add to `_compose_action`.
   - **New ActionMode** (a Tosca code outside `1/37/69/101/165/517`) → add to `ACTION_MODE` and add a top-level branch in `gen_action_line`.
   - **New ActionProperty** → add to `ASSERT_PROPS` if it's a clean boolean assertion, or extend `verify`/`waitFor` with a value-based handler.
   - **New entity shape** (an Assoc key or ObjectClass the resolver doesn't walk) → extend `resolve_steps` and the relevant index in the entity-loading loop.
   - **New XParam locator hint** → extend `collect_candidates` (slot it into the score table at the right tier).
3. **Implement the smallest change that closes the gap.** Don't speculate beyond what real .tsu files contain.
4. **Update this SKILL.md in the same change.** Add the new token to the "Tosca value tokens" table, the new property to `ASSERT_PROPS`, the new mode to the ActionMode table, etc. The skill is the contract; if the parser knows something the skill doesn't document, future agents will rediscover it the slow way.
5. **Re-run on every reference .tsu in the repo** with `--all --force` and confirm `meta.unmapped` either shrank or stayed empty across all of them. A fix that closes one gap but opens another (regression in another TSU) is not a fix.
6. **Don't change app-specific behaviour into the parser.** If a fix only makes sense for one application's quirks, it belongs in the user's hand-edited spec or `parse_tsu.config.json`, not in the generic parser code.

Modifying the skill itself when you discover something new (a non-obvious .tsu invariant, a confusing edge case, a workflow worth memorialising) is fine and encouraged. Treat the SKILL.md as living docs, not a frozen spec.

## Extending the parser

Adding a new ActionMode:
1. Add to `ACTION_MODE` dict.
2. Add a branch in `gen_action_line()` that returns `(line_str, None)` for handled cases or `todo(reason)` to log.

Adding a new value token (e.g. `{LONGPRESS}`):
1. Add the name to `CLICK_TOKENS` or `KEY_TOKENS` if it fits, OR
2. Add an `if tok == 'LONGPRESS':` branch in the `set/optionalSet/input` block.

Adding a composite pattern (concatenated tokens):
1. Extend the dispatch in `_compose_action()`'s for-loop. Return `('emit', [lines])` for handled, `('todo', reason)` to log, or `None` to bail and let the outer dispatcher handle.

Adding a new ActionProperty (e.g. `Highlighted`):
1. Add to `ASSERT_PROPS` if it has a clean `toBeX()` matcher, OR
2. Add a property-specific branch in the `verify` / `waitFor` blocks.

After any change, re-run with `--all --force` against your reference TSU(s) and check that `meta.unmapped` shrinks.

## Troubleshooting

**"Strict mode violation: 3 elements"** — Locator is too broad. Look at `locator.raw._parent_chain` in the JSON; if there's a custom-tag ancestor, manually scope: `page.locator('parent-tag').locator('...')`. The parser does this automatically when one is available, so this usually means the chain is empty or all standard tags.

**"Locator failed: 0 elements"** — The locator from the .tsu doesn't match the live app (DOM changed since recording). Check `locator.raw._self_healing` for alternates; pick a higher-weight candidate or add to `test_attributes` config if it's a new test-attribute convention.

**`{B[X]}` shows up as TODO** — `X` was never set by a `TBox Set Buffer` step, or it was set from `{PL[X]}` whose layer stack didn't carry it. Check `test_data` in the JSON; if `X` is missing, either the .tsu's preconditions don't define it or the buffer name is misspelled inside the test.

**Branch elision didn't fire (both Then and Else emitted)** — `condition_resolved` is something like `{PL[X]}=="1"` (still has braces). Means the `{PL[X]}` didn't resolve at evaluation time — usually because the `if` is *outside* the reusable block whose layer would carry it, or because the parameter name doesn't match exactly.

**Playwright spec runs sequentially when it should branch** — `--force` regenerates the spec; manual re-emits of a branch will be lost. If you want to keep manual logic, leave the spec without `--force` after first generation.

**Tosca-encrypted password emitted as literal** — Shouldn't happen post-fix; if it does, check the value matches `TOSCA_ENCRYPTED_RE` (UUID prefix + 20+ base64 chars). Manually rename the env var in `safe_env()` if needed.

**An XParam shows up that the parser ignored** — Add it to `META_XPARAMS` if it's framework metadata (parser-irrelevant) or extend `collect_candidates()` if it's a locator hint.

## When to extend the parser vs hand-edit the spec

- **Extend the parser** when the unmapped action represents a Tosca *pattern* you'll see again (a new token, mode, or property). The change benefits every future TSU.
- **Hand-edit the spec** when the issue is app-specific runtime behaviour: a custom Angular component that needs `pressSequentially()` instead of `fill()`, a virtual scroll that needs a wait, an SPA route change that needs `waitForNavigation`. Mark hand-edits clearly so they survive `--force`.
- **Hand-edit the JSON manifest** — never. It's a derived artefact; regenerate from the .tsu.

The parser's job: lossless extraction. The spec generator's job: a mechanical first draft. Polishing the spec for runtime is the user's job (or a downstream agent's, fed by the JSON manifest).
