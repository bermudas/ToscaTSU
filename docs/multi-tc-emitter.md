# Future work: multi-TC emitter

**Status**: parked. Not blocking — the parser handles multi-TC `.tsu` cleanly; the emitter currently assumes single-TC.

## Where things stand today

| Direction | Single-TC `.tsu` | Multi-TC `.tsu` |
|---|---|---|
| **Parser** (`parse_tsu.py`) | ✅ flat layout | ✅ shared playwright project + per-case audit reports |
| **Emitter** (`gen_tsu.py`) | ✅ full round-trip | ❌ assumes one `TestCase` in the base envelope |

A multi-TC `.tsu` round-trip currently fails on the emitter side: `gen_tsu.py:find_test_case()` exits with `error: base/skeleton has N TestCases — use --extend to add alongside`, and `--extend` is itself a stub.

## What "multi-TC emitter support" needs

### Scenario 1 — update one TC inside a multi-TC bundle

User edits one spec under `out/<stem>/playwright-test/tests/<area>/<tc>.spec.ts`. They want a new `.tsu` where the corresponding `TestCase` reflects those edits and the other 9 TCs stay byte-equivalent.

**Design sketch**:
1. CLI: `gen_tsu.py --spec one.spec.ts --pages <shared-pages> --base multi.tsu --tc-id <stem-or-id> --out new.tsu`
2. The emitter reads the base `.tsu`, identifies the target `TestCase` via name match (or `--tc-id` hint).
3. Wipes that TC's subtree (current behavior of `collect_tc_subtree` works for one TC at a time — already correct).
4. Rebuilds from the spec/manifest pair.
5. Other TCs in the base envelope are untouched.

The emitter's `TCBuilder` already supports this — the only missing piece is identifying *which* TC to update when the base has multiple. Add a `--tc-id` selector or auto-match by spec's `test(...)` name vs each TC's `Attributes.Name`.

### Scenario 2 — update multiple specs in one pass

User edits N out of M specs and wants to re-emit the full multi-TC `.tsu` in one command.

**Design sketch**:
1. CLI: `gen_tsu.py --specs-dir <playwright-test/tests> --pages <playwright-test/pages> --base multi.tsu --out new.tsu`
2. Walk every `*.spec.ts` under `--specs-dir`, run `spec_to_manifest` per spec.
3. For each generated manifest, identify its target `TestCase` in the base by name match.
4. Rebuild that TC's subtree; merge into the envelope.
5. TCs that weren't in `--specs-dir` keep their original entities verbatim.

### Scenario 3 — add new TCs to an existing bundle

User has a multi-TC base and wants to add new test cases without touching existing ones.

**Design sketch**: `--add-tc` mode. For each new spec, mint a fresh `TestCase` entity, set up its `ParentFolder` (point at an existing TCFolder or accept `--folder <path>`), build subtree, append to envelope.

## Acceptance criteria for "done"

Validate on `inputs-local/multicasestsu_/10tests.tsu`:

```bash
# Parse → 10 spec files + cases/<tc>/{steps.json, report.html}
python3 parse_tsu.py inputs-local/multicasestsu_/10tests.tsu --all

# (Modify one spec by hand — e.g., change one assertion)

# Re-emit → new .tsu with that one TC updated, other 9 preserved
python3 gen_tsu.py --specs-dir <out>/playwright-test/tests \
                   --pages    <out>/playwright-test/pages \
                   --base     inputs-local/multicasestsu_/10tests.tsu \
                   --out      /tmp/10tests_round.tsu

# Re-parse the regenerated .tsu — should produce the same 10 manifests,
# 9 of which match exactly, 1 reflecting the user's edit
python3 parse_tsu.py /tmp/10tests_round.tsu --all --out-dir /tmp/round_check
diff -r <(ls inputs-local/multicasestsu_/out/10tests/cases) \
        <(ls /tmp/round_check/cases)
# Expect: same case directories, same step counts, same module catalogue
# per case, only the modified TC's manifest differs.
```

## Notes for whoever picks this up

- The **per-case closure** machinery (`tc_closure` in `parse_tsu.py`) is purely a *parser-side* concern — used to filter what each case's report/pages/manifest shows. The emitter doesn't need closures: it operates on the envelope holistically and only modifies the targeted TC's subtree.
- The **`@tosca` markers** in each spec (folder/block/step/buffers/if/then/else/loop/wait) are already round-trip-safe per single-TC. They work the same for multi-TC; nothing extra needed there.
- The main code change is a small one in `gen_tsu.py:emit()`:
  - Replace `tc = find_test_case(ents)` with logic that handles 1+ TCs.
  - Accept either a single `--spec` (with optional `--tc-id`) or `--specs-dir` (process all).
  - For each (spec, target-TC) pair, the existing `TCBuilder` flow runs unchanged; just the wiring around it is new.
- Estimated effort: ~half a day. The hard work — closure walking, surrogate management, RTB transitive deps, marker handling — is already in place from the parser-side multi-TC support.

## When to revive this

When the user has an actual multi-TC editing workflow they want to round-trip. Until then, the parser produces clean per-case isolated outputs and that covers the common "convert Tosca tests to Playwright and stabilize" workflow. The emitter remains useful for single-TC `.tsu` round-trips and one-test-at-a-time scenarios (split a multi-TC `.tsu` into per-case stubs first if needed).
