#!/usr/bin/env python3
"""
parse_tsu.py — Tosca .tsu → HTML report + JSON step manifest + Playwright scaffold.

Usage:
    python3 parse_tsu.py file.tsu                            # HTML only
    python3 parse_tsu.py file.tsu --steps-json               # + JSON manifest
    python3 parse_tsu.py file.tsu --playwright               # + Playwright project
    python3 parse_tsu.py file.tsu --all                      # all outputs
    python3 parse_tsu.py file.tsu --all --force              # overwrite hand-edited spec/config
    python3 parse_tsu.py file.tsu --all --out-dir <path>     # custom output location

Outputs land in `<input_dir>/out/<stem>/` by default. Use --out-dir to override
(e.g. point everything at a single shared dir, or bypass the per-stem nesting).
The default keeps the working tree clean — drop `out/` into .gitignore once and
every parse run is covered.

The parser is app-agnostic. Locator priority blends XParam type with
SelfHealingData weights from the .tsu itself. App-specific test attributes
(data-test-id, data-cy, etc.) and modules to skip are configurable via
parse_tsu.config.json placed next to the .tsu OR in the output directory
(out-dir wins for per-run overrides).

Default config (used when no config file present):
    {
      "test_attributes": ["data-test-id", "data-testid", "data-test", "data-cy", "data-qa"],
      "skip_modules":    ["TBox Set Buffer", "TBox Window Operation",
                          "TBox Start Program", "TBox Evaluation Tool",
                          "TBox Dialog", "TBox Buffer"]
    }
"""

import sys, json, re, gzip, html as html_mod
from collections import defaultdict, Counter
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# CLI + config
# ──────────────────────────────────────────────────────────────────────────────
if len(sys.argv) < 2:
    print(__doc__); sys.exit(1)

# Parse args: positional .tsu path, flags, and --out-dir <path>
_args     = sys.argv[1:]
tsu_path  = Path(_args[0])
flags     = set()
out_dir_override = None
i = 1
while i < len(_args):
    a = _args[i]
    if a == '--out-dir' and i + 1 < len(_args):
        out_dir_override = Path(_args[i + 1])
        i += 2
    else:
        flags.add(a)
        i += 1

gen_pw   = '--all' in flags or '--playwright' in flags
gen_json = '--all' in flags or '--steps-json' in flags
force    = '--force' in flags

# Outputs go to out/<stem>/ next to the input by default. Keeps the workspace
# clean (everything generated lives under one predictable subdir, easy to
# .gitignore). Override with --out-dir <path>.
if out_dir_override is not None:
    out_dir = out_dir_override
else:
    out_dir = tsu_path.parent / 'out' / tsu_path.stem
out_dir.mkdir(parents=True, exist_ok=True)

DEFAULT_CFG = {
    'test_attributes': ['data-test-id', 'data-testid', 'data-test', 'data-cy', 'data-qa'],
    'skip_modules':    ['TBox Set Buffer', 'TBox Window Operation', 'TBox Start Program',
                        'TBox Evaluation Tool', 'TBox Dialog', 'TBox Buffer'],
}
# Config file is read from BOTH the input dir (project-level shared config) and
# the output dir (per-run override). Per-run wins when both exist.
cfg = dict(DEFAULT_CFG)
for _cfg_loc in (tsu_path.parent / 'parse_tsu.config.json',
                 out_dir / 'parse_tsu.config.json'):
    if _cfg_loc.exists():
        cfg = {**cfg, **json.loads(_cfg_loc.read_text(encoding='utf-8'))}

# ──────────────────────────────────────────────────────────────────────────────
# Tosca constants
# ──────────────────────────────────────────────────────────────────────────────
ACTION_MODE = {'1':'input', '37':'set', '69':'verify', '101':'waitFor',
               '165':'bufferRead', '517':'optionalSet'}
CLICK_TOKENS = {'CLICK', 'DOUBLECLICK', 'RIGHTCLICK', 'CLICKDOWN', 'CLICKUP'}
KEY_TOKENS   = {'ENTER', 'TAB', 'ESCAPE', 'SPACE', 'BACKSPACE', 'DELETE',
                'ARROWUP', 'ARROWDOWN', 'ARROWLEFT', 'ARROWRIGHT', 'HOME', 'END'}

# Boolean-property assertions for verify/waitFor: prop → (true_assertion, false_assertion)
ASSERT_PROPS = {
    'Visible':  ('toBeVisible()',      'toBeHidden()'),
    'Enabled':  ('toBeEnabled()',      'toBeDisabled()'),
    'Disabled': ('toBeDisabled()',     'toBeEnabled()'),
    'Checked':  ('toBeChecked()',      'not.toBeChecked()'),
    'Selected': ('toBeChecked()',      'not.toBeChecked()'),
    'Focused':  ('toBeFocused()',      'not.toBeFocused()'),
    'Editable': ('toBeEditable()',     'not.toBeEditable()'),
    'ReadOnly': ('not.toBeEditable()', 'toBeEditable()'),
    'Exists':   ('toBeAttached()',     'not.toBeAttached()'),
}

# XParam Names that carry framework state, not locator hints
META_XPARAMS = {'Engine', 'BusinessAssociation', 'ControlFramework', 'FireEvent',
                'SelfHealingData', 'RelativeId', 'UserSimulation',
                'IgnoreAriaControls', 'EnableSlotContentHandling', 'ScrollingBehavior',
                'SpecialExecutionTask', 'WaitBefore', 'WaitAfter', 'ConstraintIndex'}

ANGULAR_CLASS_RE   = re.compile(r'ng-tns-c\d+-\d+')
DYNAMIC_XPATH_RE   = re.compile(r"id\('(pn_id_[^']+|p-(?:menubarsub|menubar)_[^']+)'\)")
TOSCA_REF_RE       = re.compile(r'^\{(PL|B|CP|XL)\[([^\]]+)\]\}\*?$')
TOSCA_TOKEN_RE     = re.compile(r'^\{(\w+)\}$')
TOSCA_TOKEN_ARG_RE = re.compile(r'^\{(\w+)\[([^\]]+)\]\}$')   # {KEY[Ctrl+A]}, {SELECT[3]}
TOSCA_EXPR_RE      = re.compile(r'^\{[A-Z][A-Z0-9_]*\[')      # {CALC[…]}, {TRIM[…]}, {STRINGREPLACE[…]…}
TOSCA_ENCRYPTED_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    r'[A-Za-z0-9+/=]{20,}$'
)
RESERVED_ENV = {'USERNAME','USER','PASSWORD','HOME','PATH','SHELL','TERM','LANG',
                'LOGNAME','PWD','OLDPWD','SHLVL','TMPDIR','TEMP','TMP',
                'COMPUTERNAME','USERDOMAIN','APPDATA','SYSTEMROOT'}

ROLE_MAP = {'BUTTON':'button', 'A':'link', 'INPUT':'textbox', 'IMG':'img',
            'H1':'heading', 'H2':'heading', 'H3':'heading', 'H4':'heading', 'H5':'heading',
            'NAV':'navigation', 'UL':'list', 'OL':'list', 'LI':'listitem'}
STANDARD_TAGS = {'BUTTON','A','INPUT','IMG','DIV','H1','H2','H3','H4','H5','SPAN',
                 'P','FORM','SELECT','TEXTAREA','NAV','UL','LI','HEADER','FOOTER',
                 'TABLE','TR','TD','TH','SECTION','ARTICLE','MAIN','ASIDE','LABEL',
                 'I','SVG','PATH','OL'}

# ──────────────────────────────────────────────────────────────────────────────
# Decompress + index
# ──────────────────────────────────────────────────────────────────────────────
data = json.loads(gzip.open(tsu_path, 'rb').read())
ents = {e['Surrogate']: e for e in data['Entities']}

xparam_by_attr     = defaultdict(list)
sv_top_by_step     = defaultdict(list)   # step → top-level SVs (those without ParentValue)
sv_children        = defaultdict(list)   # parent SV sur → child SVs
sv_to_attr         = {}
step_to_module     = {}
attr_to_module_dir = {}                  # attrs that have a direct Module assoc
attr_parent_attr   = {}                  # attr_sur → parent attr_sur

for e in data['Entities']:
    cls = e['ObjectClass']; asc = e.get('Assocs', {}); sur = e['Surrogate']
    if cls == 'XParam':
        for s in asc.get('ExtendableObject', []):
            xparam_by_attr[s].append(e)
    elif cls == 'XModuleAttribute':
        for ms in asc.get('Module', []):
            attr_to_module_dir[sur] = ents.get(ms)
        for ps in asc.get('ParentAttribute', []):
            attr_parent_attr[sur] = ps
    elif cls == 'XTestStepValue':
        parents = asc.get('ParentValue', [])
        if parents:
            sv_children[parents[0]].append(e)
        else:
            for ts in asc.get('TestStep', []):
                sv_top_by_step[ts].append(e)
        for a_sur in asc.get('ModuleAttribute', []):
            sv_to_attr[sur] = ents.get(a_sur)
    elif cls == 'XTestStep':
        for ms in asc.get('Module', []):
            step_to_module[sur] = ents.get(ms)

def attr_top_module(attr_sur):
    """Walk ParentAttribute chain upward until we find an attr with a Module assoc."""
    seen = set(); cur = attr_sur
    while cur and cur not in seen:
        seen.add(cur)
        if cur in attr_to_module_dir:
            return attr_to_module_dir[cur]
        cur = attr_parent_attr.get(cur)
    return None

# Module → all attrs (including transitively-owned children)
module_attrs = defaultdict(list)
for ma in data['Entities']:
    if ma['ObjectClass'] != 'XModuleAttribute': continue
    mod = attr_top_module(ma['Surrogate'])
    if mod: module_attrs[mod['Surrogate']].append(ma)

def parent_attr_chain(attr_sur):
    chain = []; seen = set(); cur = attr_parent_attr.get(attr_sur)
    while cur and cur not in seen:
        seen.add(cur); chain.append(cur)
        cur = attr_parent_attr.get(cur)
    return chain

def walk_svs(sv):
    """Yield SV and all its descendants."""
    yield sv
    for ch in sv_children.get(sv['Surrogate'], []):
        yield from walk_svs(ch)

# ──────────────────────────────────────────────────────────────────────────────
# Per-call ParameterLayer index — for {PL[X]} resolution
# ──────────────────────────────────────────────────────────────────────────────
plr_by_call = {}        # TestStepFolderReference sur → {param_name: outer_value}
for e in data['Entities']:
    if e['ObjectClass'] != 'TestStepFolderReference': continue
    plr_surs = e.get('Assocs', {}).get('ParameterLayerReference', [])
    if not plr_surs: continue
    plr = ents.get(plr_surs[0])
    if not plr: continue
    layer = {}
    for pref_sur in plr.get('Assocs', {}).get('AllParameterReferences', []):
        pref = ents.get(pref_sur)
        if not pref: continue
        param_surs = pref.get('Assocs', {}).get('Parameter', [])
        if not param_surs: continue
        pname = ents[param_surs[0]]['Attributes'].get('Name', '')
        if pname:
            layer[pname] = pref['Attributes'].get('Value', '')
    plr_by_call[e['Surrogate']] = layer

# ──────────────────────────────────────────────────────────────────────────────
# XParam helpers
# ──────────────────────────────────────────────────────────────────────────────
def xparams(attr_sur):
    return {p['Attributes']['Name']: p['Attributes']['Value']
            for p in xparam_by_attr.get(attr_sur, [])
            if p['Attributes'].get('Name') not in META_XPARAMS
            and p['Attributes'].get('Value')}

def xparam_meta(attr_sur):
    """Return {name: value} for meta XParams (timing, constraint index)."""
    return {p['Attributes']['Name']: p['Attributes']['Value']
            for p in xparam_by_attr.get(attr_sur, [])
            if p['Attributes'].get('Name') in ('WaitBefore','WaitAfter','ConstraintIndex')
            and p['Attributes'].get('Value')}

def self_healing(attr_sur):
    for p in xparam_by_attr.get(attr_sur, []):
        if p['Attributes'].get('Name') != 'SelfHealingData': continue
        try:
            shd = json.loads(p['Attributes']['Value'])
            return [{'name': c['Name'], 'value': c.get('Value', ''),
                     'weight': float(c.get('Weight', 0))}
                    for c in shd.get('HealingParameters', {}).get('$values', [])
                    if c.get('Value') and c['Value'] not in ('<No label associated>', '')]
        except Exception:
            return []
    return []

def relative_ctx(attr_sur):
    for p in xparam_by_attr.get(attr_sur, []):
        if p['Attributes'].get('Name') != 'RelativeId': continue
        raw = p['Attributes']['Value']
        keys = re.findall(r'<Key>([^<]+)</Key>', raw)
        vals = re.findall(r'<Value>([^<]+)</Value>', raw)
        ctx  = dict(zip(keys, vals))
        for k in ('Engine', 'BusinessAssociation'): ctx.pop(k, None)
        return ctx
    return {}

# ──────────────────────────────────────────────────────────────────────────────
# Locator candidate ranking — data-driven, weight-blended
# ──────────────────────────────────────────────────────────────────────────────
def _esc(s): return s.replace("'", "\\'").replace('"', '\\"')

def _normalize_xpath(xp: str) -> str:
    """Convert Tosca's XPath conventions to standard XPath that Playwright accepts:
       1. Tosca often wraps the expression in literal double quotes ("id('X')/...")
          — strip those.
       2. Tosca uses XPath 1.0 id('X')/path syntax that Playwright's injected
          script can't evaluate; rewrite to //*[@id='X']/path.
    """
    if not xp: return xp
    s = xp.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    s = re.sub(r"id\(['\"]([^'\"]+)['\"]\)", r"//*[@id='\1']", s)
    return s
def _ts(s):  return json.dumps(s)

def _is_dynamic_xpath(xp): return bool(DYNAMIC_XPATH_RE.search(xp)) if xp else False

def _stable_class(cls):
    if not cls: return ''
    return ' '.join(t for t in cls.split() if not ANGULAR_CLASS_RE.match(t) and t)

def collect_candidates(attr_sur, value_hint=''):
    """Build a scored list of locator candidates from XParams + SelfHealingData."""
    xp = xparams(attr_sur)
    meta = xparam_meta(attr_sur)
    sh = self_healing(attr_sur)
    sh_w = {h['name'].lower(): h['weight'] for h in sh}
    def w(name, default): return sh_w.get(name.lower(), default)

    nth = meta.get('ConstraintIndex', '')
    def with_nth(sel):
        if nth and nth.isdigit() and int(nth) > 0:
            return f"{sel}.nth({int(nth)-1})"
        return sel

    cands = []

    # data-test-id family (configurable list of test attributes)
    seen_attrs = set()
    for ta in cfg['test_attributes']:
        for key in (f'attributes_{ta}', ta):
            v = xp.get(key, '')
            if v and not v.startswith('{') and '*' not in v:
                seen_attrs.add(key)
                cands.append({
                    'sel': with_nth(f"page.locator('[{ta}=\"{_esc(v)}\"]')"),
                    'score': 100 + w(key, 1.0),
                    'kind': f'attr:{ta}',
                })

    # id
    if xp.get('Id') and not xp['Id'].startswith('{'):
        cands.append({
            'sel': f"page.locator('#{_esc(xp['Id'])}')",
            'score': 90 + w('Id', 1.0),
            'kind': 'id',
        })

    # any other attributes_* (generic handler)
    for k, v in xp.items():
        if not k.startswith('attributes_') or k in seen_attrs: continue
        if not v or v.startswith('{'): continue
        attr_name = k[len('attributes_'):]
        if attr_name in ('class',): continue   # use ClassName path instead
        cands.append({
            'sel': with_nth(f"page.locator('[{attr_name}=\"{_esc(v)}\"]')"),
            'score': 70 + w(k, 0.4),
            'kind': f'attr:{attr_name}',
        })

    # aria-label
    aria = xp.get('AriaLabel') or xp.get('attributes_aria-label') or ''
    if aria and not aria.startswith('{'):
        cands.append({
            'sel': with_nth(f"page.getByLabel({_ts(aria)}, {{ exact: true }})"),
            'score': 65 + w('AriaLabel', 0.7),
            'kind': 'aria',
        })

    # role + name (when tag maps to a role and we have visible text)
    tag   = xp.get('Tag', '')
    inner = xp.get('InnerText') or xp.get('innerText') or ''
    sh_label = next((h['value'] for h in sh
                     if h['name'] == 'Label' and h['value']
                     and not h['value'].startswith('{')
                     and len(h['value']) <= 80), '')
    name_text = next((t for t in (inner, sh_label, value_hint)
                      if t and not t.startswith('{') and len(t) <= 80 and '\n' not in t), '')
    if tag in ROLE_MAP and name_text:
        cands.append({
            'sel': with_nth(f"page.getByRole('{ROLE_MAP[tag]}', "
                            f"{{ name: {_ts(name_text)}, exact: true }})"),
            'score': 60,
            'kind': 'role+name',
        })

    # href
    href = xp.get('Href') or xp.get('attributes_href') or ''
    if tag == 'A' and href and href.startswith('http'):
        path = href.split('/', 3)[-1]
        cands.append({
            'sel': f"page.locator('a[href*={_ts(path)}]')",
            'score': 50 + w('Href', 0.5),
            'kind': 'href',
        })

    # img src (skip CDN-volatile sources)
    src = xp.get('Src') or ''
    if tag == 'IMG' and src and 'kc-usercontent.com' not in src and 'opentext.cloud' not in src:
        seg = src.split('/')[-1][:40]
        cands.append({
            'sel': f"page.locator('img[src*={_ts(seg)}]')",
            'score': 45,
            'kind': 'src',
        })

    # plain text
    if name_text and not aria and tag not in ROLE_MAP:
        cands.append({
            'sel': with_nth(f"page.getByText({_ts(name_text)}, {{ exact: true }})"),
            'score': 40,
            'kind': 'text',
        })

    # stable class
    cls = _stable_class(xp.get('ClassName', ''))
    if cls:
        primary_cls = cls.split()[0]
        sel = f"{tag.lower()}.{primary_cls}" if tag and tag in STANDARD_TAGS else f".{primary_cls}"
        cands.append({
            'sel': with_nth(f"page.locator('{sel}')"),
            'score': 25 + w('ClassName', 0),
            'kind': 'class',
        })

    # custom tag alone (web component)
    if tag and tag not in STANDARD_TAGS:
        cands.append({
            'sel': with_nth(f"page.locator('{tag.lower()}')"),
            'score': 20,
            'kind': 'tag',
        })

    # XPath (last; flag if dynamic)
    xpath = xp.get('XPath') or xp.get('Xpath') or ''
    if xpath:
        is_dyn = _is_dynamic_xpath(xpath)
        cands.append({
            'sel': f"page.locator('xpath={_esc(_normalize_xpath(xpath))}')",
            'score': 5 if is_dyn else 15,
            'kind': 'xpath',
            'warn': 'dynamic-xpath' if is_dyn else '',
        })

    # Promote SelfHealing-only candidates the XParams missed
    for h in sh:
        if h['weight'] < 0.3: continue
        n, v = h['name'], h['value']
        if n in ('XPath','Xpath') and not _is_dynamic_xpath(v):
            cands.append({'sel': f"page.locator('xpath={_esc(_normalize_xpath(v))}')",
                          'score': 14 + h['weight'], 'kind': 'sh:xpath'})
        elif n == 'Url' and v.startswith('http'):
            path = v.split('/', 3)[-1]
            cands.append({'sel': f"page.locator('a[href*={_ts(path)}]')",
                          'score': 48 + h['weight'], 'kind': 'sh:url'})

    cands.sort(key=lambda c: -c['score'])
    return cands

def build_locator(attr_sur, action_property='', value_hint=''):
    """Return (primary, fallback, notes) for a Tosca attribute."""
    if not attr_sur:
        return None, None, []

    # Suppress system-token value hints
    if (action_property in ('Visible','Enabled','Checked')
            or value_hint in ('True','False','')
            or (value_hint.startswith('{') and value_hint.endswith('}'))):
        value_hint = ''

    cands = collect_candidates(attr_sur, value_hint=value_hint)
    if not cands:
        return None, None, []

    primary  = cands[0]['sel']
    fallback = cands[1]['sel'] if len(cands) > 1 and cands[1]['sel'] != primary else None
    notes    = []
    if cands[0].get('warn') == 'dynamic-xpath':
        notes.append('⚠ dynamic XPath – may break on re-render')

    # Scope under nearest custom-tag parent for uniqueness (e.g. <app-terms>...)
    parent_tag = ''
    for pa_sur in parent_attr_chain(attr_sur):
        ptag = xparams(pa_sur).get('Tag', '')
        if ptag and ptag not in STANDARD_TAGS:
            parent_tag = ptag.lower(); break
    if parent_tag and primary.startswith('page.locator(') and 'xpath=' not in primary:
        scoped = primary.replace('page.locator(', f"page.locator('{parent_tag}').locator(", 1)
        if not fallback: fallback = primary
        primary = scoped

    return primary, fallback, notes

# ──────────────────────────────────────────────────────────────────────────────
# PL/Buffer resolution
# ──────────────────────────────────────────────────────────────────────────────
EMBEDDED_REF_RE = re.compile(r'\{(PL|B|CP|XL)\[([^\]]+)\]\}')

def _lookup_ref(kind, name, pl_stack, buffer_map):
    if kind == 'PL':
        for layer in reversed(pl_stack):
            if name in layer: return layer[name]
    elif kind == 'B':
        if name in buffer_map: return buffer_map[name]
    elif kind == 'CP':
        return f'$CONFIG:{name}'
    elif kind == 'XL':
        return f'$XL:{name}'
    return None

def resolve_value(val, pl_stack, buffer_map, max_hops=6):
    """Resolve Tosca refs in `val`. Whole-string refs unwrap until literal;
    embedded refs are substituted in-place. Bounded by max_hops."""
    if not val or not isinstance(val, str) or max_hops <= 0: return val
    seen = set()
    for _ in range(max_hops):
        if val in seen: break
        seen.add(val)
        m = TOSCA_REF_RE.match(val)
        if not m: break
        nxt = _lookup_ref(m.group(1), m.group(2), pl_stack, buffer_map)
        if nxt is None or nxt == val: break
        val = nxt
    if not EMBEDDED_REF_RE.search(val): return val
    def _sub(m):
        nxt = _lookup_ref(m.group(1), m.group(2), pl_stack, buffer_map)
        if nxt is None: return m.group(0)
        return resolve_value(nxt, pl_stack, buffer_map, max_hops - 1)
    return EMBEDDED_REF_RE.sub(_sub, val)

# ──────────────────────────────────────────────────────────────────────────────
# Step resolver
# ──────────────────────────────────────────────────────────────────────────────
def step_actions(step_ent, pl_stack, buffer_map):
    actions = []
    for top_sv in sv_top_by_step.get(step_ent['Surrogate'], []):
        for sv in walk_svs(top_sv):
            attrs = sv['Attributes']
            mode  = ACTION_MODE.get(attrs.get('ActionMode',''), attrs.get('ActionMode',''))
            prop  = attrs.get('ActionProperty', '')
            val   = attrs.get('Value', '')
            ma    = sv_to_attr.get(sv['Surrogate'])
            if not ma: continue
            # Skip pure-container leaves (no value AND has children — children carry the action)
            if val == '{NULL}' and sv_children.get(sv['Surrogate']):
                continue
            attr_sur  = ma['Surrogate']
            attr_name = ma['Attributes'].get('Name', '')
            primary, fallback, notes = build_locator(attr_sur, prop, val)
            meta = xparam_meta(attr_sur)
            actions.append({
                'mode': mode,
                'action_property': prop,
                'element_name': attr_name,
                'explicit_name': attrs.get('ExplicitName', ''),
                'value': val,
                'value_resolved': resolve_value(val, pl_stack, buffer_map),
                'primary_locator': primary,
                'fallback_locator': fallback,
                'notes': notes,
                'attr_sur': attr_sur,
                'wait_before': meta.get('WaitBefore', ''),
                'wait_after':  meta.get('WaitAfter', ''),
            })
    return actions

def resolve_steps(sur_list, depth=0, pl_stack=None, buffer_map=None):
    if pl_stack is None:   pl_stack = []
    if buffer_map is None: buffer_map = {}
    out = []
    for sur in sur_list:
        e = ents.get(sur)
        if not e: continue
        cls = e['ObjectClass']
        name = e['Attributes'].get('Name', '')

        if cls == 'XTestStep':
            mod = step_to_module.get(sur)
            mod_name = mod['Attributes']['Name'] if mod else ''
            acts = step_actions(e, pl_stack, buffer_map)
            # Replay TBox Set Buffer assignments into buffer_map for later steps
            if mod_name == 'TBox Set Buffer':
                for a in acts:
                    en = a.get('explicit_name', '')
                    if not en: continue
                    # Prefer the resolved value (handles {PL[X]} → outer literal chains)
                    rv = a.get('value_resolved') or a.get('value', '')
                    if rv and not rv.startswith('{'):
                        buffer_map[en] = rv
            out.append({'type':'step', 'name':name, 'module':mod_name,
                        'actions':acts, 'depth':depth})
            for sub in e.get('Assocs', {}).get('Items', []):
                out += resolve_steps([sub], depth+1, pl_stack, buffer_map)

        elif cls == 'TestStepFolder':
            out.append({'type':'folder', 'name':name, 'depth':depth})
            out += resolve_steps(e.get('Assocs', {}).get('Items', []),
                                 depth+1, pl_stack, buffer_map)
            out.append({'type':'folder_end', 'name':name, 'depth':depth})

        elif cls == 'TestStepFolderReference':
            layer = plr_by_call.get(sur, {})
            pl_stack.append(layer)
            for r in e.get('Assocs', {}).get('ReusedItem', []):
                tgt = ents.get(r)
                if not tgt: continue
                bn = tgt['Attributes'].get('Name', '')
                out.append({'type':'block_start', 'name':bn, 'depth':depth,
                            'parameters': dict(layer)})
                out += resolve_steps(tgt.get('Assocs', {}).get('Items', []),
                                     depth+1, pl_stack, buffer_map)
                out.append({'type':'block_end', 'name':bn, 'depth':depth})
            pl_stack.pop()

        elif cls == 'TestCaseControlFlowItem':
            cffs = {}
            for cs in e.get('Assocs', {}).get('ControlFlowFolders', []):
                cf = ents.get(cs)
                if cf: cffs[cf['Attributes'].get('Name', '')] = cf

            # Pull condition expression
            cond_text = ''
            cond_folder = cffs.get('Condition')
            if cond_folder:
                for it in cond_folder.get('Assocs', {}).get('Items', []):
                    c_ent = ents.get(it)
                    if not c_ent: continue
                    for sv in sv_top_by_step.get(c_ent['Surrogate'], []):
                        cond_text = sv['Attributes'].get('Value', '') or cond_text
            cond_resolved = resolve_value(cond_text, pl_stack, buffer_map) if cond_text else ''

            out.append({'type':'if', 'name':name, 'depth':depth,
                        'condition': cond_text, 'condition_resolved': cond_resolved})
            for branch in ('Then', 'Loop', 'Else'):
                cf = cffs.get(branch)
                if not cf: continue
                out.append({'type':branch.lower()+'_start', 'depth':depth+1, 'name':branch})
                out += resolve_steps(cf.get('Assocs', {}).get('Items', []),
                                     depth+2, pl_stack, buffer_map)
                out.append({'type':branch.lower()+'_end', 'depth':depth+1, 'name':branch})
            out.append({'type':'if_end', 'name':name, 'depth':depth})

        elif cls == 'TestCaseControlFlowFolder':
            out += resolve_steps(e.get('Assocs', {}).get('Items', []),
                                 depth, pl_stack, buffer_map)
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Walk the test case
# ──────────────────────────────────────────────────────────────────────────────
# Closure helper: which entity surrogates does this test case touch?
# Walking from the TC through items/folders/blocks/steps/SVs/attributes/
# modules/RTBs/control-flow/parameter layers (transitively, including RTBs
# called by RTBs). The set returned is the per-TC "filter" for
# multi-TC .tsu outputs so each test case only ships the modules it
# actually uses.
_TC_CLOSURE_EDGES = {
    'TestCase':                ['Items'],
    'TestStepFolder':          ['Items'],
    'TestStepFolderReference': ['ReusedItem', 'ParameterLayerReference'],
    'ReuseableTestStepBlock':  ['Items', 'ParameterLayer'],
    'XTestStep':               ['TestStepValues', 'Module'],
    'XTestStepValue':          ['SubValues', 'ModuleAttribute'],
    'XModuleAttribute':        ['Properties', 'ParentAttribute', 'Module'],
    'XModule':                 ['Attributes'],
    'TestCaseControlFlowItem': ['ControlFlowFolders'],
    'TestCaseControlFlowFolder': ['Items'],
    'ParameterLayer':          ['Parameters'],
    'ParameterLayerReference': ['AllParameterReferences', 'ParameterLayer'],
    'ParameterReference':      ['Parameter'],
}
def tc_closure(tc_sur):
    seen, stack = set(), [tc_sur]
    while stack:
        s = stack.pop()
        if s in seen: continue
        seen.add(s)
        e = ents.get(s)
        if not e: continue
        for edge in _TC_CLOSURE_EDGES.get(e['ObjectClass'], ()):
            for ns in e.get('Assocs', {}).get(edge, ()):
                if ns not in seen: stack.append(ns)
    return seen

def _tc_stem(tc_entity):
    """Sanitize a TestCase name into a filesystem-safe stem.
    Preserves the bracketed test ID when present (e.g. [1034698]) and the
    main descriptive part of the name."""
    name = tc_entity['Attributes'].get('Name', '') or 'unnamed_tc'
    # Extract test ID like [1034698] if present
    id_m = re.search(r'\[(\d{5,})\]', name)
    test_id = id_m.group(1) + '_' if id_m else ''
    # Strip all bracketed prefixes [X][Y][Z] then sanitize
    rest = re.sub(r'\[[^\]]*\]', '', name).strip()
    rest = re.sub(r'[^a-zA-Z0-9]+', '_', rest).strip('_').lower()
    rest = rest[:60]  # cap length
    return f'{test_id}{rest}' or tc_entity['Surrogate'][:8]


# Generic Tosca workspace folders that aren't useful for grouping tests.
# Anything else in the parent chain becomes part of the area folder name.
_AREA_FOLDER_NOISE = {'TestCases', 'Library', 'Components', 'TBox',
                       'Modules', 'Configuration', 'Project', ''}

def _tc_area(tc):
    """Derive a sanitized 'area' folder for shared playwright-test/tests/.
    Walks the immediate parent chain; takes the first meaningful folder name,
    skipping generic Tosca workspace folders (TestCases, Library, etc.).
    Returns '' when no meaningful folder is found (test lands in tests/ flat)."""
    parents = tc.get('Assocs', {}).get('ParentFolder', [])
    while parents:
        p_sur = parents[0]
        p = ents.get(p_sur)
        if not p: break
        nm = (p['Attributes'].get('Name') or '').strip()
        if nm and nm not in _AREA_FOLDER_NOISE:
            return re.sub(r'[^a-zA-Z0-9]+', '_', nm).strip('_').lower()
        parents = p.get('Assocs', {}).get('ParentFolder', [])
    return ''

# Module-level accumulator for env vars across multi-TC runs. Each per-TC
# call's safe_env() populates this; we write the union to .env.example
# once after the dispatch loop.
env_vars_used = {}

# Last per-TC base_url; used as the default for the shared config in multi-TC.
# (Each spec sets its own test.use({ baseURL }) so this is just a fallback.)
_last_base_url = ''

_PACKAGE_JSON = """{
  "name": "tosca-playwright",
  "version": "1.0.0",
  "scripts": {
    "test": "playwright test",
    "test:headed": "playwright test --headed",
    "report": "playwright show-report"
  },
  "devDependencies": {
    "@playwright/test": "^1.44.0",
    "typescript": "^5.4.0",
    "dotenv": "^16.0.0"
  }
}
"""

def _build_env_lines(base_url, env_vars):
    """Render the .env.example body. Used for both single-TC (per-call) and
    multi-TC (post-loop, with the accumulated union)."""
    lines = [
        "# Auto-generated by parse_tsu.py — list of environment variables the",
        "# generated spec references. Copy to `.env` and fill in real values.",
        "",
        f"# Recorded base URL: {base_url}",
        "# Override the default baseURL in playwright.config.ts (and all per-spec",
        "# `test.use({ baseURL })` defaults) without editing the spec:",
        f"# BASE_URL={base_url}",
        "",
    ]
    if env_vars:
        lines.append("# Credential / config parameters referenced by spec actions:")
        for name in sorted(env_vars):
            sources = env_vars[name]
            hint = f" (from CP[{sources[0][0]}]" + (f" / {sources[0][1]}" if sources[0][1] else "") + ")"
            lines.append(f"{name}={hint}")
        lines.append("")
    return lines

def _write_shared_pw_artifacts(pw_dir, base_url, force_overwrite):
    """Write playwright.config.ts, package.json, .env.example ONCE for the
    shared multi-TC playwright project. Uses the module-level env_vars_used
    accumulated across all per-TC pipelines."""
    pw_dir.mkdir(parents=True, exist_ok=True)
    config_content = (
        "import { defineConfig, devices } from '@playwright/test';\n"
        "import 'dotenv/config';\n\n"
        "export default defineConfig({\n"
        "  testDir: './tests',\n"
        "  fullyParallel: true,\n"
        "  timeout: 300_000,\n"
        "  reporter: 'html',\n"
        "  use: {\n"
        f"    baseURL: process.env.BASE_URL || '{base_url}',\n"
        "    screenshot: 'only-on-failure',\n"
        "    video: 'on',\n"
        "    trace: 'on-first-retry',\n"
        "  },\n"
        "  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],\n"
        "});\n"
    )
    cfg_p = pw_dir / 'playwright.config.ts'
    if not cfg_p.exists() or force_overwrite:
        cfg_p.write_text(config_content)
        print(f"[✓] Shared config       → playwright-test/playwright.config.ts  (baseURL: {base_url})")
    pkg_p = pw_dir / 'package.json'
    if not pkg_p.exists() or force_overwrite:
        pkg_p.write_text(_PACKAGE_JSON)
        print(f"[✓] Shared package      → playwright-test/package.json")
    env_p = pw_dir / '.env.example'
    env_p.write_text("\n".join(_build_env_lines(base_url, env_vars_used)))
    print(f"[✓] Shared env template → playwright-test/.env.example  ({len(env_vars_used)} vars across all cases)")


def _per_tc_pipeline(tc, project, base_out_dir, multi_tc):
    """Run the full HTML/JSON/Playwright generation pipeline for one TC.
    Single-TC: outputs at base_out_dir/{<stem>_steps.json, <stem>_report.html, playwright-test/...}.
    Multi-TC: per-case JSON+HTML at base_out_dir/cases/<tc_stem>/, and a SHARED
    playwright project at base_out_dir/playwright-test/ where each spec lives
    in tests/<area>/<tc_stem>.spec.ts and pages/ holds the union of all
    referenced modules."""
    # Per-TC paths
    if multi_tc:
        case_dir = base_out_dir / 'cases' / _tc_stem(tc)
        case_dir.mkdir(parents=True, exist_ok=True)
        out_dir = case_dir          # JSON + HTML go here
        pw_root = base_out_dir / 'playwright-test'   # SHARED across all TCs
        area    = _tc_area(tc)
        print(f"  [tc] {tc['Attributes'].get('Name','')[:80]}")
        print(f"       → cases/{_tc_stem(tc)}/  +  playwright-test/tests/{area+'/' if area else ''}{_tc_stem(tc)}.spec.ts")
    else:
        out_dir = base_out_dir
        pw_root = base_out_dir / 'playwright-test'
        area    = ''

    # Closure of this TC's reachable entities — used to filter outputs so
    # this case's report/pages only include modules it actually uses.
    tc_set = tc_closure(tc['Surrogate'])

    # NOTE: env_vars_used, unmapped_actions, urls etc. are local to this
    # function (defined further down). Each per-TC call gets a fresh set
    # automatically — no manual reset needed.

    # tc and project come in as parameters from the dispatch loop —
    # don't re-bind them here (the original `tc = next(...)` would always
    # pick the FIRST TestCase, defeating multi-TC iteration).
    all_steps = resolve_steps(tc.get('Assocs', {}).get('Items', []))

    # Final buffer_map snapshot (replay all TBox Set Buffer steps in order)
    buffer_map = {}
    for s in all_steps:
        if s.get('type') == 'step' and s.get('module') == 'TBox Set Buffer':
            for a in s.get('actions', []):
                en, v = a.get('explicit_name', ''), a.get('value', '')
                if en and v and not v.startswith('{'):
                    buffer_map[en] = v

    # ──────────────────────────────────────────────────────────────────────────────
    # URL collection + base URL
    # ──────────────────────────────────────────────────────────────────────────────
    urls = set()
    for e in data['Entities']:
        if e['ObjectClass'] != 'XParam': continue
        # Per-TC filter: only collect URLs from XParams in this TC's closure
        # (i.e. attached to module-attributes this case actually touches).
        if e['Surrogate'] not in tc_set: continue
        v = e['Attributes'].get('Value', '')
        if isinstance(v, str):
            if v.startswith('http'): urls.add(v)
            if e['Attributes'].get('Name') == 'SelfHealingData':
                urls.update(re.findall(r'"Value":"(https?://[^"]+)"', v))

    def _root(u):
        m = re.match(r'(https?://[^/]+(?:/[a-z]{2}-[A-Z]{2})?(?:/pro)?)', u)
        return m.group(1) if m else u.split('?')[0]

    b2b  = sorted({_root(u) for u in urls if re.search(r'/[a-z]{2}-[A-Z]{2}/pro\b', u)})
    loc  = sorted({_root(u) for u in urls if re.search(r'/[a-z]{2}-[A-Z]{2}\b', u)})
    base_url = (b2b or loc or sorted({_root(u) for u in urls}) or [''])[0]
    # Track for post-loop shared config (multi-TC). Last seen wins; per-spec
    # test.use({baseURL}) overrides at runtime anyway.
    global _last_base_url
    _last_base_url = base_url or _last_base_url

    # ──────────────────────────────────────────────────────────────────────────────
    # JSON manifest
    # ──────────────────────────────────────────────────────────────────────────────
    def raw_locator(attr_sur):
        if not attr_sur: return {}
        xp = xparams(attr_sur); meta = xparam_meta(attr_sur)
        ctx = relative_ctx(attr_sur); sh = self_healing(attr_sur)
        out = {k: v for k, v in xp.items() if v}
        if meta: out['_meta'] = meta
        if ctx:  out['_parent_relid'] = ctx
        chain = parent_attr_chain(attr_sur)
        if chain:
            out['_parent_chain'] = []
            for s in chain:
                if s not in ents: continue
                pxp = xparams(s)
                entry = {'name': ents[s]['Attributes'].get('Name', ''),
                         'tag':  pxp.get('Tag', '')}
                test_attrs = {k: pxp[k] for k in pxp
                              if k.startswith('attributes_') and any(ta in k for ta in cfg['test_attributes'])}
                if test_attrs: entry['test_attributes'] = test_attrs
                out['_parent_chain'].append(entry)
        if sh:
            out['_self_healing'] = [
                {'name': h['name'], 'value': h['value'], 'weight': round(h['weight'], 3)}
                for h in sorted(sh, key=lambda x: -x['weight'])
            ]
        return out

    def serialize_steps(steps):
        out = []
        for s in steps:
            node = {'type': s['type'], 'name': s.get('name', ''), 'depth': s.get('depth', 0)}
            if s['type'] == 'step':
                node['module'] = s.get('module', '')
                acts = []
                for a in s.get('actions', []):
                    if not a['value'] and not a['primary_locator']: continue
                    act = {'mode': a['mode'],
                           'element': a['element_name'] or a['action_property'] or '',
                           'value': a['value']}
                    if a.get('explicit_name'):
                        act['explicit_name'] = a['explicit_name']
                    if a['action_property']:
                        act['property'] = a['action_property']
                    if a['value_resolved'] != a['value']:
                        act['value_resolved'] = a['value_resolved']
                    if a['primary_locator']:
                        locator = {'primary': a['primary_locator']}
                        if a['fallback_locator']: locator['fallback'] = a['fallback_locator']
                        raw = raw_locator(a.get('attr_sur'))
                        if raw: locator['raw'] = raw
                        act['locator'] = locator
                    if a['notes']:        act['notes'] = a['notes']
                    if a.get('wait_before'): act['wait_before_ms'] = int(a['wait_before'])
                    if a.get('wait_after'):  act['wait_after_ms']  = int(a['wait_after'])
                    acts.append(act)
                node['actions'] = acts
            elif s['type'] == 'if':
                node['condition'] = s.get('condition', '')
                if s.get('condition_resolved') and s['condition_resolved'] != s.get('condition', ''):
                    node['condition_resolved'] = s['condition_resolved']
            elif s['type'] == 'block_start':
                if s.get('parameters'): node['parameters'] = s['parameters']
            out.append(node)
        return out

    # ──────────────────────────────────────────────────────────────────────────────
    # Spec generation — dispatch-or-log
    # ──────────────────────────────────────────────────────────────────────────────
    # unmapped[] collects every action we couldn't translate, surfaced in both
    # the JSON manifest (meta.unmapped) and as TODO comments in the spec.
    unmapped_actions = []

    # env_vars_used is at MODULE scope (see top-level definition near
    # _per_tc_pipeline). Multi-TC runs accumulate vars across all cases so
    # the shared playwright-test/.env.example written post-loop covers
    # the union of every spec's needs. Don't reset here.

    def safe_env(raw_name, hint=''):
        n = re.sub(r'[^A-Z0-9_]', '_', raw_name.upper()).strip('_')
        if n in RESERVED_ENV:
            h = re.sub(r'[^A-Z0-9_]', '_', (hint or 'APP').upper()).strip('_')
            n = (h + '_' + n).lstrip('_')
        env_vars_used.setdefault(n, []).append((raw_name, hint))
        return n

    # Walk every ParameterReference now, capture CP[*] targets, register them as
    # env vars. parse_tsu's flow only registers CP[*] as it walks XTestStepValues
    # during action emission — but URL-style parameters reach the spec only via
    # RTB ParameterLayer indirection (PL[X] → CP[Y]) and miss that path. Surface
    # them up-front so .env.example is complete.
    for _pref in data['Entities']:
        if _pref['ObjectClass'] != 'ParameterReference': continue
        _val = _pref['Attributes'].get('Value', '') or ''
        for _m in re.finditer(r'\{CP\[([^\]]+)\]\}', _val):
            _cp_name = _m.group(1)
            # Hint: which Parameter (in which RTB) this CP feeds
            _param_surs = _pref.get('Assocs', {}).get('Parameter', [])
            _param_hint = ents.get(_param_surs[0], {}).get('Attributes', {}).get('Name', '') if _param_surs else ''
            safe_env(_cp_name, _param_hint)

    def loc_expr(primary, fallback):
        if fallback and primary: return f"({primary}).or({fallback})"
        return primary or ''

    # Existence-style assertion props where appending .first() to a standalone
    # locator is safe — multiple matches just confirm "exists"; .first() avoids
    # strict-mode collisions without changing semantics.
    EXISTENCE_PROPS = {'Visible', 'Hidden', 'Exists', 'Disabled', 'Enabled'}

    def loc_for_assert(primary, fallback, prop=''):
        """Locator expression for `expect(...).matcher()`: appends .first() when
        the chain has an `.or()` fallback (strict-mode hot zone) OR when the
        matcher is existence-style. Other assertions on standalone locators
        keep their original strictness so multi-match issues surface."""
        base = loc_expr(primary, fallback)
        if not base: return base
        if fallback or prop in EXISTENCE_PROPS:
            return f'{base}.first()'
        return base

    def loc_for_action(primary, fallback):
        """Locator expression for `.click()/.fill()/.press()`: appends .first()
        when the chain has `.or()`. Bare locators are left strict — if they
        match multiple, the test should fail loudly so the locator gets fixed."""
        base = loc_expr(primary, fallback)
        if not base: return base
        return f'{base}.first()' if fallback else base

    _KEY_ALIASES = {
        'CTRL':'Control', 'CONTROL':'Control', 'CMD':'Meta', 'META':'Meta',
        'WIN':'Meta', 'OPT':'Alt', 'ALT':'Alt', 'SHIFT':'Shift',
        'DEL':'Delete', 'DELETE':'Delete', 'ESC':'Escape', 'ESCAPE':'Escape',
        'PGUP':'PageUp', 'PGDN':'PageDown', 'INS':'Insert', 'INSERT':'Insert',
        'TAB':'Tab', 'ENTER':'Enter', 'RETURN':'Enter', 'SPACE':' ',
        'BACKSPACE':'Backspace', 'BS':'Backspace',
        'UP':'ArrowUp', 'DOWN':'ArrowDown', 'LEFT':'ArrowLeft', 'RIGHT':'ArrowRight',
        'HOME':'Home', 'END':'End',
    }
    def _norm_key(s):
        """Tosca chord 'Ctrl+A' or 'CONTROL+a' → Playwright 'Control+a'."""
        parts = re.split(r'\s*\+\s*', s)
        out = []
        for p in parts:
            u = p.upper()
            if u in _KEY_ALIASES: out.append(_KEY_ALIASES[u])
            elif len(p) == 1:     out.append(p.lower())
            else:                 out.append(p[0].upper() + p[1:].lower())
        return '+'.join(out)

    # Recognise concatenated Tosca tokens like:
    #   {click}{sendkeys[{B[X]}]}        → click() + fill(X)
    #   {B[X]} {TAB}                     → fill(X) + press('Tab')
    #   {SENDKEYS[hello]}                → fill('hello')
    #   {SENDKEYS["^{a}"]}{SENDKEYS["{DEL}"]}{SENDKEYS[X]}  → press Control+a, Delete, fill X
    _FRAGMENT_RE = re.compile(r'\{([A-Za-z]+)(?:\[([^\]]*)\])?\}|([^{}\s]+)|\s+')

    def _compose_action(val, loc, resolved, catch, action):
        """Try to decompose a composite Tosca value into a sequence of PW calls.
        Returns ('emit', [lines]) | ('todo', reason) | None (caller continues).
        Returns None for plain (non-composite) values so callers handle them."""
        if not val or not loc: return None
        # Skip plain singletons — let regular dispatch handle them
        if TOSCA_REF_RE.match(val) or TOSCA_TOKEN_RE.match(val) or TOSCA_TOKEN_ARG_RE.match(val):
            return None
        # Parse over `resolved` when it differs — embedded {B[X]} are pre-expanded,
        # which avoids regex confusion from nested brackets.
        src = resolved if (resolved and resolved != val and not resolved.startswith('{B[')
                           and not resolved.startswith('{PL[')) else val
        parts = []
        todo_reasons = []
        for m in _FRAGMENT_RE.finditer(src):
            tok, arg, lit = m.group(1), m.group(2), m.group(3)
            if tok:
                up = tok.upper()
                if up in CLICK_TOKENS:
                    parts.append(('click', None))
                elif up in ('SENDKEYS', 'TYPE', 'TEXT', 'TEXTINPUT'):
                    if arg is None: parts.append(('press', 'Backspace'))   # rare
                    else:
                        # Inner content may itself contain {B[X]} or {KEYS}
                        inner = arg.strip().strip('"').strip("'")
                        if (mref := TOSCA_REF_RE.match(inner)):
                            kind, name = mref.group(1), mref.group(2)
                            if kind == 'B' and not resolved.startswith('{'):
                                parts.append(('fill', resolved))
                            elif kind == 'PL':
                                parts.append(('fill_env', name))
                            else:
                                todo_reasons.append(f'sendkeys with {kind}[{name}]')
                                parts.append(('comment', f'sendkeys[{inner}]'))
                        elif inner.startswith('^') or inner.startswith('{'):
                            # ^{a} = Ctrl+A, {DEL} = Delete — Tosca shorthand
                            chord = inner.replace('^','Control+').replace('{','').replace('}','')
                            parts.append(('press', _norm_key(chord) if '+' in chord else chord.title()))
                        else:
                            parts.append(('fill', inner))
                elif up in KEY_TOKENS:
                    parts.append(('press', up.title() if len(up) > 1 else up.lower()))
                elif up in ('KEYPRESS','KEYDOWN','KEYUP','KEY') and arg:
                    parts.append(('press', _norm_key(arg)))
                elif up == 'TAB':
                    parts.append(('press', 'Tab'))
                elif up in ('B','PL','CP','XL'):
                    # {B[X]} matched as a fragment (no surrounding chars). Treat as ref.
                    if up == 'B' and not resolved.startswith('{'):
                        parts.append(('fill', resolved))
                    elif up == 'PL':
                        parts.append(('fill_env', arg))
                    else:
                        todo_reasons.append(f'composite with {up}[{arg}]')
                        parts.append(('comment', f'{up}[{arg}]'))
                else:
                    todo_reasons.append(f'unknown token {{{up}}}')
                    parts.append(('comment', f'{{{up}{("["+arg+"]") if arg else ""}}}'))
            elif lit:
                mref = TOSCA_REF_RE.match(lit)
                if mref:
                    kind, name = mref.group(1), mref.group(2)
                    if kind == 'B' and not resolved.startswith('{'):
                        parts.append(('fill', resolved))
                    elif kind == 'PL':
                        parts.append(('fill_env', name))
                    else:
                        todo_reasons.append(f'composite with {kind}[{name}]')
                        parts.append(('comment', f'{kind}[{name}]'))
                else:
                    parts.append(('fill', lit))
        if not parts: return None
        # Coalesce {KEYDOWN[X]} … {KEYPRESS[Y]} … {KEYUP[X]} sequences into a chord.
        # Marker for keydown/keyup: we tagged them as ('press', X). After parse,
        # a triple where parts[i]=parts[i+2] (modifier) and parts[i+1] is a key,
        # merges into ('press', f'{X}+{Y}').
        coalesced = []
        i = 0
        while i < len(parts):
            if (i + 2 < len(parts) and parts[i][0] == 'press' and parts[i+1][0] == 'press'
                    and parts[i+2][0] == 'press' and parts[i][1] == parts[i+2][1]
                    and parts[i][1] in _KEY_ALIASES.values()):
                coalesced.append(('press', f"{parts[i][1]}+{parts[i+1][1]}"))
                i += 3
            else:
                coalesced.append(parts[i]); i += 1
        parts = coalesced
        out = []
        for kind, arg in parts:
            if kind == 'click':
                out.append(f"await {loc}.click(){catch};")
            elif kind == 'fill':
                out.append(f"await {loc}.fill({json.dumps(arg)}){catch};")
            elif kind == 'fill_env':
                env = safe_env(arg, action.get('element_name', ''))
                out.append(f"await {loc}.fill(process.env.{env} ?? ''){catch};")
            elif kind == 'press':
                out.append(f"await {loc}.press({json.dumps(arg)}){catch};")
            elif kind == 'comment':
                out.append(f"// TODO unmapped fragment: {arg}")
        if todo_reasons and not any(p[0] != 'comment' for p in parts):
            return ('todo', '; '.join(todo_reasons))
        return ('emit', out)

    def gen_action_line(action, step_context=''):
        """Return (line_or_None, unmapped_info_or_None).

        line=None      → action intentionally skipped (NULL value, container, etc.)
        line=str       → emitted Playwright code
        info=dict set  → action could not be mapped; line is a TODO comment and
                         the same dict is appended to unmapped_actions for the
                         manifest's meta.unmapped[].
        """
        mode  = action['mode']
        prop  = action['action_property']
        val   = action['value']
        resolved = action.get('value_resolved', val)
        primary  = action['primary_locator']; fallback = action['fallback_locator']
        notes = action['notes']
        wb, wa = action.get('wait_before', ''), action.get('wait_after', '')

        pfx = ''
        for note in notes: pfx += f'    // NOTE: {note}\n'
        if wb and wb.isdigit():
            pfx += f'    await page.waitForTimeout({wb}); // WaitBefore\n'
        sfx = f'\n    await page.waitForTimeout({wa}); // WaitAfter' if wa and wa.isdigit() else ''
        # Two locator forms — assert-side decides .first() based on prop, action-side
        # always adds .first() when chain has .or() fallback.
        loc        = loc_expr(primary, fallback)               # raw, for compose actions etc.
        loc_act    = loc_for_action(primary, fallback)
        loc_assert = lambda p='': loc_for_assert(primary, fallback, p)

        def line(code): return f"{pfx}    {code}{sfx}" if code else None
        def emit(code): return (line(code), None)
        def skip():     return (None, None)
        def todo(reason):
            info = {
                'step':     step_context,
                'element':  action.get('element_name', ''),
                'mode':     mode,
                'property': prop,
                'value':    val if len(val) <= 200 else val[:200] + '…',
                'reason':   reason,
            }
            unmapped_actions.append(info)
            return (line(f"// TODO unmapped ({reason}): mode={mode} prop={prop!r} val={val!r}"), info)

        # Token parsing — but only if it isn't a Tosca reference (PL/B/CP/XL)
        tok_m   = None
        tok     = ''
        tok_arg = None
        if val and not TOSCA_REF_RE.match(val):
            if (m := TOSCA_TOKEN_ARG_RE.match(val)) and m.group(1).upper() not in ('PL','B','CP','XL'):
                tok_m, tok, tok_arg = m, m.group(1).upper(), m.group(2)
            elif (m := TOSCA_TOKEN_RE.match(val)):
                tok_m, tok, tok_arg = m, m.group(1).upper(), None

        # ── waitFor (ActionMode 101) ─────────────────────────────────────────────
        if mode == 'waitFor':
            if not loc: return skip()
            if prop in ASSERT_PROPS:
                true_a, false_a = ASSERT_PROPS[prop]
                return emit(f"await expect({loc_assert(prop)}).{false_a if val == 'False' else true_a};")
            if prop == 'Count' and val.isdigit():
                return emit(f"await expect({loc_assert(prop)}).toHaveCount({val});")
            if prop == 'Value':
                target = resolved if not resolved.startswith('{') else val
                return emit(f"await expect({loc_assert(prop)}).toHaveValue({json.dumps(target)});")
            if val and val not in ('True', 'False'):
                target = resolved if not resolved.startswith('{') else val
                return emit(f"await expect({loc_assert()}).toContainText({json.dumps(target.strip('*'))});")
            if not val: return skip()
            return todo('unhandled waitFor property')

        # ── verify (ActionMode 69) ───────────────────────────────────────────────
        if mode == 'verify':
            if not loc or not val: return skip()
            if prop in ASSERT_PROPS:
                true_a, false_a = ASSERT_PROPS[prop]
                return emit(f"await expect({loc_assert(prop)}).{false_a if val == 'False' else true_a};")
            if prop == 'Count' and val.isdigit():
                return emit(f"await expect({loc_assert(prop)}).toHaveCount({val});")
            if prop == 'Value':
                target = resolved if not resolved.startswith('{') else val
                return emit(f"await expect({loc_assert(prop)}).toHaveValue({json.dumps(target)});")
            if prop == 'InnerText' or prop == '':
                target = resolved if not resolved.startswith('{') else val
                if target.startswith('{'):
                    return todo('verify InnerText with unresolved value')
                return emit(f"await expect({loc_assert()}).toContainText({json.dumps(target.strip('*'))});")
            return todo(f'unhandled verify property {prop}')

        # ── set / optionalSet / input ────────────────────────────────────────────
        if mode in ('set', 'optionalSet', 'input'):
            catch = '.catch(() => {})' if mode == 'optionalSet' else ''
            if not val or val == '{NULL}': return skip()

            # Token-based interactions
            if tok and not tok_arg:
                if tok in CLICK_TOKENS:
                    call = {'CLICK':'click()', 'DOUBLECLICK':'dblclick()',
                            'RIGHTCLICK':'click({ button: "right" })',
                            'CLICKDOWN':'click({ button: "left" })',
                            'CLICKUP':'click({ button: "left" })'}.get(tok, 'click()')
                    return emit(f"await {loc_act}.{call}{catch};") if loc else skip()
                if tok == 'HOVER':
                    return emit(f"await {loc_act}.hover(){catch};") if loc else skip()
                if tok in ('SCROLL','SCROLLTO','SCROLLINTOVIEW'):
                    return emit(f"await {loc_act}.scrollIntoViewIfNeeded(){catch};") if loc else skip()
                if tok == 'FOCUS':
                    return emit(f"await {loc_act}.focus(){catch};") if loc else skip()
                if tok == 'BLUR':
                    return emit(f"await {loc_act}.blur(){catch};") if loc else skip()
                if tok in KEY_TOKENS:
                    key = tok.title() if len(tok) > 1 else tok.lower()
                    return emit(f"await {loc_act}.press({json.dumps(key)}){catch};") if loc else skip()
                return todo(f'unknown token {{{tok}}}')

            # Token with arg: {KEY[Ctrl+A]}, {SELECT[3]}, {SELECT[Option Name]}
            if tok and tok_arg:
                if tok == 'KEY':
                    return emit(f"await {loc_act}.press({json.dumps(_norm_key(tok_arg))}){catch};") if loc else skip()
                if tok == 'SELECT':
                    if tok_arg.isdigit():
                        return emit(f"await {loc_act}.selectOption({{ index: {int(tok_arg)-1} }}){catch};") if loc else skip()
                    return emit(f"await {loc_act}.selectOption({json.dumps(tok_arg)}){catch};") if loc else skip()
                if tok == 'WAIT' and tok_arg.isdigit():
                    return emit(f"await page.waitForTimeout({int(tok_arg)});")
                return todo(f'unknown token {{{tok}[{tok_arg}]}}')

            if not loc: return skip()

            # Tosca reference values
            m = TOSCA_REF_RE.match(val)
            if m:
                kind, name = m.group(1), m.group(2)
                if kind == 'PL':
                    env = safe_env(name, action.get('element_name', ''))
                    return emit(f"await {loc_act}.fill(process.env.{env} ?? ''){catch};")
                if kind == 'B':
                    if not resolved.startswith('{'):
                        return emit(f"await {loc_act}.fill({json.dumps(resolved)}){catch};")
                    return todo(f'unresolved buffer reference {val}')
                if kind == 'CP':
                    env = safe_env(name)
                    return emit(f"await {loc_act}.fill(process.env.{env} ?? ''){catch};  // from CP[{name}]")
                if kind == 'XL':
                    return todo(f'external-layer reference {val}')

            # Tosca-encrypted credential blob
            if TOSCA_ENCRYPTED_RE.match(val):
                env = safe_env(action.get('element_name', '') or 'CREDENTIAL')
                return emit(f"await {loc_act}.fill(process.env.{env} ?? ''){catch}; // ⚠ Tosca-encrypted")

            # Composite patterns: {click}{sendkeys[…]}, {B[X]} {TAB}, {SENDKEYS[X]}, etc.
            # Decompose into multiple Playwright calls. Returns combined emit or todo.
            composite = _compose_action(val, loc_act, resolved, catch, action)
            if composite is not None:
                kind, payload = composite
                if kind == 'emit':
                    return (f"{pfx}    " + (f"\n    ".join(payload)) + sfx, None)
                if kind == 'todo':
                    return todo(payload)

            # Generic Tosca expression: {CALC[…]}, {TRIM[…]}, {STRINGREPLACE[…]…}
            if TOSCA_EXPR_RE.match(val):
                return todo(f'Tosca expression — needs evaluation')

            # Plain literal
            if not val.startswith('{'):
                return emit(f"await {loc_act}.fill({json.dumps(val)}){catch};")

            return todo('value contains unresolved braces')

        # ── bufferRead (ActionMode 165) ──────────────────────────────────────────
        if mode == 'bufferRead':
            if not loc: return skip()
            var = re.sub(r'[^a-zA-Z0-9]', '_', val.lower()).strip('_') if val else 'captured'
            return (f"{pfx}    const {var} = await {loc_act}.textContent() ?? '';", None)

        return todo(f'unknown ActionMode {mode!r}')

    # ── Build spec via state machine ──────────────────────────────────────────────
    # Run unconditionally so unmapped_actions populates regardless of --playwright.
    _COND_RE = re.compile(r'^\s*"?([^"]*?)"?\s*(==|!=)\s*"?([^"]*?)"?\s*$')
    def eval_cond(cond):
        if not cond: return None
        m = _COND_RE.match(cond)
        if not m: return None
        lhs, op, rhs = m.group(1), m.group(2), m.group(3)
        if any(c in lhs+rhs for c in '{}'): return None
        return (lhs == rhs) if op == '==' else (lhs != rhs)

    tc_name   = tc['Attributes']['Name']
    test_name = re.sub(r'\[\d+\]\s*', '', tc_name).strip()
    spec_lines = [
        "import { test, expect } from '@playwright/test';",
        "",
        "// Auto-generated from Tosca .tsu — generic scaffold.",
        "// Treat this as a starting point: app-specific helpers (cookie banners,",
        "// spinners, login flows) are not auto-injected. Add them in this file.",
        f"// Test case: {tc_name}",
        f"// Project:   {project['Attributes']['Name']}  (Revision {tc['Attributes'].get('Revision','')})",
        "",
        # Pin the baseURL inside the spec — each .tsu was recorded against a
        # specific environment (often differs across test cases in the same project).
        # test.use() overrides the global playwright.config.ts default for this
        # file only, so the spec is self-contained.
        f"test.use({{ baseURL: process.env.BASE_URL || {json.dumps(base_url)} }});",
        "",
        f"test({json.dumps(test_name)}, async ({{ page }}) => {{",
        "  await page.goto('/');",
        "",
    ]
    if buffer_map:
        spec_lines.append("  // Test data (resolved from Tosca TBox Set Buffer preconditions)")
        for k, v in buffer_map.items():
            spec_lines.append(f"  const {re.sub(r'[^a-zA-Z0-9]', '_', k)} = {json.dumps(v)};")
        spec_lines.append("  void [" + ", ".join(re.sub(r'[^a-zA-Z0-9]', '_', k) for k in buffer_map) + "];")
        spec_lines.append("")

    depth = 0
    step_name = None
    step_body = []
    elide_stack = []

    def flush():
        nonlocal step_name, step_body
        if step_name is None: return
        if step_body:
            spec_lines.append(f"  await test.step({json.dumps(step_name)}, async () => {{")
            spec_lines.extend(step_body)
            spec_lines.append("  });")
        else:
            spec_lines.append(f"  // [{step_name}] – no Playwright actions generated")
        spec_lines.append("")
        step_name = None; step_body = []

    def in_elided_branch(): return any(b == 'SUPPRESS' for b in elide_stack)

    def _mq(s):
        """Quote a name for embedding in a single-line @tosca marker. Strips
        newlines, escapes backslashes/double-quotes."""
        if s is None: return ''
        return str(s).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


    for s in all_steps:
        t = s['type']; nm = s.get('name', '')

        if t in ('folder','block_start'):
            depth += 1
            kind = 'folder' if t == 'folder' else 'block'
            if depth == 1:
                flush()
                step_name = nm
                step_body = []
                # Top-level: emitted as await test.step(name, ...). We still drop a
                # marker so the reverse parser unambiguously knows what kind.
                step_body.append(f'    // @tosca {kind}: "{_mq(nm)}"')
                if t == 'block_start' and s.get('parameters'):
                    params = ', '.join(f"{k}={v}" for k, v in list(s['parameters'].items())[:5])
                    step_body.append(f"    // Block parameters: {params}")
            elif not in_elided_branch():
                step_body.append(f'    // @tosca {kind}: "{_mq(nm)}"')
                step_body.append(f"    // ── {nm}")

        elif t in ('folder_end','block_end'):
            kind = 'folder' if t == 'folder_end' else 'block'
            if depth > 1 and not in_elided_branch():
                step_body.append(f'    // @tosca /{kind}')
            if depth == 1:
                step_body.append(f'    // @tosca /{kind}')
                flush()
            depth = max(0, depth - 1)

        elif t == 'if':
            cond = s.get('condition_resolved') or s.get('condition','')
            verdict = eval_cond(cond)
            if not in_elided_branch():
                step_body.append(f'    // @tosca if: "{_mq(nm)}" cond="{_mq(cond)}"'
                                 + (f' verdict={verdict}' if verdict is not None else ''))
                step_body.append(f"    // [if {nm}] condition: {cond}"
                                 + (f"  → {verdict}" if verdict is not None else ""))
            elide_stack.append({'verdict': verdict})

        elif t == 'if_end':
            if elide_stack: elide_stack.pop()
            if not in_elided_branch():
                step_body.append(f'    // @tosca /if')
                step_body.append(f"    // [end if {nm}]")

        elif t in ('then_start','else_start','loop_start'):
            branch = t.split('_')[0]
            if elide_stack and isinstance(elide_stack[-1], dict):
                verdict = elide_stack[-1]['verdict']
                if verdict is True and branch == 'else':
                    elide_stack.append('SUPPRESS')
                elif verdict is False and branch == 'then':
                    elide_stack.append('SUPPRESS')
                else:
                    elide_stack.append('KEEP')
                    if not in_elided_branch():
                        step_body.append(f'    // @tosca {branch}')
                        step_body.append(f"    // [{branch.title()}]")

        elif t in ('then_end','else_end','loop_end'):
            if elide_stack and elide_stack[-1] in ('SUPPRESS','KEEP'):
                kind = elide_stack[-1]
                elide_stack.pop()
                if kind == 'KEEP' and not in_elided_branch():
                    step_body.append(f'    // @tosca /branch')

        elif t == 'step' and not in_elided_branch():
            mod = s.get('module', '')
            if mod == 'TBox Wait':
                dur = next((a['value'] for a in s.get('actions', [])
                            if a.get('element_name') == 'Duration' and (a.get('value') or '').isdigit()), None)
                if dur:
                    step_body.append(f'    // @tosca wait: {dur} name="{_mq(nm)}"')
                    step_body.append(f"    // TBox Wait: {dur} ms")
                    step_body.append(f"    await page.waitForTimeout({dur});")
                continue
            if mod in cfg['skip_modules']:
                # Emit a marker so the reverse parser sees the step (no action lines
                # — those modules carry framework operations that don't map to
                # Playwright code anyway).
                step_body.append(f'    // @tosca step: "{_mq(nm)}" module="{_mq(mod)}"')
                if mod == 'TBox Set Buffer':
                    acts_serial = []
                    for a in s.get('actions', []):
                        en = a.get('explicit_name', '') or ''
                        val = a.get('value', '')
                        if en:
                            acts_serial.append(f'{en}={_mq(val)}')
                    if acts_serial:
                        step_body.append(f'    // @tosca buffers: ' + '; '.join(acts_serial))
                continue
            step_body.append(f'    // @tosca step: "{_mq(nm)}" module="{_mq(mod)}"')
            step_body.append(f"    // {nm}")
            for a in s.get('actions', []):
                ln, _info = gen_action_line(a, step_context=nm)
                if ln: step_body.append(ln)

    flush()
    spec_lines += ["});", ""]

    # ──────────────────────────────────────────────────────────────────────────────
    # JSON manifest
    # ──────────────────────────────────────────────────────────────────────────────
    if gen_json:
        manifest = {
            'meta': {
                'test_name':   tc['Attributes']['Name'],
                'project':     project['Attributes']['Name'],
                'revision':    tc['Attributes'].get('Revision', ''),
                'surrogate':   tc['Surrogate'],
                'base_url':    base_url,
                'source_file': tsu_path.name,
                'all_urls':    sorted(urls),
                'config':      cfg,
                'unmapped':    unmapped_actions,
            },
            'test_data': buffer_map,
            'steps':     serialize_steps(all_steps),
        }
        # Multi-TC: per-case JSON named just steps.json inside cases/<tc_stem>/
        # Single-TC: legacy <stem>_steps.json next to .tsu's out dir.
        json_path = out_dir / ('steps.json' if multi_tc
                                else (tsu_path.stem + '_steps.json'))
        json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"[✓] Steps JSON   → {json_path.relative_to(base_out_dir) if multi_tc else json_path.name}  ({len(unmapped_actions)} unmapped action(s))")

    # ──────────────────────────────────────────────────────────────────────────────
    # HTML report
    # ──────────────────────────────────────────────────────────────────────────────
    def esc(t): return html_mod.escape(str(t)) if t else ''

    ACTION_INFO = {'verify':('b-verify','Verify'),'set':('b-set','Set'),
                   'input':('b-input','Input'),'optionalSet':('b-optional','Optional'),
                   'waitFor':('b-wait','Wait'),'bufferRead':('b-other','Read')}
    SEVERITY_ORDER = ['dynamic-xpath','angular-class','fragile-parent','long-innertext',
                      'cdn-src','class-only','generic-heading','custom-tag','custom-parent']
    BADGE_INFO = {
        'dynamic-xpath':  ('danger','&#128308; Dynamic XPath'),
        'angular-class':  ('danger','&#128308; Angular auto-class'),
        'fragile-parent': ('danger','&#128308; Fragile parent scope'),
        'long-innertext': ('warn',  '&#128992; Long InnerText'),
        'cdn-src':        ('warn',  '&#128992; CDN Src URL'),
        'class-only':     ('warn',  '&#128992; Class-only locator'),
        'generic-heading':('warn',  '&#128992; Generic heading'),
        'custom-tag':     ('info',  '&#128309; Custom web component'),
        'custom-parent':  ('info',  '&#128309; Custom parent scope'),
    }

    def locator_block(attr_sur):
        if not attr_sur: return ''
        xp = xparams(attr_sur); ctx = relative_ctx(attr_sur); sh = self_healing(attr_sur)
        rows = ''.join('<tr><td class="ln">'+esc(n)+'</td><td class="lv">'+esc(v)+'</td></tr>'
                       for n, v in xp.items() if v)
        chain = parent_attr_chain(attr_sur)
        chain_html = ''
        if chain:
            chain_html = '<div class="ctx-row">↑ parent: ' + ' &raquo; '.join(
                '<span class="ctxv">'+esc(ents[s]['Attributes'].get('Name',''))+'</span>'
                for s in chain if s in ents) + '</div>'
        sh_rows = ''.join(
            '<tr><td class="ln">'+esc(h['name'])+'</td><td class="lv">'+esc(h['value'])+'</td>'
            '<td class="lw">'+str(round(h['weight'], 2))+'</td></tr>'
            for h in sorted(sh, key=lambda x: -x['weight'])
            if h['value'] not in ('<No label associated>', ''))
        out = ''
        if rows: out += '<table class="lt"><tbody>'+rows+'</tbody></table>'
        if chain_html: out += chain_html
        if sh_rows:
            out += ('<details class="shd"><summary>Self-healing ('+str(len(sh))+')</summary>'
                    '<table class="lt"><thead><tr><th>Property</th><th>Value</th><th>W</th></tr></thead>'
                    '<tbody>'+sh_rows+'</tbody></table></details>')
        return out or '<span class="no-locs">—</span>'

    def render_steps_html(steps):
        out = ''
        for s in steps:
            t = s['type']
            if t == 'folder':
                out += ('<div class="folder"><div class="fhdr"><span>&#128193;</span>'
                        '<span class="fn">'+esc(s['name'])+'</span></div><div class="fb">')
            elif t == 'folder_end':
                out += '</div></div>'
            elif t == 'block_start':
                param_str = ''
                if s.get('parameters'):
                    param_str = ' <span class="fb-params">[' + ', '.join(
                        f"{esc(k)}={esc(v)}" for k,v in list(s['parameters'].items())[:4]
                    ) + (']' if len(s['parameters']) <= 4 else ', …]') + '</span>'
                out += ('<div class="folder fblock"><div class="fhdr"><span>&#128279;</span>'
                        '<span class="fn">'+esc(s['name'])+'</span>'
                        '<span class="fb-badge">reusable block</span>'+param_str+'</div><div class="fb">')
            elif t == 'block_end':
                out += '</div></div>'
            elif t == 'if':
                cr = s.get('condition_resolved', '')
                cr_html = '<span class="cf-resolved">→ '+esc(cr)+'</span>' if cr and cr != s.get('condition','') else ''
                out += ('<div class="folder fcf"><div class="fhdr"><span>&#9881;</span>'
                        '<span class="fn">'+esc(s['name'])+'</span>'
                        '<span class="cf-cond">'+esc(s.get('condition',''))+'</span>'+cr_html+'</div><div class="fb">')
            elif t == 'if_end':
                out += '</div></div>'
            elif t in ('then_start','else_start','loop_start'):
                out += ('<div class="cf-branch cf-'+t.split('_')[0]+'">'
                        '<div class="cf-branch-hdr">'+esc(s.get('name',t.split('_')[0].title()))+'</div>')
            elif t in ('then_end','else_end','loop_end'):
                out += '</div>'
            elif t == 'step':
                rows = ''
                for a in s['actions']:
                    if not a['value']: continue
                    mc, ml = ACTION_INFO.get(a['mode'], ('b-other', a['mode']))
                    attr_n = esc(a['element_name'] or a['action_property'] or '—')
                    val_html = '<span class="svv">'+esc(a['value'])
                    if a['value_resolved'] != a['value']:
                        val_html += '<span class="svr"> → '+esc(a['value_resolved'])+'</span>'
                    val_html += '</span>'
                    pw_html = ''
                    if a['primary_locator']:
                        pw_html = '<div class="pw-loc"><span class="pw-badge">PW</span><code>'+esc(a['primary_locator'])+'</code></div>'
                        if a['fallback_locator']:
                            pw_html += '<div class="pw-loc pw-fb"><span class="pw-badge pw-fb-badge">fallback</span><code>'+esc(a['fallback_locator'])+'</code></div>'
                    for note in a['notes']:
                        pw_html += '<div class="pw-note">'+esc(note)+'</div>'
                    rows += ('<div class="svr"><div class="svh">'
                             '<span class="badge '+mc+'">'+ml+'</span>'
                             '<span class="sva">'+attr_n+'</span>'
                             '<span class="svarrow">&#8594;</span>'+val_html
                             +'</div>'+(('<div class="svl">'+pw_html+'</div>') if pw_html else '')+'</div>')
                mod_html = ('<span class="sm">'+esc(s['module'])+'</span>') if s['module'] else ''
                out += ('<div class="step"><div class="step-hdr"><span class="si">&#9654;</span>'
                        '<span class="sn">'+esc(s['name'])+'</span>'+mod_html+'</div>'
                        +(('<div class="step-body">'+rows+'</div>') if rows else '')+'</div>')
        return out

    # challenge analysis
    challenges = []
    for ent in data['Entities']:
        if ent['ObjectClass'] != 'XModuleAttribute': continue
        attr_sur = ent['Surrogate']
        attr_name = ent['Attributes'].get('Name', '')
        mod = attr_top_module(attr_sur)
        if not mod: continue
        mod_name = mod['Attributes'].get('Name', '')
        xp  = xparams(attr_sur); ctx = relative_ctx(attr_sur)
        tag = xp.get('Tag', ''); id_ = xp.get('Id', ''); cls = xp.get('ClassName', '')
        inner = xp.get('InnerText', ''); xpath = xp.get('XPath', ''); src = xp.get('Src', '')
        issues = []
        has_dtid = any(xp.get(f'attributes_{ta}') for ta in cfg['test_attributes'])
        if has_dtid: continue   # data-test-id elements aren't challenges
        if tag and tag not in STANDARD_TAGS:
            issues.append(('custom-tag','Custom web component: <'+tag+'>','Non-standard HTML element.'))
        ctx_tag = ctx.get('Tag', ''); ctx_inner = ctx.get('InnerText', '')
        if ctx_tag and ctx_tag not in STANDARD_TAGS:
            issues.append(('custom-parent','Scoped inside: <'+ctx_tag+'>','Custom parent container.'))
        if ctx_tag == 'DIV' and ctx_inner:
            issues.append(('fragile-parent','Parent DIV by InnerText','Sibling text change will break parent scoping.'))
        if xpath and DYNAMIC_XPATH_RE.search(xpath):
            issues.append(('dynamic-xpath','Dynamic PrimNG XPath','pn_id_* changes on re-render.'))
        if cls and ANGULAR_CLASS_RE.search(cls):
            ng = ANGULAR_CLASS_RE.search(cls).group()
            issues.append(('angular-class','Angular auto-class: '+ng,'Changes every build.'))
        if inner and len(inner) > 60 and not id_ and not xpath:
            issues.append(('long-innertext','Long InnerText locator','Content change breaks this.'))
        if src and 'kc-usercontent.com' in src:
            issues.append(('cdn-src','CDN image Src','Asset URL changes on publish.'))
        if not id_ and not xpath and not src and tag in STANDARD_TAGS and cls and not inner:
            if not ANGULAR_CLASS_RE.search(cls):
                issues.append(('class-only','ClassName-only locator','Class may change on framework upgrade.'))
        if tag in ('H2','H3','H4','H1','H5') and not id_ and not inner and not xpath:
            issues.append(('generic-heading','Generic <'+tag+'> – no text or ID','Matches first heading in scope.'))
        if issues:
            challenges.append({'module':mod_name,'element':attr_name,'tag':tag,'id':id_,
                               'cls':cls,'inner':inner,'xpath':xpath,'ctx':ctx,'issues':issues})

    def sev(c):
        codes = [i[0] for i in c['issues']]
        return min((SEVERITY_ORDER.index(cd) if cd in SEVERITY_ORDER else 99) for cd in codes)
    challenges.sort(key=sev)
    type_counts = Counter(i[0] for c in challenges for i in c['issues'])
    summary_chips = ''.join('<span class="summary-chip">'+BADGE_INFO.get(code,('','&#x26A0; '+code))[1]+' &times; '+str(cnt)+'</span>'
                             for code, cnt in sorted(type_counts.items(), key=lambda x: -x[1]))

    challenges_html = ''
    for c in challenges:
        locs = []
        if c['tag']:   locs.append('tag=<b>'+esc(c['tag'])+'</b>')
        if c['id']:    locs.append('id=<b>'+esc(c['id'])+'</b>')
        if c['xpath']: locs.append('xpath=<b>'+esc(c['xpath'][:60])+'</b>')
        if c['inner']: locs.append('text=<b>'+esc(c['inner'][:60])+('...' if len(c['inner'])>60 else '')+'</b>')
        if c['cls']:   locs.append('class=<b>'+esc(c['cls'][:70])+('...' if len(c['cls'])>70 else '')+'</b>')
        ctx_parts = ' '.join('<span class="ctxk">'+esc(k)+'</span>=<span class="ctxv">'+esc(v)+'</span>' for k,v in c['ctx'].items())
        iss_html = ''.join('<div class="issue issue-'+BADGE_INFO.get(i[0],('info',''))[0]+'">'
                           '<div class="issue-label">'+BADGE_INFO.get(i[0],('','⚠'))[1]+'</div>'
                           '<div class="issue-short">'+esc(i[1])+'</div>'
                           '<div class="issue-detail">'+esc(i[2])+'</div></div>' for i in c['issues'])
        challenges_html += ('<div class="chal-card"><div class="chal-header">'
                            '<div class="chal-mod">'+esc(c['module'])+'</div>'
                            '<div class="chal-elem">'+esc(c['element'])+'</div>'
                            '<div class="chal-locs">'+' | '.join(locs)+'</div>'
                            +(('<div class="chal-ctx">Parent: '+ctx_parts+'</div>') if ctx_parts else '')
                            +'</div><div class="chal-issues">'+iss_html+'</div></div>')

    def render_modules_html():
        out = []
        for ent in data['Entities']:
            if ent['ObjectClass'] != 'XModule': continue
            # Per-TC filter: only show modules this case actually uses.
            if ent['Surrogate'] not in tc_set: continue
            mn = ent['Attributes'].get('Name', '')
            itype = {'0':'Action','1':'HTML'}.get(str(ent['Attributes'].get('InterfaceType', '')), '')
            attrs = module_attrs.get(ent['Surrogate'], [])
            elems = ''.join('<div class="ec"><div class="en">'+esc(a['Attributes'].get('Name',''))+'</div>'
                            '<div class="el">'+locator_block(a['Surrogate'])+'</div></div>' for a in attrs)
            out.append('<div class="mc"><div class="mhdr"><span class="mn">'+esc(mn)+'</span>'
                       '<span class="mb">'+esc(itype)+'</span></div>'
                       '<div class="mb2">'+(elems or '<div class="empty">No elements</div>')+'</div></div>')
        return ''.join(out)

    params_html = ''
    seen = set()
    for ent in data['Entities']:
        if ent['ObjectClass'] != 'Parameter': continue
        nm = ent['Attributes'].get('Name', ''); dv = ent['Attributes'].get('DefaultValue', '')
        if nm in seen: continue
        seen.add(nm)
        params_html += '<span class="pill"><span class="pk">'+esc(nm)+'</span><span class="pv">'+(esc(dv) or '&mdash;')+'</span></span>'
    urls_html = ''.join('<a class="up" href="'+esc(u)+'" target="_blank">'+esc(u)+'</a>' for u in sorted(urls))

    CSS = """
    :root{--bg:#0f1117;--s1:#1a1d26;--s2:#22263a;--bd:#2e3350;--ac:#5b8dee;--gn:#3ecf8e;--or:#f7a23a;--rd:#e05252;--pu:#9b6dff;--tx:#d0d4e8;--mu:#7a80a0;--co:#a8b4e8;--r:8px}
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:var(--bg);color:var(--tx);font-family:"Inter","Segoe UI",sans-serif;font-size:13px;line-height:1.6}
    a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
    .ph{background:linear-gradient(135deg,#1a1d26,#12162b);border-bottom:1px solid var(--bd);padding:24px 32px}
    .pl{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--mu);margin-bottom:4px}
    .pt{font-size:20px;font-weight:700;color:#fff;margin-bottom:8px}
    .pm{display:flex;gap:14px;flex-wrap:wrap}.pmi{font-size:11px;color:var(--mu)}.pmi strong{color:var(--tx)}
    .nav{display:flex;background:var(--s2);border-bottom:1px solid var(--bd);padding:0 32px}
    .nav a{padding:12px 18px;font-size:12px;color:var(--mu);border-bottom:3px solid transparent;display:block}
    .nav a:hover{color:var(--tx);text-decoration:none}.nav a.active{color:var(--ac);border-color:var(--ac)}
    .wrap{max-width:1280px;margin:0 auto;padding:24px 32px}
    .tab-panel{display:none}.tab-panel.active{display:block}
    .sec{margin-bottom:32px}
    .st{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--mu);border-bottom:1px solid var(--bd);padding-bottom:7px;margin-bottom:14px}
    .pill{display:inline-flex;align-items:center;gap:6px;background:var(--s2);border:1px solid var(--bd);border-radius:16px;padding:4px 12px;margin:0 6px 6px 0}
    .pk{color:var(--ac);font-weight:600}.pv{color:var(--mu);font-size:11px}
    .up{display:block;background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:6px 12px;margin-bottom:5px;font-family:monospace;font-size:12px;color:var(--co)}
    .up:hover{border-color:var(--ac)}
    .summary-chips{margin-bottom:18px;display:flex;flex-wrap:wrap;gap:8px}
    .summary-chip{font-size:12px;padding:4px 12px;background:var(--s2);border:1px solid var(--bd);border-radius:16px;color:var(--tx)}
    .chal-card{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);margin-bottom:12px;overflow:hidden}
    .chal-header{background:var(--s2);padding:10px 14px;border-bottom:1px solid var(--bd)}
    .chal-mod{font-size:11px;color:var(--mu);margin-bottom:2px}
    .chal-elem{font-size:14px;font-weight:700;color:#fff;margin-bottom:4px}
    .chal-locs{font-family:monospace;font-size:11px;color:var(--co)}.chal-locs b{color:#fff}
    .chal-ctx{font-size:11px;color:var(--mu);margin-top:2px}.ctxk{color:var(--mu)}.ctxv{color:var(--or)}
    .chal-issues{padding:10px 14px;display:flex;flex-direction:column;gap:8px}
    .issue{border-radius:6px;padding:8px 12px}.issue-label{font-weight:700;font-size:12px;margin-bottom:3px}
    .issue-short{font-weight:600;font-size:12px;color:#fff;margin-bottom:2px}.issue-detail{font-size:12px;color:var(--mu)}
    .issue-danger{background:#2a1a1a;border:1px solid #5a2a2a}.issue-danger .issue-label{color:var(--rd)}
    .issue-warn{background:#2a2410;border:1px solid #5a4a10}.issue-warn .issue-label{color:var(--or)}
    .issue-info{background:#1a1a2a;border:1px solid #2a2a4a}.issue-info .issue-label{color:var(--ac)}
    .folder{border:1px solid var(--bd);border-radius:var(--r);margin-bottom:8px;overflow:hidden}
    .fhdr{background:var(--s2);padding:9px 13px;display:flex;align-items:center;gap:8px;font-weight:600;color:#fff}
    .fn{flex:1}.fc{font-size:11px;color:var(--mu);background:var(--bd);padding:1px 8px;border-radius:10px}
    .fb{padding:10px 13px;background:var(--s1)}
    .fblock .fhdr{background:#162030;border-left:3px solid var(--ac)}
    .fcf .fhdr{background:#1f1830;border-left:3px solid var(--pu)}
    .fb-badge{font-size:10px;color:var(--ac);background:#0f1e2e;border:1px solid #1a3050;padding:1px 7px;border-radius:10px}
    .fb-params{font-size:10px;color:var(--mu);font-family:monospace}
    .cf-cond{font-size:11px;color:var(--or);font-family:monospace;background:#241a10;padding:1px 6px;border-radius:4px}
    .cf-resolved{font-size:11px;color:var(--gn);font-family:monospace}
    .cf-branch{margin:6px 0;border-left:2px solid #2a2a4a;padding-left:10px}
    .cf-branch-hdr{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mu);margin-bottom:4px}
    .cf-then{border-color:var(--gn)}.cf-then .cf-branch-hdr{color:var(--gn)}
    .cf-else{border-color:var(--rd)}.cf-else .cf-branch-hdr{color:var(--rd)}
    .cf-loop{border-color:var(--or)}.cf-loop .cf-branch-hdr{color:var(--or)}
    .empty{color:var(--mu);font-style:italic;font-size:12px}
    .step{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);margin-bottom:7px;overflow:hidden}
    .step-hdr{padding:8px 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;background:var(--s2)}
    .si{color:var(--ac);font-size:11px}.sn{font-weight:600;color:#fff;flex:1}
    .sm{font-size:11px;color:var(--mu);background:var(--s1);border:1px solid var(--bd);padding:1px 7px;border-radius:4px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .step-body{padding:9px 12px}
    .svr{margin-bottom:8px;border:1px solid var(--bd);border-radius:6px;overflow:hidden}
    .svh{display:flex;align-items:center;gap:7px;padding:6px 10px;background:var(--s2);flex-wrap:wrap}
    .sva{font-weight:600;color:var(--co)}.svarrow{color:var(--mu)}
    .svv{font-family:monospace;color:#fff;background:var(--bg);padding:1px 6px;border-radius:4px}
    .svv .svr{color:var(--gn)}
    .svl{padding:7px 10px}
    .badge{font-size:11px;padding:2px 8px;border-radius:12px;font-weight:600;white-space:nowrap}
    .b-verify{background:#1a3a2a;color:var(--gn);border:1px solid #2a5a3a}
    .b-set{background:#2a2a0a;color:var(--or);border:1px solid #4a4a1a}
    .b-input{background:#1a1a3a;color:var(--ac);border:1px solid #2a2a5a}
    .b-optional{background:#2a1a3a;color:var(--pu);border:1px solid #4a2a5a}
    .b-wait{background:#2a1a3a;color:var(--pu);border:1px solid #4a2a5a}
    .b-other{background:var(--s2);color:var(--mu);border:1px solid var(--bd)}
    .lt{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:3px}
    .lt td,.lt th{padding:4px 8px;border-top:1px solid var(--bd);vertical-align:top;text-align:left}
    .lt thead tr{background:#1e2038}.lt th{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mu);font-weight:600}
    .ln{color:var(--ac);font-family:monospace;width:160px;white-space:nowrap}
    .lv{color:var(--tx);font-family:monospace;word-break:break-all}
    .lw{color:var(--mu);width:44px;text-align:right;font-size:11px}
    .no-locs{color:var(--mu);font-style:italic;font-size:12px}
    .ctx-row{font-size:11px;color:var(--mu);padding:4px 2px;margin-top:3px}
    .shd{margin-top:4px}.shd summary{font-size:11px;color:var(--mu);cursor:pointer;padding:3px}.shd summary:hover{color:var(--tx)}
    .pw-loc{font-size:11px;font-family:monospace;padding:3px 4px;display:flex;align-items:flex-start;gap:6px;margin-bottom:3px}
    .pw-badge{background:#1a2a4a;color:var(--ac);border:1px solid #2a3a6a;padding:1px 5px;border-radius:4px;font-size:10px;white-space:nowrap;flex-shrink:0}
    .pw-fb .pw-fb-badge{background:#2a1a3a;color:var(--pu);border-color:#4a2a5a}
    .pw-loc code{color:var(--co);word-break:break-all}
    .pw-note{font-size:11px;color:var(--or);padding:3px 4px}
    .mg{display:grid;grid-template-columns:repeat(auto-fill,minmax(500px,1fr));gap:16px}
    .mc{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden}
    .mhdr{background:linear-gradient(90deg,#1e2040,#1a1d26);padding:10px 13px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--bd)}
    .mn{font-weight:700;color:#fff;flex:1}.mb{font-size:11px;padding:2px 7px;border-radius:12px;background:var(--s2);color:var(--mu);border:1px solid var(--bd)}
    .mb2{padding:10px 13px}
    .ec{border:1px solid var(--bd);border-radius:6px;margin-bottom:8px;overflow:hidden}
    .en{background:var(--s2);padding:6px 10px;font-weight:600;color:var(--ac);font-size:12px;border-bottom:1px solid var(--bd)}
    .el{padding:7px 10px}
    """

    flow_html = render_steps_html(all_steps)
    HTML = ('<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            '<title>Tosca – '+esc(tc["Attributes"]["Name"])+'</title>'
            '<style>'+CSS+'</style></head><body>'
            '<div class="ph">'
            '  <div class="pl">Tosca &middot; '+esc(project["Attributes"]["Name"])+'</div>'
            '  <div class="pt">'+esc(tc["Attributes"]["Name"])+'</div>'
            '  <div class="pm">'
            '    <div class="pmi">Revision <strong>'+esc(tc["Attributes"].get("Revision",""))+'</strong></div>'
            '    <div class="pmi" style="font-family:monospace;font-size:11px">'+esc(tc["Surrogate"])+'</div>'
            '  </div>'
            '</div>'
            '<nav class="nav">'
            '  <a href="#" class="active" onclick="showTab(\'challenges\',this);return false">&#9888; Locator Challenges ('+str(len(challenges))+')</a>'
            '  <a href="#" onclick="showTab(\'flow\',this);return false">&#9654; Execution Flow + Playwright Locators</a>'
            '  <a href="#" onclick="showTab(\'modules\',this);return false">&#129513; Module Catalogue</a>'
            '  <a href="#" onclick="showTab(\'meta\',this);return false">&#8505; Meta</a>'
            '</nav>'
            '<div class="wrap">'
            '  <div id="tab-challenges" class="tab-panel active">'
            '    <div class="sec"><div class="st">Locator Risk Analysis</div>'
            '      <div class="summary-chips">'+summary_chips+'</div>'
            '      '+challenges_html+''
            '    </div>'
            '  </div>'
            '  <div id="tab-flow" class="tab-panel">'
            '    <div class="sec"><div class="st">Execution Flow</div>'+flow_html+'</div>'
            '  </div>'
            '  <div id="tab-modules" class="tab-panel">'
            '    <div class="sec"><div class="st">Module Catalogue</div>'
            '      <div class="mg">'+render_modules_html()+'</div>'
            '    </div>'
            '  </div>'
            '  <div id="tab-meta" class="tab-panel">'
            '    <div class="sec"><div class="st">Parameters</div>'+params_html+'</div>'
            '    <div class="sec"><div class="st">URLs</div>'+urls_html+'</div>'
            '  </div>'
            '</div>'
            '<script>'
            'function showTab(id,el){'
            '  document.querySelectorAll(\'.tab-panel\').forEach(p=>p.classList.remove(\'active\'));'
            '  document.querySelectorAll(\'.nav a\').forEach(a=>a.classList.remove(\'active\'));'
            '  document.getElementById(\'tab-\'+id).classList.add(\'active\');'
            '  el.classList.add(\'active\');'
            '}'
            '</script></body></html>')
    report_path = out_dir / ('report.html' if multi_tc
                              else (tsu_path.stem + '_report.html'))
    report_path.write_text(HTML, encoding='utf-8')
    print(f"[✓] HTML report  → {report_path.relative_to(base_out_dir) if multi_tc else report_path.name}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Playwright scaffold (mechanical; uses control-flow walking, no app helpers)
    # ──────────────────────────────────────────────────────────────────────────────
    if not gen_pw:
        print("    (add --playwright to also generate Playwright project files)")
        return  # Per-TC pipeline done; let the dispatch loop continue with next TC

    # Multi-TC: pw_dir is shared across cases (pw_root, set at top of function);
    # tests live in tests/<area>/<tc>.spec.ts. Single-TC: pw_dir = out_dir/playwright-test.
    pw_dir = pw_root
    (pw_dir / 'tests').mkdir(parents=True, exist_ok=True)
    (pw_dir / 'pages').mkdir(parents=True, exist_ok=True)

    def to_camel(s): return re.sub(r'[^a-zA-Z0-9]', ' ', s).title().replace(' ', '')
    def to_snake(s):
        s = re.sub(r'[^a-zA-Z0-9]+', '_', s).strip('_').lower()
        return re.sub(r'_+', '_', s)

    # Generate page object per module
    page_object_files = {}
    for ent in data['Entities']:
        if ent['ObjectClass'] != 'XModule': continue
        # Per-TC filtering: skip XModules this TC doesn't actually use.
        # tc_set is the transitive closure built earlier; for multi-TC mode
        # this trims each case's pages/ to only the modules it touches.
        if ent['Surrogate'] not in tc_set: continue
        mod_name = ent['Attributes'].get('Name', '')
        if not mod_name or mod_name in cfg['skip_modules']: continue
        elem_locs = {}
        for attr in module_attrs.get(ent['Surrogate'], []):
            attr_name = attr['Attributes'].get('Name', '')
            p, fb, notes = build_locator(attr['Surrogate'])
            if p: elem_locs[attr_name] = (p, fb, notes)
        if not elem_locs: continue
        class_name = to_camel(mod_name) + 'Page'
        lines = ["import { type Page, type Locator } from '@playwright/test';", "",
                 f"export class {class_name} {{", "  readonly page: Page;", ""]
        seen_props = set()
        for elem_name in elem_locs:
            prop = to_snake(elem_name)
            if not prop or prop in seen_props: continue
            seen_props.add(prop)
            lines.append(f"  readonly {prop}: Locator;")
        lines += ["", f"  constructor(page: Page) {{", "    this.page = page;"]
        seen_props = set()
        for elem_name, (primary, fallback, notes) in elem_locs.items():
            prop = to_snake(elem_name)
            if not prop or prop in seen_props: continue
            seen_props.add(prop)
            for note in notes: lines.append(f"    // NOTE: {note}")
            if fallback:
                lines.append(f"    this.{prop} = {primary}")
                lines.append(f"      .or({fallback});")
            else:
                lines.append(f"    this.{prop} = {primary};")
        lines += ["  }", "}", ""]
        fname = to_snake(mod_name) + '.page.ts'
        page_object_files[mod_name] = (fname, class_name, '\n'.join(lines))

    for mod_name, (fname, cls_name, content) in page_object_files.items():
        fpath = pw_dir / 'pages' / fname
        if not fpath.exists() or force:
            fpath.write_text(content, encoding='utf-8')
            print(f"[✓] Page object  → {fpath.name}  ({cls_name})")

    # playwright.config.ts — baseURL is env-overridable; per-spec test.use({baseURL})
    # overrides this default for individual specs (each .tsu pins its own URL).
    config_content = f"""import {{ defineConfig, devices }} from '@playwright/test';
    import 'dotenv/config';

    export default defineConfig({{
      testDir: './tests',
      fullyParallel: true,
      timeout: 300_000,
      reporter: 'html',
      use: {{
        baseURL: process.env.BASE_URL || '{base_url}',
        screenshot: 'only-on-failure',
        video: 'on',
        trace: 'on-first-retry',
      }},
      projects: [{{ name: 'chromium', use: {{ ...devices['Desktop Chrome'] }} }}],
    }});
    """
    # config + package + .env.example: written ONCE for multi-TC (after the
    # dispatch loop, by _write_shared_pw_artifacts) so they reflect the union
    # across all cases. For single-TC, write here as before.
    if not multi_tc:
        config_path = pw_dir / 'playwright.config.ts'
        if not config_path.exists() or force:
            config_path.write_text(config_content)
            print(f"[✓] Config       → playwright.config.ts  (baseURL: {base_url})")
        else:
            print(f"[~] Config       → playwright.config.ts  (skipped – use --force to overwrite)")

        # .env.example — list every env var the generated spec references.
        env_example_path = pw_dir / '.env.example'
        env_lines = _build_env_lines(base_url, env_vars_used)
        if not env_example_path.exists() or force:
            env_example_path.write_text("\n".join(env_lines))
            print(f"[✓] Env template → .env.example      ({len(env_vars_used)} vars)")
        else:
            print(f"[~] Env template → .env.example      (skipped – use --force to overwrite)")

        pkg_path = pw_dir / 'package.json'
        if not pkg_path.exists() or force:
            pkg_path.write_text(_PACKAGE_JSON)

    # Spec — multi-TC: pw_dir/tests/<area>/<tc_stem>.spec.ts; single-TC: legacy name.
    if multi_tc:
        spec_path = pw_dir / 'tests' / area / (_tc_stem(tc) + '.spec.ts') if area \
                    else pw_dir / 'tests' / (_tc_stem(tc) + '.spec.ts')
        spec_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        spec_path = pw_dir / 'tests' / (to_snake(test_name) + '.spec.ts')
    if not spec_path.exists() or force:
        spec_path.write_text('\n'.join(spec_lines), encoding='utf-8')
        rel = spec_path.relative_to(base_out_dir) if multi_tc else spec_path.name
        print(f"[✓] Test spec    → {rel}  ({len(unmapped_actions)} TODO unmapped)")
    else:
        print(f"[~] Test spec    → {spec_path.name}  (skipped – use --force to overwrite)")

    print(f"\nSetup:")
    print(f"  cd {pw_dir}")
    print(f"  npm install")
    print(f"  npx playwright install chromium")
    print(f"  npm test")


# ──────────────────────────────────────────────────────────────────────────────
# Multi-TC dispatch: discover all TestCases, run the per-TC pipeline for each.
# Single-TC .tsu keeps the original output layout (base_out_dir/...).
# Multi-TC .tsu writes per-case isolated outputs under base_out_dir/<tc_stem>/.
# ──────────────────────────────────────────────────────────────────────────────
_test_cases = [x for x in data['Entities'] if x['ObjectClass'] == 'TestCase']
_project    = next((x for x in data['Entities'] if x['ObjectClass'] == 'TCProject'), None)
if not _test_cases:
    sys.exit('error: no TestCase entity found in .tsu')
if _project is None:
    sys.exit('error: no TCProject entity found in .tsu')

_multi = len(_test_cases) > 1
_base  = out_dir
if _multi:
    print(f'[i] {len(_test_cases)} test cases — shared playwright project under {_base}/playwright-test/, '
          f'per-case JSON+HTML under {_base}/cases/<tc_stem>/')
for _tc in _test_cases:
    _per_tc_pipeline(_tc, _project, _base, _multi)

# Multi-TC post-pass: write shared playwright artifacts (config + package +
# union .env.example) once. Single-TC handled them inside the per-TC pipeline.
if _multi and gen_pw:
    _write_shared_pw_artifacts(_base / 'playwright-test', _last_base_url, force)
if _multi:
    print(f'[✓] All {len(_test_cases)} test cases emitted.')
