#!/usr/bin/env python3
"""
gen_tsu.py — Playwright/manifest → Tosca .tsu (Tosca-importable).

Inverse of parse_tsu.py. Three working modes:

    # Update an existing test case + its module(s):
    python3 gen_tsu.py --manifest steps.json --base existing.tsu --out new.tsu

    # New TC referencing existing modules (skeleton supplies envelope):
    python3 gen_tsu.py --manifest steps.json --skeleton project_skeleton.tsu --out new.tsu

    # Add new TC alongside the existing one (extend mode):
    python3 gen_tsu.py --manifest steps.json --base existing.tsu --extend --out new.tsu

    # From .spec.ts (Phase B — uses tree-sitter to AST-parse the spec):
    python3 gen_tsu.py --spec test.spec.ts --pages playwright-test/pages \
                       --base existing.tsu --out new.tsu

Concepts:
    Envelope = TCProject, TCFolder, TCComponentFolder, XModule catalog,
               XModuleAttribute, XParam, ReuseableTestStepBlock, ParameterLayer,
               Parameter, TestStepLibrary, OwnedFile, FileContent.
    TC subtree = TestCase + its TestStepFolders, TestStepFolderReferences,
                 XTestSteps, XTestStepValues, TestCaseControlFlowItems,
                 TestCaseControlFlowFolders, ParameterLayerReferences,
                 ParameterReferences.

The emitter rebuilds the TC subtree from the manifest. It preserves
envelope entities verbatim — XModule.TCProperties blobs, encrypted
passwords, server-side metadata are pass-through.

When the manifest references a module/attribute name not in base/skeleton,
the emitter mints a fresh XModule + XModuleAttribute + XParam tree. When
the locator differs, it updates the matching XParam values.

Validation:
    python3 gen_tsu.py --manifest complexTest1_steps.json \
                       --base complexTest1.tsu --out complexTest1_round.tsu
    python3 parse_tsu.py complexTest1_round.tsu --steps-json
    diff <(jq -S . complexTest1_steps.json) \
         <(jq -S . complexTest1_round_steps.json)
"""

import argparse, gzip, json, sys, re, uuid, hashlib, copy
from pathlib import Path
from collections import defaultdict, OrderedDict


# ──────────────────────────────────────────────────────────────────────────────
# Tosca constants — kept aligned with parse_tsu.py
# ──────────────────────────────────────────────────────────────────────────────
ACTION_MODE_BY_KEY = {'input':'1', 'set':'37', 'verify':'69', 'waitFor':'101',
                      'bufferRead':'165', 'optionalSet':'517'}

# Default Attributes Tosca expects on each ObjectClass (when minting fresh).
# Values are the empty/zero defaults observed across multiple .tsu samples.
DEFAULTS = {
    'TestCase': {
        'CheckoutWorkspace': '', 'SynchronizationPolicy': '2', 'Description': '',
        'CheckOutState': '0', 'Revision': '0', 'IncludeForSynchronization': '1',
        'Pausable': '1', 'TestCaseWorkState': '2', 'IsBusinessTestCase': '0',
        'DerivedFromName': '', 'TestConfigurationParameters': '',
    },
    'TestStepFolder': {
        'Condition': '', 'Path': '', 'BreakInstantiation': '0',
        'Repetition': '', 'DisabledDescription': '', 'Pausable': '2',
    },
    'TestStepFolderReference': {
        'Condition': '', 'Path': '', 'BreakInstantiation': '0',
        'Repetition': '', 'DisabledDescription': '', 'Pausable': '0',
    },
    'XTestStep': {
        'Condition': '', 'Path': '', 'BreakInstantiation': '0',
        'Repetition': '', 'DisabledDescription': '', 'Pausable': '2',
        'ReorderAllowed': '0',
    },
    'XTestStepValue': {
        'ExplicitName': '', 'DataType': '0', 'ActionMode': '37',
        'Operator': '0', 'Value': '', 'ActionProperty': '',
        'DisabledDescription': '', 'Condition': '', 'Path': '',
    },
    'TestCaseControlFlowItem': {
        'Condition': '', 'Path': '', 'BreakInstantiation': '0',
        'Repetition': '', 'DisabledDescription': '', 'Pausable': '0',
        'StatementType': '1', 'MaximumRepetitions': '',
    },
    'TestCaseControlFlowFolder': {
        'Condition': '', 'Path': '', 'BreakInstantiation': '0',
        'Repetition': '', 'DisabledDescription': '', 'Pausable': '0',
        'StatementType': '0',
    },
    'ParameterLayerReference': {},
    'ParameterReference': {'Value': ''},
    'XModule': {
        'CheckoutWorkspace': '', 'SynchronizationPolicy': '2', 'Description': '',
        'CheckOutState': '1', 'Revision': '0', 'IncludeForSynchronization': '1',
        'InterfaceType': '0', 'ImplementationType': '', 'TechnicalId': '',
        'IsAbstract': '0', 'ValueSelectionGroup': '', 'BusinessType': '',
        'SpecialIcon': '', 'TCProperties': '',
    },
    'XModuleAttribute': {
        'ValueSelectionGroup': '', 'Description': '', 'BusinessType': '',
        'SpecialIcon': '', 'Cardinality': '1-1', 'DefaultDataType': '0',
        'DefaultActionMode': '37', 'InterfaceType': '2147483647',
        'DefaultValue': '', 'Visible': '1',
    },
    'XParam': {'Visible': '1', 'Readonly': '0', 'SupressCopy': '0', 'ParamType': '8'},
}

# Default Assocs structure per class (lists, even when empty)
ASSOC_KEYS = {
    'TestCase': ['ConfigurationLinks','Properties','ParentFolder','AttachedFiles','Items'],
    'TestStepFolder': ['ConfigurationLinks','TestCase','ParentFolder','ExecutionContainerLogs','Items'],
    'TestStepFolderReference': ['ConfigurationLinks','ParentFolder','ExecutionContainerLogs',
                                 'ReusedItem','ParameterLayerReference'],
    'XTestStep': ['ConfigurationLinks','ParentFolder','ExecutionContainerLogs',
                   'TestStepValues','Module','ExecutionLogs'],
    'XTestStepValue': ['ConfigurationLinks','TestStep','SubValues','ParentValue',
                        'ModuleAttribute','ExecutionLogs'],
    'TestCaseControlFlowItem': ['ConfigurationLinks','ParentFolder','ExecutionContainerLogs',
                                 'ControlFlowFolders'],
    'TestCaseControlFlowFolder': ['ConfigurationLinks','ExecutionContainerLogs','Items',
                                   'ParentControlFlowItem'],
    'ParameterLayerReference': ['ConfigurationLinks','TestStepFolderReference',
                                 'AllParameterReferences','ParameterLayer'],
    'ParameterReference': ['ConfigurationLinks','ParameterLayerReference','Parameter'],
    'XModule': ['ConfigurationLinks','Properties','ParentFolder','AttachedFiles',
                 'Specializations','Attributes','ReferencingAttributes',
                 'UsedAsDefaultSpecializationIn','TestSteps'],
    'XModuleAttribute': ['ConfigurationLinks','Properties','Module','Attributes',
                          'UIChildren','TestStepValues','ParentAttribute','ParentAttributeFor'],
    'XParam': ['ConfigurationLinks','ExtendableObject'],
}


# ──────────────────────────────────────────────────────────────────────────────
# Surrogate minting — deterministic UUIDs (hash-derived) for stability
# ──────────────────────────────────────────────────────────────────────────────
class SurrogateMinter:
    """Mints stable UUIDs from (entity_class, identifying_path) so re-runs on
    equivalent input produce equivalent output. UUIDs are formatted to look
    like Tosca's surrogates (8-4-4-4-12 lowercase hex)."""

    def __init__(self, salt: str):
        self.salt = salt
        self.counter = 0

    def mint(self, *parts) -> str:
        self.counter += 1
        material = self.salt + '|' + '|'.join(str(p) for p in parts) + f'|{self.counter}'
        h = hashlib.sha1(material.encode('utf-8')).hexdigest()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ──────────────────────────────────────────────────────────────────────────────
# Envelope analysis — what's inside the TestCase subtree vs preserved
# ──────────────────────────────────────────────────────────────────────────────
def find_test_case(ents) -> dict:
    """Return the (single) TestCase entity. Errors out if 0 or >1."""
    tcs = [e for e in ents.values() if e['ObjectClass'] == 'TestCase']
    if not tcs:
        sys.exit('error: base/skeleton .tsu has no TestCase entity (cannot anchor)')
    if len(tcs) > 1:
        sys.exit(f'error: base/skeleton has {len(tcs)} TestCases — use --extend to add alongside')
    return tcs[0]


def collect_tc_subtree(ents, tc_sur: str) -> set:
    """Walk down from a TestCase, collecting all entity surrogates that belong
    to it (NOT envelope, NOT RTB internals).

    Edges followed: Items, TestStepValues, SubValues, ControlFlowFolders,
    ParameterLayerReference, AllParameterReferences. We stop at TestStepFolderReference's
    ReusedItem (that's a pointer into the RTB library)."""

    follow = {'Items', 'TestStepValues', 'SubValues', 'ControlFlowFolders',
              'ParameterLayerReference', 'AllParameterReferences'}
    seen = set()
    stack = [tc_sur]
    while stack:
        sur = stack.pop()
        if sur in seen: continue
        seen.add(sur)
        e = ents.get(sur)
        if not e: continue
        for k in follow:
            for s in e.get('Assocs', {}).get(k, []):
                if s not in seen:
                    stack.append(s)
    return seen


# ──────────────────────────────────────────────────────────────────────────────
# Module catalog — index XModule + XModuleAttribute by name
# ──────────────────────────────────────────────────────────────────────────────
def build_module_catalog(ents):
    """Build {module_name: {sur, attrs: {attr_name: attr_sur}}} index over the envelope."""
    cat = {}

    # Find each XModule and its attributes
    attr_by_module = defaultdict(list)
    for e in ents.values():
        if e['ObjectClass'] == 'XModuleAttribute':
            for ms in e.get('Assocs', {}).get('Module', []):
                attr_by_module[ms].append(e)

    # Walk parent_attribute chains so that nested attrs index to their top module
    parent_of = {}
    for e in ents.values():
        if e['ObjectClass'] == 'XModuleAttribute':
            ps = e.get('Assocs', {}).get('ParentAttribute', [])
            if ps: parent_of[e['Surrogate']] = ps[0]

    for e in ents.values():
        if e['ObjectClass'] != 'XModule': continue
        mod_sur = e['Surrogate']
        mod_name = e['Attributes'].get('Name', '')
        attrs = {}
        # Direct attrs
        for a in attr_by_module.get(mod_sur, []):
            attrs[a['Attributes'].get('Name', '')] = a['Surrogate']
        # Indirect (children whose parent chain leads here)
        for a in ents.values():
            if a['ObjectClass'] != 'XModuleAttribute': continue
            cur = a['Surrogate']
            seen = set()
            while cur in parent_of and cur not in seen:
                seen.add(cur)
                cur = parent_of[cur]
            # cur is now the top-most attr — does it belong to this module?
            for ms in ents.get(cur, {}).get('Assocs', {}).get('Module', []):
                if ms == mod_sur:
                    name = a['Attributes'].get('Name', '')
                    if name and name not in attrs:
                        attrs[name] = a['Surrogate']
                    break
        cat[mod_name] = {'sur': mod_sur, 'attrs': attrs}
    return cat


def build_rtb_catalog(ents):
    """Index ReuseableTestStepBlock by Name → {sur, layer_sur, params: {name: sur}}."""
    cat = {}
    for e in ents.values():
        if e['ObjectClass'] != 'ReuseableTestStepBlock': continue
        layer_surs = e.get('Assocs', {}).get('ParameterLayer', [])
        layer_sur = layer_surs[0] if layer_surs else None
        params = {}
        if layer_sur and layer_sur in ents:
            for p_sur in ents[layer_sur].get('Assocs', {}).get('Parameters', []):
                p = ents.get(p_sur)
                if p:
                    params[p['Attributes'].get('Name', '')] = p_sur
        cat[e['Attributes'].get('Name', '')] = {
            'sur': e['Surrogate'], 'layer_sur': layer_sur, 'params': params,
        }
    return cat


# ──────────────────────────────────────────────────────────────────────────────
# Locator → XParam set (for new XModuleAttribute creation)
# ──────────────────────────────────────────────────────────────────────────────
# Maps manifest locator.raw keys → XParam Name.  Most are 1:1.
LOCATOR_RAW_TO_XPARAM = {
    'Tag': 'Tag', 'Id': 'Id', 'ClassName': 'ClassName', 'Name': 'Name',
    'AriaLabel': 'AriaLabel', 'Type': 'Type', 'Title': 'Title',
    'Href': 'Href', 'Src': 'Src', 'XPath': 'XPath', 'AbsoluteXPath': 'AbsoluteXPath',
    'RelativeId': 'RelativeId', 'PlaceHolder': 'PlaceHolder', 'Value': 'Value',
    'AlternateText': 'AlternateText',
}


def locator_raw_to_xparams(raw: dict) -> list:
    """Convert a manifest locator.raw dict to a list of (xparam_name, value) pairs.
    Skips meta keys (_self_healing, _parent_chain) — those are derived."""
    out = []
    for k, v in (raw or {}).items():
        if k.startswith('_') or v is None or v == '':
            continue
        if k in LOCATOR_RAW_TO_XPARAM:
            out.append((LOCATOR_RAW_TO_XPARAM[k], str(v)))
        elif k.startswith('attributes_'):
            out.append((k, str(v)))
        else:
            # passthrough — uncommon XParam name, preserve verbatim
            out.append((k, str(v)))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Entity factory
# ──────────────────────────────────────────────────────────────────────────────
def mk_entity(cls: str, surrogate: str, attrs_overrides=None, assocs_overrides=None) -> dict:
    """Create an entity with class defaults filled in."""
    attrs = {**DEFAULTS.get(cls, {}), **(attrs_overrides or {})}
    assocs = {k: [] for k in ASSOC_KEYS.get(cls, [])}
    if assocs_overrides:
        for k, v in assocs_overrides.items():
            assocs[k] = v
    return {
        'ObjectClass': cls,
        'Surrogate': surrogate,
        'Attributes': attrs,
        'Assocs': assocs,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Builder — walks manifest.steps stream, emits entity tree
# ──────────────────────────────────────────────────────────────────────────────
class TCBuilder:
    def __init__(self, tc_sur: str, ents: dict, mod_catalog: dict, rtb_catalog: dict,
                 minter: SurrogateMinter, base_buffer_module_name='TBox Set Buffer'):
        self.tc_sur = tc_sur
        self.ents = ents                 # envelope entities (read-only here)
        self.new_ents = {}                # entities we mint
        self.mod_cat = mod_catalog
        self.rtb_cat = rtb_catalog
        self.minter = minter
        self.buffer_module = base_buffer_module_name
        # Stack frames: (parent_sur, items_list_to_append_to)
        # The TestCase frame's items_list is the TC's Assocs.Items list.
        self.stack = []
        self.tc_items_assoc = []  # gets attached to the TC at finalize
        self.stack.append((tc_sur, self.tc_items_assoc))
        self.path = []  # for stable surrogates: stack of names
        # block_depth > 0 means we're inside a block_start..block_end frame and
        # all step/folder/control-flow nodes should be SUPPRESSED (the RTB owns
        # those entities — they're already in the envelope; the manifest only
        # unfurls them for human readability).
        self.block_depth = 0

    @property
    def cur_parent(self) -> str:
        return self.stack[-1][0]

    @property
    def cur_items(self) -> list:
        return self.stack[-1][1]

    def _push(self, sur: str, items_list: list, name=''):
        self.stack.append((sur, items_list))
        self.path.append(name)

    def _pop(self):
        self.stack.pop()
        if self.path: self.path.pop()

    def _path_key(self, *extra) -> str:
        return '/'.join(self.path + list(extra))

    # ── manifest stream handlers ──────────────────────────────────────────
    def handle(self, node: dict):
        t = node.get('type')
        # Suppress all node types except block_start/block_end (and their nested
        # blocks) when we're inside an RTB body. The RTB owns those entities.
        if self.block_depth > 0 and t not in ('block_start', 'block_end'):
            return
        h = getattr(self, f'on_{t}', None)
        if h is None:
            print(f'warning: unknown manifest node type {t!r}; skipping', file=sys.stderr)
            return
        h(node)

    def on_folder(self, node):
        name = node.get('name', '')
        sur = self.minter.mint('TSF', self._path_key(name))
        ent = mk_entity('TestStepFolder',
                        sur,
                        attrs_overrides={'Name': name},
                        assocs_overrides={
                            'TestCase': [self.tc_sur] if len(self.stack) == 1 else [],
                            'ParentFolder': [self.cur_parent],
                            'Items': [],
                        })
        self.new_ents[sur] = ent
        self.cur_items.append(sur)
        self._push(sur, ent['Assocs']['Items'], name)

    def on_folder_end(self, node):
        self._pop()

    def on_block_start(self, node):
        block_name = node.get('name', '')
        rtb = self.rtb_cat.get(block_name)
        if not rtb:
            print(f'warning: block_start references unknown ReuseableTestStepBlock {block_name!r}; '
                  f'creating reference anyway with no ReusedItem', file=sys.stderr)
        # TestStepFolderReference
        ref_sur = self.minter.mint('TSFR', self._path_key(block_name))
        # ParameterLayerReference
        plr_sur = self.minter.mint('PLR', self._path_key(block_name))
        # ParameterReference children
        param_refs_surs = []
        params_in = node.get('parameters', {}) or {}
        if rtb:
            for pname, pval in params_in.items():
                pname_sur = rtb['params'].get(pname)
                if not pname_sur: continue
                pr_sur = self.minter.mint('PR', self._path_key(block_name, pname))
                pr = mk_entity('ParameterReference', pr_sur,
                               attrs_overrides={'Value': str(pval)},
                               assocs_overrides={
                                   'ParameterLayerReference': [plr_sur],
                                   'Parameter': [pname_sur],
                               })
                self.new_ents[pr_sur] = pr
                param_refs_surs.append(pr_sur)
        plr = mk_entity('ParameterLayerReference', plr_sur,
                        attrs_overrides={'Name': 'Business Parameters'},
                        assocs_overrides={
                            'TestStepFolderReference': [ref_sur],
                            'AllParameterReferences': param_refs_surs,
                            'ParameterLayer': [rtb['layer_sur']] if rtb and rtb['layer_sur'] else [],
                        })
        self.new_ents[plr_sur] = plr
        ref = mk_entity('TestStepFolderReference', ref_sur,
                        attrs_overrides={'Name': ''},
                        assocs_overrides={
                            'ParentFolder': [self.cur_parent],
                            'ReusedItem': [rtb['sur']] if rtb else [],
                            'ParameterLayerReference': [plr_sur],
                        })
        self.new_ents[ref_sur] = ref
        self.cur_items.append(ref_sur)
        # Push a frame so block_end pops cleanly, and bump block_depth so any
        # nested step/folder/control-flow nodes (the RTB body unfurled by the
        # parser) get suppressed — those entities live in the envelope.
        self._push(ref_sur, [], block_name)
        self.block_depth += 1

    def on_block_end(self, node):
        self._pop()
        self.block_depth -= 1

    def on_step(self, node):
        step_name = node.get('name', '')
        module_name = node.get('module', '')
        actions = node.get('actions') or []

        # Resolve XModule for this step
        mod_entry = self.mod_cat.get(module_name)
        if not mod_entry:
            mod_entry = self._mint_module(module_name)
        module_sur = mod_entry['sur']

        # XTestStep
        xts_sur = self.minter.mint('XTS', self._path_key(step_name))
        sv_surs = []  # children TestStepValues
        xts = mk_entity('XTestStep', xts_sur,
                        attrs_overrides={'Name': step_name},
                        assocs_overrides={
                            'ParentFolder': [self.cur_parent],
                            'TestStepValues': sv_surs,
                            'Module': [module_sur],
                        })
        self.new_ents[xts_sur] = xts

        # Each action becomes an XTestStepValue
        for i, act in enumerate(actions):
            sv_sur = self._emit_action(xts_sur, mod_entry, act, i, step_name)
            sv_surs.append(sv_sur)

        self.cur_items.append(xts_sur)

    def _emit_action(self, xts_sur, mod_entry, act, idx, step_name) -> str:
        attr_name = act.get('element', '') or '<Buffername>'
        explicit = act.get('explicit_name') or ''
        mode_key = act.get('mode', 'set')
        prop = act.get('property', '') or ''
        value = act.get('value', '')

        # Resolve XModuleAttribute
        attr_sur = mod_entry['attrs'].get(attr_name)
        if not attr_sur:
            # Mint a new attribute if locator info present
            loc_raw = (act.get('locator') or {}).get('raw') or {}
            attr_sur = self._mint_attribute(mod_entry, attr_name, loc_raw)

        # ExplicitName: only set when the manifest explicitly carries it
        # (TBox Set Buffer steps where attr=<Buffername>, explicit=<bufferKey>).
        # Otherwise leave empty — Tosca derives the display name from the
        # ModuleAttribute's Name.
        sv_path_id = explicit or attr_name
        sv_sur = self.minter.mint('SV', self._path_key(step_name, str(idx), sv_path_id))
        sv = mk_entity('XTestStepValue', sv_sur,
                       attrs_overrides={
                           'ActionMode': ACTION_MODE_BY_KEY.get(mode_key, '37'),
                           'ActionProperty': prop,
                           'Value': str(value) if value is not None else '',
                           'ExplicitName': explicit,
                       },
                       assocs_overrides={
                           'TestStep': [xts_sur],
                           'ModuleAttribute': [attr_sur] if attr_sur else [],
                       })
        self.new_ents[sv_sur] = sv
        return sv_sur

    def _mint_module(self, name: str) -> dict:
        sur = self.minter.mint('XModule', name)
        ent = mk_entity('XModule', sur,
                        attrs_overrides={'Name': name},
                        assocs_overrides={'Attributes': []})
        self.new_ents[sur] = ent
        entry = {'sur': sur, 'attrs': {}}
        self.mod_cat[name] = entry
        return entry

    def _mint_attribute(self, mod_entry: dict, attr_name: str, loc_raw: dict) -> str:
        attr_sur = self.minter.mint('XMA', mod_entry['sur'], attr_name)
        attr = mk_entity('XModuleAttribute', attr_sur,
                         attrs_overrides={'Name': attr_name},
                         assocs_overrides={
                             'Module': [mod_entry['sur']],
                             'Properties': [],
                         })
        self.new_ents[attr_sur] = attr
        # Add the parent module's Attributes list
        if mod_entry['sur'] in self.ents:
            mod_ent = self.ents[mod_entry['sur']]
            mod_ent['Assocs'].setdefault('Attributes', []).append(attr_sur)
        elif mod_entry['sur'] in self.new_ents:
            self.new_ents[mod_entry['sur']]['Assocs']['Attributes'].append(attr_sur)
        # XParams from locator
        for xp_name, xp_value in locator_raw_to_xparams(loc_raw):
            xp_sur = self.minter.mint('XP', attr_sur, xp_name)
            xp = mk_entity('XParam', xp_sur,
                           attrs_overrides={'Name': xp_name, 'Value': xp_value},
                           assocs_overrides={'ExtendableObject': [attr_sur]})
            self.new_ents[xp_sur] = xp
            attr['Assocs']['Properties'].append(xp_sur)
        mod_entry['attrs'][attr_name] = attr_sur
        return attr_sur

    # ── control flow ──────────────────────────────────────────────────────
    def on_if(self, node):
        name = node.get('name', '') or ''
        cfi_sur = self.minter.mint('CFI', self._path_key('if', name))
        cfi = mk_entity('TestCaseControlFlowItem', cfi_sur,
                        attrs_overrides={'Name': name, 'StatementType': '1'},
                        assocs_overrides={
                            'ParentFolder': [self.cur_parent],
                            'ControlFlowFolders': [],
                        })
        self.new_ents[cfi_sur] = cfi
        self.cur_items.append(cfi_sur)
        # Synthesize a Condition folder with one Eval step
        cond_expr = node.get('condition', '') or ''
        cond_sur = self.minter.mint('CFF-Cond', self._path_key('if', name))
        cond = mk_entity('TestCaseControlFlowFolder', cond_sur,
                         attrs_overrides={'Name': 'Condition', 'StatementType': '0'},
                         assocs_overrides={
                             'ParentControlFlowItem': [cfi_sur],
                             'Items': [],
                         })
        self.new_ents[cond_sur] = cond
        cfi['Assocs']['ControlFlowFolders'].append(cond_sur)
        # Note: we don't synthesize the TBox Evaluation Tool step inside Condition
        # for v1 — the manifest may not always carry it. Tosca import may flag
        # this; if it does, extend.
        # Push a frame keyed by 'if' so that then/else/loop folders can be added.
        self._push(cfi_sur, cfi['Assocs']['ControlFlowFolders'], 'if:' + name)

    def on_then_start(self, node):
        self._push_branch('Then')

    def on_then_end(self, node):
        self._pop()

    def on_else_start(self, node):
        self._push_branch('Else')

    def on_else_end(self, node):
        self._pop()

    def on_loop_start(self, node):
        self._push_branch('Loop')

    def on_loop_end(self, node):
        self._pop()

    def _push_branch(self, kind: str):
        cfi_sur = self.cur_parent  # frame from on_if
        sur = self.minter.mint(f'CFF-{kind}', self._path_key(kind))
        cff = mk_entity('TestCaseControlFlowFolder', sur,
                        attrs_overrides={'Name': kind, 'StatementType': '0'},
                        assocs_overrides={
                            'ParentControlFlowItem': [cfi_sur],
                            'Items': [],
                        })
        self.new_ents[sur] = cff
        # Append to the CFI's ControlFlowFolders list (cur_items here points at it)
        self.cur_items.append(sur)
        self._push(sur, cff['Assocs']['Items'], kind)

    def on_if_end(self, node):
        self._pop()


# ──────────────────────────────────────────────────────────────────────────────
# Top-level orchestration
# ──────────────────────────────────────────────────────────────────────────────
def emit(manifest: dict, base_path: Path, out_path: Path,
         is_skeleton: bool, extend: bool):
    raw = json.loads(gzip.open(base_path, 'rb').read())
    ents = OrderedDict((e['Surrogate'], e) for e in raw['Entities'])

    if is_skeleton:
        # Skeleton mode — there must be no TestCase yet, OR one stub TestCase.
        existing = [e for e in ents.values() if e['ObjectClass'] == 'TestCase']
        if not existing:
            sys.exit('error: --skeleton .tsu has no TestCase shell — '
                     'export one stub test from Tosca to use as skeleton')
        tc = existing[0]
    elif extend:
        sys.exit('error: --extend not yet implemented (next iteration)')
    else:
        tc = find_test_case(ents)

    # Refresh TestCase Attributes from manifest
    test_name = manifest.get('meta', {}).get('test_name') or tc['Attributes'].get('Name', '')
    tc['Attributes']['Name'] = test_name

    # Wipe the old subtree
    old_subtree = collect_tc_subtree(ents, tc['Surrogate'])
    old_subtree.discard(tc['Surrogate'])  # keep the TC itself, just clear its body
    for s in old_subtree:
        ents.pop(s, None)

    # Reset TC Items list so we rebuild from scratch
    tc['Assocs']['Items'] = []

    # Build catalogs
    mod_cat = build_module_catalog(ents)
    rtb_cat = build_rtb_catalog(ents)

    # Mint deterministic surrogates seeded by the TC name (so re-runs are stable)
    salt = hashlib.sha1(test_name.encode('utf-8')).hexdigest()[:16]
    minter = SurrogateMinter(salt)

    # Walk manifest
    builder = TCBuilder(tc['Surrogate'], ents, mod_cat, rtb_cat, minter)
    for node in manifest.get('steps', []):
        builder.handle(node)

    # Synthesize a "TBox Set Buffer" folder for test_data preconditions if the
    # manifest doesn't already have explicit buffer-set steps (it usually does,
    # but for fresh-from-spec manifests we'd need this).
    # For v1, trust the manifest stream.

    # Attach builder's items list to the TC
    tc['Assocs']['Items'] = builder.tc_items_assoc

    # Merge new entities
    for s, e in builder.new_ents.items():
        if s in ents:
            print(f'warning: surrogate collision on {s}; keeping new', file=sys.stderr)
        ents[s] = e

    # Re-fix module Assocs.Attributes for any modules mutated in-place
    # (mk_entity already initializes; _mint_attribute appends to envelope module's list)

    out_data = {'Entities': list(ents.values())}
    raw_bytes = json.dumps(out_data, ensure_ascii=False).encode('utf-8')
    with gzip.open(out_path, 'wb', compresslevel=6) as f:
        f.write(raw_bytes)
    return len(ents), len(builder.new_ents), len(old_subtree)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--manifest', help='Path to _steps.json')
    ap.add_argument('--spec', help='Path to .spec.ts (Phase B; not yet wired)')
    ap.add_argument('--pages', help='Page-objects dir (with --spec)')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--base', help='Existing .tsu to update (full envelope + TC)')
    g.add_argument('--skeleton', help='Skeleton .tsu (envelope + stub TC)')
    ap.add_argument('--extend', action='store_true',
                    help='Add new TC alongside the existing one (use with --base)')
    ap.add_argument('--out', required=True, help='Output .tsu path')
    args = ap.parse_args()

    if not args.manifest and not args.spec:
        ap.error('must pass --manifest or --spec')

    base = Path(args.base or args.skeleton)
    out = Path(args.out)
    if not base.exists():
        sys.exit(f'error: {base} not found')

    if args.spec:
        # Lazy-import to avoid hard dependency on tree-sitter for manifest mode.
        try:
            from spec_to_manifest import build_manifest_from_spec
        except ImportError as e:
            sys.exit(f'error: --spec mode requires tree-sitter ({e}). '
                      f'install: pip3 install --user tree_sitter tree-sitter-typescript')
        spec = Path(args.spec)
        if not spec.exists():
            sys.exit(f'error: {spec} not found')
        pages = Path(args.pages) if args.pages else None
        # If a base manifest exists alongside the base .tsu (the parse_tsu
        # output), use it as a context source — provides project name, base_url,
        # all_urls, etc. — so the regenerated .tsu picks them up unchanged.
        base_manifest = None
        sibling = base.with_name(base.stem + '_steps.json')
        if sibling.exists():
            try:
                base_manifest = json.loads(sibling.read_text(encoding='utf-8'))
            except Exception:
                base_manifest = None
        manifest = build_manifest_from_spec(spec, pages, base_manifest)
    else:
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))

    total, minted, deleted = emit(
        manifest=manifest,
        base_path=base,
        out_path=out,
        is_skeleton=bool(args.skeleton),
        extend=args.extend,
    )

    print(f'wrote {out}')
    print(f'  {total} total entities, {minted} new, {deleted} replaced from base')


if __name__ == '__main__':
    main()
