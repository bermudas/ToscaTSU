# Tosca ↔ Playwright

Bidirectional bridge between **Tricentis Tosca** test bundles (`.tsu`) and **Playwright** TypeScript specs.

```
.tsu  ──parse_tsu.py──►  manifest + HTML report + Playwright project
                                │
                                ├─ run, debug, stabilize against the live app
                                │
.tsu  ◄──gen_tsu.py──── (optional) round-trip back for Tosca re-import
```

- **`parse_tsu.py`** — decodes a gzipped Tosca `.tsu` into a JSON manifest, an interactive HTML locator-quality report, and a runnable Playwright project (page objects + spec + `.env.example`).
- **`gen_tsu.py`** — the inverse. Takes a Playwright `.spec.ts` (plus a base/skeleton `.tsu` envelope) and emits an importable `.tsu`. Three operating modes: update an existing test case, add a new test that references existing modules, or build from scratch on a project skeleton.
- **`spec_to_manifest.py`** — companion to `gen_tsu.py`; walks a `.spec.ts` AST via tree-sitter and produces the manifest JSON the emitter consumes.

## Requirements

- **Python 3.8+** — no external dependencies for `parse_tsu.py` (stdlib only)
- **Node.js 18+ and npm** — for running the generated Playwright tests
- **(emitter only)** `tree_sitter` + `tree-sitter-typescript` Python packages — only needed if you use `gen_tsu.py --spec` (i.e. spec → .tsu direction). Install with `pip install tree_sitter tree-sitter-typescript`.

## Install

Pick the path that matches what you want:

### A. Use the CLI tools directly (no Claude Code needed)

```bash
git clone <this-repo> ToscaTSU
cd ToscaTSU

# (Optional) Install emitter dependencies if you'll use the spec → .tsu direction:
pip3 install --user tree_sitter tree-sitter-typescript
```

`parse_tsu.py` runs straight from the clone with stdlib only. Add the repo to your `PATH` or alias it if you'll invoke from elsewhere.

### B. Install the Claude Code skills into a target repo (one-liner from GitHub)

No clone required — pipe the install script through `bash`:

```bash
# Install into the current directory's .claude/skills/:
curl -fsSL https://raw.githubusercontent.com/bermudas/ToscaTSU/main/install-from-github.sh | bash

# Or specify a target:
curl -fsSL https://raw.githubusercontent.com/bermudas/ToscaTSU/main/install-from-github.sh | bash -s -- /path/to/your-project

# Then in your target project, restart Claude Code or run /reload-plugins
```

The script downloads the two `.skill` zips from this repo's `dist/` (raw GitHub URLs), unzips them into `<target>/.claude/skills/`, and prints the next-step. Each skill bundle includes its own scripts (`parse_tsu.py` etc.) under `scripts/`, so the target repo doesn't need to clone anything.

Override the source if you forked: `REPO=myfork/ToscaTSU BRANCH=mybranch bash install-from-github.sh /path/to/target`.

If you've already cloned the source locally, you can also run the bundled `./install.sh /path/to/target` instead — same outcome, no network call.

By hand, if you prefer:

```bash
mkdir -p /target/.claude/skills
curl -fsSL https://github.com/bermudas/ToscaTSU/raw/main/dist/tosca-tsu-parser.skill  -o /tmp/p.skill && unzip -q -o /tmp/p.skill -d /target/.claude/skills/
curl -fsSL https://github.com/bermudas/ToscaTSU/raw/main/dist/tosca-tsu-emitter.skill -o /tmp/e.skill && unzip -q -o /tmp/e.skill -d /target/.claude/skills/
```

### C. Install the skills user-globally (work in every repo)

Same as B but pass `~` as the target — skills land in `~/.claude/skills/` and are visible to every Claude Code session.

```bash
curl -fsSL https://raw.githubusercontent.com/bermudas/ToscaTSU/main/install-from-github.sh | bash -s -- ~
```

### D. Develop / contribute back

Clone, edit the canonical scripts at the repo root, then re-build the skill bundles:

```bash
./package_skills.sh   # syncs scripts → skill bundles → re-packages dist/*.skill
```

See **Development** below for full details.

## Where to put your .tsu files

Drop your Tosca exports into `inputs-local/` (gitignored — your inputs and any per-run artifacts stay out of commits). Examples in this README assume that layout, but any path works.

```
inputs-local/
├── MyTest.tsu
└── out/MyTest/         ← parser writes everything here
    ├── MyTest_steps.json
    ├── MyTest_report.html
    └── playwright-test/
```

## Quick start: .tsu → runnable Playwright test

```bash
# 1. Generate everything from a .tsu
python3 parse_tsu.py inputs-local/MyTest.tsu --all

# Outputs land under inputs-local/out/MyTest/ by default:
#   MyTest_steps.json     ← canonical JSON manifest
#   MyTest_report.html    ← interactive locator-quality report (open in browser)
#   playwright-test/      ← runnable TypeScript project
#       playwright.config.ts
#       package.json
#       .env.example      ← lists every env var the spec reads, with Tosca CP[*] traceability
#       pages/*.page.ts   ← one page object per Tosca XModule
#       tests/<stem>.spec.ts ← the test (with structured @tosca markers for round-trip)
#
# Override location with --out-dir <path> if you want everything elsewhere.

# 2. Set up env + install Playwright
cd inputs-local/out/MyTest/playwright-test
cp .env.example .env
$EDITOR .env                   # fill in the listed credentials / config values
npm install
npx playwright install chromium

# 3. Run
npx playwright test --workers=1 --reporter=list

# Open the HTML run report
npx playwright show-report
```

### CLI flags for `parse_tsu.py`

```bash
python3 parse_tsu.py file.tsu                            # HTML report only
python3 parse_tsu.py file.tsu --steps-json               # + JSON manifest
python3 parse_tsu.py file.tsu --playwright               # + Playwright project
python3 parse_tsu.py file.tsu --all                      # all three
python3 parse_tsu.py file.tsu --all --force              # also overwrite hand-edited spec/config
python3 parse_tsu.py file.tsu --all --out-dir <path>     # custom output location
```

`--force` re-overwrites the spec and `playwright.config.ts`. Without it, hand-edits are preserved across re-runs (page objects always regenerate — they're a derived catalog). `--out-dir` lets you point everything at a single shared directory or bypass the per-stem nesting.

## Quick start: Playwright spec → .tsu

Pick the right paths for your layout (these examples assume the parser-default of `inputs-local/out/<stem>/...`):

```bash
# Update an existing Tosca test (preserves the envelope + module catalog):
python3 gen_tsu.py --spec inputs-local/out/MyTest/playwright-test/tests/MyTest.spec.ts \
                   --pages inputs-local/out/MyTest/playwright-test/pages \
                   --base inputs-local/MyTest.tsu \
                   --out inputs-local/out/MyTest/MyTest_updated.tsu

# Or feed a manifest directly (skips spec parsing — useful when you have JSON):
python3 gen_tsu.py --manifest inputs-local/out/MyTest/MyTest_steps.json \
                   --base inputs-local/MyTest.tsu \
                   --out inputs-local/out/MyTest/MyTest_updated.tsu

# Add a new test referencing an existing module catalog:
python3 gen_tsu.py --spec new_test.spec.ts --pages playwright-test/pages \
                   --skeleton project_skeleton.tsu --out new.tsu
```

The skeleton `.tsu` is a near-empty test you export once from your Tosca project — it captures the project envelope (TCProject, Library, ParameterLayers) the emitter can't synthesize from scratch. Keep it around as a template.

## The stabilization loop (real-world auto-gen workflow)

The auto-generated spec is a **starting point**, not a finished test. The recorded Tosca .tsu was authored against a specific snapshot of the app — DOM, locale, auth state, dynamic IDs all drift over time. Plan for iteration:

1. **Read the HTML report** (`<stem>_report.html`) before running. Red badges flag fragile locators (dynamic XPath, Angular auto-classes, generic class-only) — those are the predicted breakage zones.
2. **First run** the generated spec. Most failures fall into a few categories:
   - **Strict-mode collision** (`resolved to N elements`) — locator too broad
   - **Element not found** — DOM drifted (renamed, restructured, removed)
   - **OAuth/auth bounce** — session state changed since recording
   - **Localization mismatch** — recorded English text vs live Swedish (or vice versa)
3. **Use a browser tool to inspect the failure point in the live app**:
   - **Playwright MCP** (`@playwright/mcp`) — `browser_navigate` then `browser_snapshot` to grab the current DOM and pick a tighter selector
   - **`npx playwright codegen <URL>`** — record fresh selectors against the live app
   - **`npx playwright test --ui`** — step through interactively
4. **Patch the spec, not the .tsu.** Hand-edits survive re-runs unless you pass `--force`. The .tsu is the historical Tosca recording; the spec is your living contract with the current app.
5. **When stable**, optionally round-trip the patched spec back to `.tsu` via `gen_tsu.py` for re-import into Tosca.

If the same kind of fix keeps appearing, fold it into `parse_tsu.py` so future generations include it automatically — the script and skills are designed to be **self-improving**.

## Outputs at a glance

### HTML report (`<stem>_report.html`)
Static file, opens in any browser. Tabs:
- **Locator Challenges** — risk-rated table of fragile locators
- **Execution Flow** — full step tree with computed Playwright locators
- **Module Catalogue** — every Tosca module, its UI elements, raw locator properties, weighted self-healing candidates
- **Meta** — test parameters, all observed URLs, unmapped actions

### JSON manifest (`<stem>_steps.json`)
Canonical interchange format. Same shape produced by parsers, consumed by emitters. Top-level keys: `meta` (test_name, project, base_url, unmapped[]), `test_data` (Tosca buffer values), `steps[]` (linear stream of `folder` / `block_start` / `step` / `if` / branch nodes).

### Playwright project (`playwright-test/`)
Self-contained npm project. Each spec pins its recorded baseURL via `test.use({ baseURL: process.env.BASE_URL || '<recorded URL>' })`, so multiple specs from different .tsu files coexist without conflicting global config. `.env.example` enumerates every env var the spec reads, with traceback to the original Tosca `CP[*]` source — e.g.:

```
ALCROB2BURL=     (from CP[AlcroB2BURL] / Url)
USERPWD=         (from CP[UserPWD] / Password)
EMAIL_ADDRESS_USERNAME= (from CP[UserName] / Email Address)
```

## Optional: `parse_tsu.config.json`

Place next to the .tsu to override defaults:

```json
{
  "test_attributes": ["data-test-id", "data-testid", "data-test", "data-cy", "data-qa"],
  "skip_modules":    ["TBox Set Buffer", "TBox Window Operation", "TBox Start Program",
                      "TBox Evaluation Tool", "TBox Dialog", "TBox Buffer"]
}
```

The first matching `test_attribute` wins highest locator priority. Defaults are sensible for most apps — only set this if your test attribute is named something unusual.

## Claude Code integration

Four skills ship in `.claude/skills/`:

- **`tosca-tsu-parser`** — invoked when working on `.tsu` files, the parser, or converting Tosca → Playwright
- **`tosca-tsu-emitter`** — invoked when generating .tsu from Playwright code, updating Tosca tests, or round-tripping
- **`playwright-testing`** ([sourced from arozumenko/sdlc-skills](https://github.com/arozumenko/sdlc-skills/tree/main/skills/playwright-testing)) — UI/E2E test automation via Playwright MCP. Auto-triggers on prompts about testing the UI, browser tests, taking screenshots, or writing E2E tests
- **`browser-verify`** ([same source](https://github.com/arozumenko/sdlc-skills/tree/main/skills/browser-verify)) — direct Chrome DevTools Protocol automation for arbitrary JS execution, cookie/localStorage inspection, computed-style checks, and real mouse/keyboard events; zero npm dependencies

The first two are also packaged as `.skill` files in `dist/`. The two external skills carry their own `setup.yaml` and bundled scripts; they install in any Claude Code project that has these `.claude/skills/` folders present.

## MCP server setup (Playwright browser MCP)

The repo ships ready-to-use MCP server config for the **Playwright browser MCP** (`@playwright/mcp`) in four formats so it works across the major AI-coding tools without setup:

| Tool | File | Format details |
|---|---|---|
| **Claude Code** | `.mcp.json` | `{"mcpServers": {...}}` ([reference](https://docs.anthropic.com/claude-code/mcp)) |
| **VS Code** | `.vscode/mcp.json` | `{"servers": {...}}` ([reference](https://code.visualstudio.com/docs/copilot/customization/mcp-servers)) |
| **GitHub Copilot in IDE** | `.github/mcp.json` | `{"servers": {...}}` (project-shared, version-controlled) |
| **GitHub Copilot CLI** | `.copilot/mcp-config.json` | template — copy to `~/.copilot/mcp-config.json` ([reference](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)) |

All four point at the same server: `npx @playwright/mcp@latest`. The Playwright MCP gives the AI tools `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, etc. — perfect for the **stabilization loop** described above where you debug auto-generated specs against the live app.

Restart your tool (or `/reload-plugins` in Claude Code) after first install so the MCP server is picked up.

## How a `.tsu` is structured (brief)

A `.tsu` is gzip-compressed JSON: `{"Entities": [...]}`. Each entity has `ObjectClass`, `Surrogate` (UUID), `Attributes`, `Assocs` (refs to other surrogates).

```
TestCase
  └── Items → TestStepFolder              (Precondition / Process / Postcondition)
                └── Items → XTestStep
                              ├── Module → XModule (the page-object catalog)
                              └── TestStepValues → XTestStepValue
                                    ├── ActionMode  (37=Set, 69=Verify, 1=Input, 101=WaitFor, …)
                                    ├── Value       (literal or {TOKEN} or {PL[X]} / {B[X]} / {CP[X]} / {XL[X]})
                                    └── ModuleAttribute → XModuleAttribute
                                          └── Properties → XParam   (Tag, Id, ClassName, XPath, attributes_*, SelfHealingData, …)
```

`TestStepFolderReference` entities point to `ReuseableTestStepBlock` entities (Tosca's shared blocks); `parse_tsu.py` walks them inline. `ParameterLayer` carries per-call parameter wiring. Tosca's encryption (passwords) is workspace-bound symmetric — the parser detects encrypted blobs and emits `process.env.X` placeholders for users to fill from real values.

For the deeper entity model and the manifest schema, see `.claude/skills/tosca-tsu-parser/SKILL.md`.

## Development

### Source layout

The Python scripts at the repo root (`parse_tsu.py`, `gen_tsu.py`, `spec_to_manifest.py`) are **the canonical source**. The `.claude/skills/*/scripts/` copies and the `dist/*.skill` files are **build artifacts** — generated from the root scripts. Don't edit the bundled copies directly; edit the root and re-build:

```bash
./package_skills.sh   # syncs canonical scripts → skill bundles → re-packages dist/*.skill
```

This sync-and-package step is fast (sub-second) and keeps the three locations from drifting. The `.skill` files in `dist/` are the artefact you'd share if someone wants to install just the Claude Code skill in another repo.

### Round-trip validation

```bash
# Confirms parser + emitter agree end-to-end:
python3 parse_tsu.py inputs-local/reference.tsu --steps-json --force
python3 gen_tsu.py --manifest inputs-local/out/reference/reference_steps.json \
                   --base inputs-local/reference.tsu \
                   --out inputs-local/out/reference/reference_round.tsu
python3 parse_tsu.py inputs-local/out/reference/reference_round.tsu --steps-json --force
diff <(jq -S 'del(.meta.surrogate, .meta.source_file, .meta.revision)' \
        inputs-local/out/reference/reference_steps.json) \
     <(jq -S 'del(.meta.surrogate, .meta.source_file, .meta.revision)' \
        inputs-local/out/reference_round/reference_round_steps.json)
```

A clean round-trip is the canonical correctness check for either tool.

## License

See repo. Project layout, scripts, and skills are open for use, modification, and extension. Tricentis Tosca is a trademark of Tricentis GmbH; this project is independent.
