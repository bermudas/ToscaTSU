---
name: tosca-tsu-emitter
description: 'Emit Tricentis Tosca .tsu test-case bundles from Playwright .spec.ts files plus a base or skeleton .tsu envelope (Tosca-importable gzipped entity graphs). Inverse of parse_tsu.py. Use whenever the user mentions gen_tsu.py, spec_to_manifest.py, generating .tsu from Playwright code, updating an existing Tosca test case, adding a new test that references existing modules, building a fresh test from scratch, or round-tripping Playwright back into Tosca. Three modes (update / reference-existing / scratch+skeleton), JSON manifest as the canonical interchange (same shape parse_tsu emits), structured @tosca markers that round-trip cleanly, surrogate-reuse strategy that keeps Tosca-internal IDs stable, decorator-skipping in spec_to_manifest so .first / .filter / .catch and per-call timeout options never confuse reverse parsing. Self-improving — extend the script and this skill together when new patterns surface.'
---

# Tosca .tsu Emitter

The scripts live at `scripts/gen_tsu.py` and `scripts/spec_to_manifest.py` inside this skill. In the original repo they also exist at the project root for direct CLI use; the bundled copies are for portability when the skill is installed elsewhere. **All copies are byte-identical** — point users to whichever location is convenient.

`gen_tsu.py` is the inverse of `parse_tsu.py`. Where the parser decodes a .tsu down to a JSON manifest, the emitter builds a .tsu up from a manifest (or directly from a Playwright `.spec.ts`). The pipeline:

```
spec.ts + page-objects + base/skeleton.tsu
       ↓ Phase A — spec_to_manifest.py (tree-sitter AST + @tosca markers)
   manifest (parse_tsu schema)
       ↓ Phase B — gen_tsu.py (envelope graft + TC subtree rebuild)
   importable .tsu
```

The **manifest is the canonical interchange** — same shape `parse_tsu` emits.
Phase A is replaceable (could come from a manifest editor, a UI, an LLM); Phase
B is the deterministic half.

> **Companion skill: `tosca-tsu-parser`** covers the forward direction (.tsu →
> spec) plus the **stabilization loop** for hardening auto-generated specs
> against the live app via browser MCP / Playwright codegen. The typical
> end-to-end flow is: parse → stabilize against live → (optionally) round-trip
> back to .tsu via this emitter for re-import into Tosca.

## Operating modes

```bash
# 1. Update an existing test case + its module(s):
python3 gen_tsu.py --spec test.spec.ts --pages playwright-test/pages \
                   --base existing.tsu --out updated.tsu

# 2. New TC referencing existing modules (skeleton supplies envelope):
python3 gen_tsu.py --spec test.spec.ts --pages playwright-test/pages \
                   --skeleton project_skeleton.tsu --out new.tsu

# 3. From a JSON manifest directly (skips Phase A):
python3 gen_tsu.py --manifest steps.json --base existing.tsu --out updated.tsu
```

## What's preserved verbatim (the "envelope")

- `TCProject`, `TCFolder`, `TCComponentFolder` — workspace structure
- `XModule` catalog (with `TCProperties` Tricentis-internal blobs)
- `XModuleAttribute` + their `XParam` locator hints
- `ReuseableTestStepBlock` (RTB) bodies — full sub-graph
- `ParameterLayer`, `Parameter` — RTB parameter definitions
- `TestStepLibrary` — the shared-block library
- `OwnedFile`, `FileContent` — attached screenshots
- Encrypted password values (Tosca-internal cipher, can't be re-encrypted)

## What's rebuilt from manifest (the "TC subtree")

- `TestCase` — Attributes refreshed from manifest meta
- `TestStepFolder` — Tosca folders inside the test
- `TestStepFolderReference` — call sites for RTBs (carries `ParameterLayerReference`)
- `XTestStep` — leaf test steps (linked to `XModule` via `Assocs.Module`)
- `XTestStepValue` — actions (linked to `XModuleAttribute` via `Assocs.ModuleAttribute`)
- `TestCaseControlFlowItem` + `TestCaseControlFlowFolder` — if/then/else/loop blocks
- `ParameterLayerReference` + `ParameterReference` — per-call parameter wiring

## Three scenarios in detail

### 1. Update test case + module
Both spec.ts and one or more page-object files have changed. The base .tsu
provides the envelope; gen_tsu replaces the TC subtree and patches XParam
values on existing XModuleAttributes when locators shifted. New attributes
mint new `XModuleAttribute` + `XParam` entities under the existing XModule.

### 2. Reference existing module
Spec.ts is brand-new; the modules it uses already exist. Same flow as (1)
with `--skeleton` instead of `--base`; the skeleton provides the envelope
and the existing module catalog.

### 3. Scratch + skeleton
Both spec and module are new to Tosca. The skeleton .tsu must be exported
once from your Tosca project — a near-empty test that captures the project
metadata Tosca expects (TCProject Revision, the Library structure, etc.).
gen_tsu mints fresh XModules + XModuleAttributes from the page-object
catalog; it derives module names from page-object class names
(`LoginPagePage` → "Login Page") and attribute names from snake_case fields
(`email_address` → "email address"). Hand-rename in Tosca after import if
the auto-derived names don't match your team's conventions.

## Structured `// @tosca …` markers (the round-trip contract)

`parse_tsu.py` emits machine-readable markers in the spec it generates so
the emitter can faithfully reconstruct structure. These are **the source of
truth** for the spec parser; everything else (`// ── X` decoration, step
name comments, `[if Cond]`) is fallback for hand-written specs.

| Marker | Emitted at | Carries |
| --- | --- | --- |
| `// @tosca folder: "Name"` | TestStepFolder open | folder name |
| `// @tosca /folder` | folder close | — |
| `// @tosca block: "Name"` | TestStepFolderReference open (RTB call) | RTB name |
| `// @tosca /block` | RTB call close | — |
| `// @tosca step: "Name" module="ModuleName"` | XTestStep open (one per step) | step + module |
| `// @tosca wait: 3000 name="Wait"` | TBox Wait step | duration ms + step name |
| `// @tosca buffers: A=val1; B=val2` | TBox Set Buffer step | buffer name=value pairs |
| `// @tosca if: "Name" cond="Expr" verdict=True` | TestCaseControlFlowItem open | name, condition, optional verdict |
| `// @tosca /if` | if close | — |
| `// @tosca then` / `else` / `loop` | branch open | — |
| `// @tosca /branch` | branch close | — |

Quoting rules: `"` is escaped as `\"`, `\` as `\\`, newlines stripped (the
markers are single-line). The reverse parser unquotes via `_unquote()`.

## Spec-side decorators the reverse parser ignores

`parse_tsu.py` emits several runtime-friendly chain decorators that have **no
semantic effect on the manifest** but would confuse a naïve reverse walker.
`spec_to_manifest.py` strips these explicitly:

| Decorator | Where it appears | What it means |
| --- | --- | --- |
| `.first()` | After `.or()` chains, after standalone locators in existence asserts | Strict-mode safety; pick first match |
| `.filter({...})` | After locator chains | Constrain matched set (e.g. `hasText`) |
| `.nth(N)` | After locator chains | Pick the Nth match |
| `.catch(() => {})` | After action calls | Tolerant action (banner-may-not-appear pattern) |
| Action options like `{ timeout: 3000 }` | Inside action args | Per-call timeout / strict / etc. |
| `test.use({ baseURL: ... })` | Top-level once per spec | Per-spec baseURL pin |

Round-trip safety: a spec generated by `parse_tsu.py` with these decorators in
place re-parses to a manifest **byte-for-byte equivalent** to the manifest of
the original `.tsu`. If you add NEW decorator types in `parse_tsu.py`, mirror
them in `spec_to_manifest.CHAIN_DECORATORS_*` constants and
`_strip_action_options` to keep round-trip tight.

## Manifest schema (interchange contract)

Identical to what `parse_tsu.py --steps-json` produces. Top-level shape:

```json
{
  "meta": {"test_name": "...", "project": "...", "base_url": "...",
           "config": {...}, "unmapped": [...]},
  "test_data": {"BufferA": "value", ...},
  "steps": [
    {"type": "folder", "name": "Preconditions", "depth": 0},
    {"type": "block_start", "name": "Login...", "parameters": {...}},
    {"type": "step", "name": "Click Submit", "module": "Cart Page",
     "actions": [{"mode": "set", "element": "Submit btn",
                  "explicit_name": "ProductNameTinted",
                  "value": "{Click}",
                  "locator": {"primary": "page.locator(...)",
                              "fallback": "...",
                              "raw": {"Tag": "BUTTON", "Id": "...",
                                       "attributes_data-test-id": "..."}}}]},
    {"type": "if", "name": "ContinueOrCheckout",
     "condition": "{PL[X]}==\"1\"", "condition_resolved": "1==\"1\""},
    {"type": "then_start"}, {"type": "step", ...}, {"type": "then_end"},
    {"type": "if_end"},
    {"type": "folder_end"}
  ]
}
```

Key fields the emitter reads:
- `meta.test_name` → `TestCase.Attributes.Name`
- `test_data` → re-emitted as XTestStepValues under TBox Set Buffer steps
  (synthesized when missing)
- `steps[].type` → entity class (folder/block/step/if/then_start/…)
- `steps[].module` → resolved against base envelope's XModule catalog by name
- `steps[].actions[].element` → resolved against XModuleAttribute by name
- `steps[].actions[].explicit_name` → `XTestStepValue.ExplicitName` (used for
  buffer keys; only set when the manifest carries a non-empty value)
- `steps[].actions[].mode` → `ActionMode` code (set=37, verify=69, …)
- `steps[].actions[].value` → `XTestStepValue.Value` (literal or `{TOKEN}`)
- `steps[].actions[].locator.raw` → XParam values for new XModuleAttributes
- `steps[].condition` → propagated to TBox Evaluation Tool inside `Condition` folder

## Architecture pointers (gen_tsu.py)

Approximate line ranges; refresh when extending.

- **DEFAULTS / ASSOC_KEYS** — Tosca attribute and assoc defaults per ObjectClass
- **`SurrogateMinter`** — deterministic UUID synthesis (sha1 of test name + path)
- **`find_test_case`, `collect_tc_subtree`** — envelope/TC partition
- **`build_module_catalog`, `build_rtb_catalog`** — name→surrogate lookup
- **`locator_raw_to_xparams`** — manifest locator hints → XParam tuples
- **`mk_entity`** — entity factory with class defaults
- **`TCBuilder`** — state machine over the manifest stream:
  - `_push` / `_pop` — folder/block stack
  - `block_depth` — suppresses RTB-body unfurling (parse_tsu inlines those)
  - `on_folder` / `on_folder_end` — TestStepFolder open/close
  - `on_block_start` / `on_block_end` — TestStepFolderReference + ParameterLayerReference
  - `on_step` → `_emit_action` — XTestStep + XTestStepValue
  - `on_if` / `on_then_start` / `on_else_start` / `on_loop_start` / `on_if_end` —
    control flow tree
  - `_mint_module` / `_mint_attribute` — fresh module/attribute creation
- **`emit`** — top-level orchestration (load envelope → wipe TC subtree →
  rebuild via TCBuilder → re-merge → gzip-write)

## Architecture pointers (spec_to_manifest.py)

- **`parse_pages_dir`** — reads `pages/*.page.ts`, builds `(catalog, rev_index)`.
  Catalog is per-class fields; rev_index maps `normalized_locator_text → (class, field)`.
- **`SpecToManifest`** — tree-sitter walk of the spec:
  - `_walk_stream` collects (comment | await | test.step | test) in document order
  - `_on_marker` — primary path; consumes `@tosca …` markers
  - `_on_legacy` — fallback path for hand-written specs without markers
  - `_on_await` → `_handle_expect` for `expect(...).toX()` / direct path for
    `await page.X.Y(...)`
  - `_call_chain` — left-to-right call-chain decomposition
    (`page.locator(X).or(page.locator(Y)).fill('v')` → `[(page,_), (locator,X), (or,Y), (fill,'v')]`)
  - `_resolve_locator` — reverse-lookup against page-object catalog
  - `_emit_or_extend_step` / `_flush_step` — accumulate actions per logical step

## Validating a generated .tsu

The fastest validation is round-trip: parse the generated .tsu and check
`meta.unmapped` is empty, plus the structural counts match the source manifest.

```bash
# 1. Generate
python3 gen_tsu.py --spec test.spec.ts --pages playwright-test/pages \
                   --base original.tsu --out generated.tsu

# 2. Round-trip — should re-parse cleanly
python3 parse_tsu.py generated.tsu --steps-json --force

# 3. Diff manifests
diff <(jq -S 'del(.meta.surrogate, .meta.source_file, .meta.revision)' \
       original_steps.json) \
     <(jq -S 'del(.meta.surrogate, .meta.source_file, .meta.revision)' \
       generated_steps.json)
```

For real Tosca import validation, drop the .tsu into Tosca via TC import and
report any rejection messages. Common ones (and fixes):
- "Module attribute X not found" — page-object field maps to a name not in
  the base envelope's XModule. Either add the attribute in Tosca first, or
  use `--skeleton` so the emitter mints fresh `XModuleAttribute` entities.
- "Surrogate Y already exists" — `SurrogateMinter` collision (rare; sha1
  prefix collision). Re-run; minter has a per-call counter to avoid this.
- "ParameterLayer Z missing" — an RTB call referenced in the spec doesn't
  match any RTB in the envelope. Check `// @tosca block: "X"` markers — the
  name must match the `ReuseableTestStepBlock.Name` in the base.

## Common workflows

**Round-trip an auto-generated spec back to its origin .tsu:**
```bash
python3 gen_tsu.py --spec generated.spec.ts --pages pages \
                   --base origin.tsu --out roundtrip.tsu
diff <(python3 parse_tsu.py origin.tsu --steps-json && cat *_steps.json) \
     <(python3 parse_tsu.py roundtrip.tsu --steps-json --force && cat *_steps.json)
```

**Add a new step to a test:**
1. Edit the spec.ts — insert `await page.locator(...).click();` and the
   `// @tosca step: "..."` marker before it.
2. `python3 gen_tsu.py --spec test.spec.ts --pages pages --base orig.tsu --out new.tsu`
3. Import `new.tsu` into Tosca; the new XTestStep + XTestStepValue appear in
   the right folder, all envelope entities preserved.

**Use a manifest as the input (skip Phase A):**
Useful when an upstream tool (manifest editor, agent) produces structured
JSON and you just want the .tsu out:
```bash
python3 gen_tsu.py --manifest custom_steps.json --base orig.tsu --out new.tsu
```

## Closing gaps (self-improving loop)

When emission fails or Tosca import rejects an output, treat it as a backlog
of small fixes:

1. **For unmapped step kinds** (the manifest stream has a `type` gen_tsu
   doesn't recognise) — add the handler in `TCBuilder.on_<type>` and the
   matching marker pattern in `spec_to_manifest.TOSCA_MARKERS`.
2. **For new ActionMode values** — add to `ACTION_MODE_BY_KEY`. Every mode
   key in this dict must round-trip through both parse_tsu's `ACTION_MODE`
   and our reverse mapping.
3. **For locator hints the emitter doesn't translate** — extend
   `LOCATOR_RAW_TO_XPARAM` (manifest key → XParam Name). The fallback
   passes through unknown keys verbatim (treated as XParam Name=value).
4. **For Tosca rejections on import** — usually a missing default Attribute
   or Assoc list. Add the field to `DEFAULTS[ClassName]` or
   `ASSOC_KEYS[ClassName]`; re-test on a known-good base .tsu.
5. **For new spec idioms** — tree-sitter walks AST nodes, so adding a new
   construct usually means a new branch in `_on_await` /
   `_handle_expect` plus a regex in `PLAYWRIGHT_ACTIONS` or
   `EXPECT_MATCHERS`.
6. **Update this SKILL.md in the same change.** The skill is the contract;
   if gen_tsu emits something the skill doesn't document, future agents
   rediscover it the slow way.

## When to extend the emitter vs hand-edit the .tsu

- **Extend** when the gap is a *pattern* (a Playwright idiom, a Tosca
  ObjectClass shape, a marker that should round-trip). The change benefits
  every future spec.
- **Hand-edit a manifest** is fine for one-off content fixes; the emitter
  treats the manifest as authoritative. Do NOT hand-edit the .tsu — it's
  the derived artefact; regenerate from the manifest or spec.

## Known limitations (v1)

- **Nested `XTestStepValue.SubValues` trees collapse to flat SVs.** The
  parser walks SubValues recursively and emits one action per leaf with
  parent-chain locator scoping. The emitter currently mints flat SVs that
  point directly at the leaf attribute. Locators still work (parser regenerates
  them on re-read), but the SubValues topology differs. Affects ~2 actions
  in `complexTest1.tsu`; semantically equivalent.
- **Editing inside an RTB** is not supported. RTBs are envelope content; if
  you change a step inside an RTB body the spec, the emitter ignores it
  (suppressed by `block_depth>0`). Edit the RTB in Tosca itself, re-export
  the .tsu, and use as new base.
- **Encrypted password re-encryption is impossible** (Tosca-internal cipher).
  Passwords come from the base .tsu verbatim; if a spec references a fresh
  `process.env.NEW_PWD`, gen_tsu emits it as `{CP[NEW_PWD]}` which Tosca
  treats as a config-parameter reference.
- **From-scratch (`--skeleton` only) requires a real Tosca-exported skeleton**
  containing TCProject metadata, the Library, and at least one stub
  XModule. Pure-synthetic envelopes are likely to fail Tosca import on
  internal blob fields like `XModule.TCProperties`.
- **Hand-written specs without `@tosca` markers** are partially supported via
  `LEGACY_PATTERNS` (heuristic comment parsing + POM lookup). Full fidelity
  requires the markers.

## Test attribute config

`gen_tsu.py` reads `parse_tsu.config.json` (sibling of the .tsu) for the
`test_attributes` priority list — same config file as the parser. New
`XModuleAttribute` minted by gen_tsu use this list to decide which XParam
name to give the locator hint. Default order:

```json
{
  "test_attributes": ["data-test-id", "data-testid", "data-test", "data-cy", "data-qa"]
}
```

If your team uses something else, drop a config file and both directions
pick it up.
