#!/usr/bin/env python
"""Reproduce the context a scheduled task imports in, and give a verdict.

A scheduled job dies on its first import lines, before any logging exists, so
it leaves no log. The same script imports cleanly in a console. Three things
differ between the two, and nothing in a console shows any of them.

    1. stdout, stderr and stdin can be None (pythonw, a service, a task with
       no console). Code that calls sys.stdout.reconfigure() or .isatty() at
       import time raises AttributeError there and nowhere else.
    2. The user site-packages directory is a different directory for a
       different account, and a wrapper may switch it off. A package that is
       only installed there vanishes, and a copy there can shadow the one you
       meant.
    3. arcpy needs an initialised licence for the account and session the task
       runs under, and its failure message names none of that.

The existing tool pywho explains one interpreter and one environment: which
python, why `import X` loaded that file, which files shadow a module. It does
that well and importprobe does not replace it. pywho does not document running
the import a second time under the scheduled-task conditions and comparing.
This tool imports each module in two child processes of the target
interpreter, one as your shell sees it and one with the task conditions
applied, and reports every module whose result differs.

    python importprobe.py --self-test
    python importprobe.py --module json --module arcpy
    python importprobe.py --script nightly.py
    python importprobe.py --script nightly.py --env PYTHONNOUSERSITE=1
    python importprobe.py --script nightly.py --task-xml nightly_task.xml
    python importprobe.py --script nightly.py --out report.json --apply

--script is read with ast and never run. Only the modules it imports are
imported, in children, one process per module and context.

Exit codes: 0 PASS, 1 FAIL (an import dies in the task context, or a probe
process died), 3 WARN (every import works but something differs, is shadowed,
or nothing could be probed), 2 a file could not be read or written or the
interpreter could not run the probe, 64 usage error, 70 an unexpected crash,
which is never a finding. Nothing is written without --apply.
"""

from __future__ import print_function

import argparse
import ast
import codecs
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
import traceback
import xml.etree.ElementTree as ET

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Seconds one probe process may run. An arcpy import takes several seconds on a
# cold start, so the limit is generous. A hang becomes a CRASH finding.
DEFAULT_TIMEOUT = 120.0

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

# Prefix of the one line a probe process writes to the real stdout it was
# started with. The module under test never sees that stream in the task
# context, so the result cannot travel through sys.stdout.
MARKER = "IMPORTPROBE-RESULT "

# Where a winning copy of a module is surprising. A copy in the user site or
# beside the script is the one a person did not install on purpose.
SURPRISING = ("user-site", "script-dir")

# Matches the message arcpy raises when no licence was initialised. Esri
# publishes the sentence in a knowledge base article, not as a stable API, so
# a different wording falls through to IMPORT-ERROR instead of being guessed.
_LICENCE = re.compile(r"licen[cs]e.{0,40}not.{0,20}initiali[sz]ed", re.I)

# Exceptions an `except` clause can name to guard an optional import.
_IMPORT_CATCHERS = ("ImportError", "ModuleNotFoundError", "Exception",
                    "BaseException")

# Names of variables whose values are treated as secrets. A value of one of
# these is replaced in every line the tool prints or writes.
_SECRETISH = re.compile(r"token|passw|secret|key|pwd|cred", re.I)

_EXIT = {OK: 0, FAIL: 1, WARN: 3}


class Finding(object):
    """One thing the probe learned about one module, or about the task."""

    def __init__(self, module, code, severity, text):
        self.module = module
        self.code = code
        self.severity = severity
        self.text = text

    def as_dict(self):
        return {"module": self.module, "code": self.code,
                "severity": self.severity, "text": self.text}


# ----------------------------------------------------------------- the child

def child_main(spec, out):
    """Import one module and write what happened to `out` as one JSON line.

    This function runs INSIDE the target interpreter. The parent sends its
    source with `python -c`, so the probe imports nothing of this file and the
    imports below are the only ones the probe adds to a fresh interpreter.
    Everything is imported inside the function for that reason. `json` comes
    last, so probing `json` is not distorted by the probe itself.
    """
    startup = set(sys.modules)
    import importlib
    import importlib.machinery
    import os
    import site
    import sysconfig
    name = spec["module"]
    top = name.split(".")[0]
    tainted = top in sys.modules and top not in startup
    # `python -c` puts "" (the current directory) first. A task runs a script
    # file, which puts the script directory first and never the cwd.
    if sys.path and sys.path[0] == "":
        sys.path.pop(0)
    if spec["lead"]:
        sys.path.insert(0, spec["lead"])
    if spec["blank"]:
        sys.stdin = sys.stdout = sys.stderr = None
        sys.__stdin__ = sys.__stdout__ = sys.__stderr__ = None
    facts = {
        "executable": sys.executable,
        "version": "%d.%d.%d" % tuple(sys.version_info[:3]),
        "enable_user_site": site.ENABLE_USER_SITE,
        "user_site": site.getusersitepackages(),
        "site_dirs": (list(site.getsitepackages())
                      if hasattr(site, "getsitepackages") else []),
        "stdlib": sysconfig.get_paths()["stdlib"],
        "stdout_none": sys.stdout is None,
    }

    def origin_of(mod):
        path = getattr(mod, "__file__", None)
        if path:
            return path
        search = list(getattr(mod, "__path__", None) or [])
        if search:
            return search[0]
        if mod.__name__ in sys.builtin_module_names:
            return "built-in"
        return None

    candidates = []
    parts = []
    for entry in sys.path:
        spec_found = importlib.machinery.PathFinder.find_spec(top, [entry])
        if spec_found is None:
            continue
        where = spec_found.origin
        namespace = where in (None, "namespace")
        if namespace:
            where = (list(spec_found.submodule_search_locations or [])
                     or [None])[0]
        if where and where not in candidates:
            candidates.append(where)
            if namespace:
                parts.append(where)

    module = {"name": name, "ok": False, "origin": None, "top_origin": None,
              "candidates": candidates, "namespace_parts": parts,
              "tainted": tainted,
              "error_type": None, "error": None, "where": None}
    licence = None
    try:
        mod = importlib.import_module(name)
        module["ok"] = True
        module["origin"] = origin_of(mod)
        module["top_origin"] = origin_of(sys.modules[top])
        if top == "arcpy":
            licence = {"product": None, "error": None}
            try:
                licence["product"] = str(sys.modules[top].ProductInfo())
            except Exception as exc:
                licence["error"] = "%s: %s" % (type(exc).__name__, exc)
    except BaseException as exc:
        module["error_type"] = type(exc).__name__
        module["error"] = str(exc)
        # Innermost frame that is not the probe or the import machinery, so
        # the location names the module code that raised and a module that is
        # simply missing names nothing.
        frame = exc.__traceback__
        importlib_dir = os.path.dirname(importlib.__file__)
        while frame is not None:
            filename = frame.tb_frame.f_code.co_filename
            if not (frame.tb_frame.f_code is child_main.__code__
                    or filename.startswith("<")
                    or filename.startswith(importlib_dir)):
                module["where"] = "%s:%d" % (filename, frame.tb_lineno)
            frame = frame.tb_next
    import json
    # The leading newline keeps the marker on its own line when the module
    # wrote text at import without a trailing one.
    out.write("\n" + MARKER + json.dumps({"facts": facts, "module": module,
                                          "licence": licence}) + "\n")
    out.flush()


# ----------------------------------------------------------------- pure core

def parse_env_pairs(items):
    """{name: value} from NAME=VALUE strings, or ValueError for a bad one."""
    env = {}
    for number, item in enumerate(items, 1):
        name, sep, value = item.partition("=")
        if not sep or not name:
            # The item is not echoed: a mistyped one may be the secret itself.
            raise ValueError("--env item %d needs NAME=VALUE" % number)
        env[name] = value
    return env


def secret_pairs(overrides, ambient):
    """[(name, value)] to hide from every line the tool prints or writes.

    A value of four or more characters is hidden when its name looks secret
    (token, password, secret, key, pwd, cred), in the shell environment or in
    --env. A --env value of six or more characters is hidden whatever its
    name, except for PYTHON* variables, which are settings and not secrets.
    """
    pairs = [(n, v) for n, v in ambient.items()
             if _SECRETISH.search(n) and len(v) >= 4]
    pairs.extend((n, v) for n, v in overrides.items()
                 if len(v) >= 4 and (_SECRETISH.search(n) or (
                     len(v) >= 6 and not n.upper().startswith("PYTHON"))))
    return pairs


def _forms(value):
    """The ways a value can appear in text: as is, and as repr(), ascii() and
    JSON show it. repr() doubles backslashes, so an OSError message that names
    a secret path no longer contains the raw value."""
    return set([value, repr(value)[1:-1], ascii(value)[1:-1],
                json.dumps(value)[1:-1]])


def redact(text, secrets):
    """`text` with every secret value replaced by <redacted:NAME>."""
    forms = [(name, form) for name, value in secrets for form in _forms(value)
             if form]
    for name, form in sorted(forms, key=lambda p: (-len(p[1]), p[1])):
        text = text.replace(form, "<redacted:%s>" % name)
    return text


def _clip(text, secrets, limit=300):
    """`text` redacted first and then cut, so a cut never leaves the front of
    a secret behind."""
    return redact(text, secrets)[:limit]


# `except*` (3.11) guards an import like `except`. Older parsers lack TryStar.
_TRY = tuple(getattr(ast, name) for name in ("Try", "TryStar")
             if hasattr(ast, name))


def _guards_import(handler):
    """True when an except clause would catch a failed import."""
    kinds = (handler.type.elts if isinstance(handler.type, ast.Tuple)
             else [handler.type])
    return any(kind is None or (isinstance(kind, ast.Name)
                                and kind.id in _IMPORT_CATCHERS)
               for kind in kinds)


def _exits(stmts):
    """True when the statements raise or call exit(), quit(), sys.exit() or
    os._exit() anywhere, including inside a nested block. A handler that does
    this does not let the script carry on without the import."""
    for node in (n for stmt in stmts for n in ast.walk(stmt)):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in ("exit", "quit"):
                return True
            if (isinstance(func, ast.Attribute)
                    and func.attr in ("exit", "_exit")
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ("sys", "os")):
                return True
    return False


def _swallows(handler):
    """True when the handler catches a failed import and lets the script go on."""
    return _guards_import(handler) and not _exits(handler.body)


def _is_type_checking(test):
    """True for `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:`."""
    return ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"))


def _note_fallback(fallbacks, name, via):
    """Record that `name` is imported only as the fallback for the modules in
    `via`. An import seen anywhere else clears the record for good."""
    if via and fallbacks.get(name, via) is not None:
        fallbacks[name] = set(fallbacks.get(name, ())) | via
    else:
        fallbacks[name] = None


def _walk_imports(stmts, optional, found, subs, fallbacks, via=None):
    """Collect (name, optional) for imports that run when the file loads.

    Function bodies are skipped: they run later, after logging exists, and so
    is the body of `if TYPE_CHECKING:`, which never runs. An import in a try
    whose handler catches ImportError is optional, unless that handler raises
    or exits, because then the script dies without the module. An import in
    such a handler is optional too, and is recorded in `fallbacks` as the
    fallback for the modules the try body imports: it runs only when one of
    them failed. `from pkg import name` also records pkg.name in `subs`,
    because name may be a submodule that dies on its own.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = []
        if isinstance(stmt, ast.Import):
            names = [alias.name for alias in stmt.names]
        elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
            names = [stmt.module]
            for alias in stmt.names:
                if alias.name != "*":
                    key = "%s.%s" % (stmt.module, alias.name)
                    subs[key] = subs.get(key, True) and optional
        for name in names:
            found[name] = found.get(name, True) and optional
            _note_fallback(fallbacks, name, via)
        if isinstance(stmt, _TRY):
            guarded = optional or any(_swallows(h) for h in stmt.handlers)
            _walk_imports(stmt.body, guarded, found, subs, fallbacks, via)
            primary = {}
            _walk_imports(stmt.body, True, primary, {}, {})
            for handler in stmt.handlers:
                catches = _guards_import(handler)
                _walk_imports(handler.body, optional or catches, found, subs,
                              fallbacks, set(primary) if catches else via)
            _walk_imports(stmt.orelse, optional, found, subs, fallbacks, via)
            _walk_imports(stmt.finalbody, optional, found, subs, fallbacks, via)
            continue
        branches = [getattr(stmt, "body", []), getattr(stmt, "orelse", [])]
        branches.extend(case.body for case in getattr(stmt, "cases", []))
        if isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
            branches = branches[1:]
        for branch in branches:
            _walk_imports(branch, optional, found, subs, fallbacks, via)


def script_imports(text, subs=None, fallbacks=None):
    """[(module, optional)] a script imports at load time, in first-seen order.

    The script is parsed, never run. A SyntaxError propagates. A name that is
    imported once guarded and once unguarded counts as required. When `subs`
    is a dict, it receives {"pkg.name": optional} for each `from pkg import
    name`. When `fallbacks` is a dict, it receives {name: set of modules} for
    each name that is imported only in an except clause, as the fallback for
    the modules in the try body.
    """
    found = {}
    marks = {}
    _walk_imports(ast.parse(text).body, False, found,
                  {} if subs is None else subs, marks)
    if fallbacks is not None:
        fallbacks.update((n, v) for n, v in marks.items() if v is not None)
    return [(name, found[name]) for name in found]


_STREAMS = ("stdin", "stdout", "stderr", "__stdin__", "__stdout__",
            "__stderr__")


def _stream_ref(node):
    """True for the expression sys.stdout (or stdin, stderr, or a dunder one)."""
    return (isinstance(node, ast.Attribute) and node.attr in _STREAMS
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


class _StreamUse(ast.NodeVisitor):
    """Finds sys.stdout.<attr> style use in the code that runs at load time.

    A use is skipped when it sits in an `if` whose test mentions the stream,
    or in a `try` that catches AttributeError, because that code expects a
    stream that is None.
    """

    def __init__(self):
        self.uses = []

    def visit_FunctionDef(self, node):
        """A function body runs later, so it is not load-time code."""

    visit_AsyncFunctionDef = visit_Lambda = visit_FunctionDef

    def visit_Attribute(self, node):
        if _stream_ref(node.value) and isinstance(node.value.ctx, ast.Load):
            self.uses.append((node.lineno, "sys.%s.%s"
                              % (node.value.attr, node.attr)))
        self.generic_visit(node)

    def visit_If(self, node):
        before = len(self.uses)
        self.visit(node.test)
        guard = any(_stream_ref(n) for n in ast.walk(node.test))
        if len(self.uses) == before and guard:
            return
        for stmt in node.body + node.orelse:
            self.visit(stmt)

    def visit_Try(self, node):
        catches = any(h.type is None or any(
            isinstance(k, ast.Name) and k.id in ("AttributeError", "Exception",
                                                 "BaseException")
            for k in (h.type.elts if isinstance(h.type, ast.Tuple)
                      else [h.type])) for h in node.handlers)
        if not catches:
            for stmt in node.body:
                self.visit(stmt)
        for stmt in node.handlers + node.orelse + node.finalbody:
            self.visit(stmt)

    visit_TryStar = visit_Try


def script_stream_uses(text):
    """[(line, "sys.stdout.name")] the script touches when it loads.

    sys.stdout.reconfigure(), .isatty(), .write() and the like raise
    AttributeError when the stream is None. The imports of the script are
    probed, but its own load-time code is not run, so this reads it instead.
    """
    finder = _StreamUse()
    for stmt in ast.parse(text).body:
        finder.visit(stmt)
    return finder.uses


def plan_entries(modules, imports, subs):
    """([(name, optional)], soft) to probe, and the names that are soft.

    A soft name is pkg.name from `from pkg import name` that is not also
    imported as a module. It may be a plain attribute, so a missing module
    there is not a finding. A name seen twice is required if any use is.
    """
    seen = {}
    for name, optional in [(m, False) for m in modules] + list(imports):
        seen[name] = seen.get(name, True) and optional
    soft = [name for name in subs if name not in seen]
    entries = [(name, seen[name]) for name in seen]
    entries.extend((name, subs[name]) for name in soft)
    return entries, set(soft)


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _same_file(first, second):
    """True when two paths name one file, through a link or a short name too."""
    if _norm(first) == _norm(second):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _may_overwrite(path):
    """True when --apply may write `path`: it is absent, or an old report."""
    if not os.path.exists(path):
        return True
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "verdict" in data and "findings" in data


def _under(path, root):
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def classify_location(origin, facts, lead):
    """Name the kind of place a module file sits in.

    The deepest matching root wins. On Windows site.getsitepackages() lists
    the interpreter prefix itself, which contains the stdlib and every other
    directory, so "first match" would call every stdlib module site-packages.
    On equal depth the user site beats the script directory beats site-packages
    beats the stdlib.
    """
    if origin is None:
        return "unknown"
    if origin == "built-in":
        return "built-in"
    roots = [("stdlib", facts["stdlib"])]
    roots.extend(("site-packages", d) for d in facts["site_dirs"])
    if lead:
        roots.append(("script-dir", lead))
    if facts["user_site"]:
        roots.append(("user-site", facts["user_site"]))
    hits = [(len(_norm(root)), rank, kind)
            for rank, (kind, root) in enumerate(roots)
            if _under(origin, root)]
    return max(hits)[2] if hits else "other"


def hidden_copies(module):
    """Other copies of the top-level module that a fresh import would not use.

    When a namespace package wins, the other directories are its parts, which
    merge, and a regular package anywhere on the path would have won instead.
    """
    winner = _norm(module["top_origin"]) if module["top_origin"] else None
    if winner in [_norm(p) for p in module.get("namespace_parts", [])]:
        return []
    return [c for c in module["candidates"] if _norm(c) != winner]


def classify_error(error_type, message, none_is_cause):
    """A short code for why an import died.

    STDOUT-NONE needs the import to work with a real stream and to fail with
    the streams alone set to None, so the caller passes none_is_cause only
    then. Otherwise the same AttributeError would be blamed on a stream that
    was never the difference.
    """
    if _LICENCE.search(message):
        return "LICENCE"
    if error_type == "ModuleNotFoundError":
        return "NOT-IMPORTABLE"
    if none_is_cause and _none_attribute(error_type, message):
        return "STDOUT-NONE"
    return "IMPORT-ERROR"


def _none_attribute(error_type, message):
    return (error_type == "AttributeError"
            and "'NoneType' object has no attribute" in message)


def blames_none(h, t):
    """True when the task run failed like a None stream and the here run did
    not, so the streams-only run is worth making."""
    if "crash" in h or "crash" in t:
        return False
    tm = t["module"]
    return (h["module"]["ok"] and not tm["ok"]
            and _none_attribute(tm["error_type"], tm["error"]))


def _describe(module, secrets=()):
    where = " (at %s)" % module["where"] if module["where"] else ""
    return "%s: %s%s" % (module["error_type"],
                         _clip(module["error"], secrets), where)


def names_missing(name, error_type, message):
    """True when a ModuleNotFoundError names `name` or a package above it."""
    found = re.match(r"No module named '([^']+)'", message or "")
    return (error_type == "ModuleNotFoundError" and found is not None
            and (name == found.group(1)
                 or name.startswith(found.group(1) + ".")))


def compare_module(name, optional, h, t, lead, soft=False, secrets=()):
    """Findings for one module from its here result and its task result.

    h and t are what child_main wrote, or {"crash": text} when the process
    died or wrote nothing. A soft module may be a plain attribute: when it is
    missing in both contexts it is not a finding. t may carry "streams_only",
    the result of a run with the streams None and no --env. Error text is
    redacted with `secrets` before it is cut.
    """
    out = []

    def add(code, severity, text):
        out.append(Finding(name, code, severity, text))

    if "crash" in h or "crash" in t:
        where = " and ".join(label for label, r in (("here", h), ("task", t))
                             if "crash" in r)
        detail = (t if "crash" in t else h)["crash"]
        add("CRASH", FAIL, "the probe process died in the %s context: %s"
            % (where, detail))
        return out
    hm, tm = h["module"], t["module"]
    if soft and not hm["ok"] and names_missing(name, tm["error_type"],
                                               tm["error"]):
        return out
    if tm["ok"]:
        if not hm["ok"]:
            add("PASSES-ONLY-IN-TASK", WARN,
                "imports in the task context and fails here with %s"
                % _describe(hm, secrets))
        elif _norm_or_none(hm["top_origin"]) != _norm_or_none(tm["top_origin"]):
            add("ORIGIN-DIFFERS", WARN,
                "resolves to %s here and to %s in the task context"
                % (hm["top_origin"], tm["top_origin"]))
        place = classify_location(tm["top_origin"], t["facts"], lead)
        hidden = hidden_copies(tm)
        if place in SURPRISING and hidden:
            add("SHADOWS", WARN,
                "the copy in the %s wins (%s) and hides %s"
                % (place, tm["top_origin"], ", ".join(hidden)))
        if tm["tainted"]:
            add("UNPROBED", OK,
                "the probe loaded this module itself before the import, so "
                "its resolution here is the interpreter's, not a fresh import")
        lic = t["licence"]
        if lic is not None:
            if lic["error"]:
                add("LICENCE-CHECK", WARN,
                    "arcpy imported but ProductInfo() raised %s"
                    % _clip(lic["error"], secrets))
            elif lic["product"] == "NotInitialized":
                add("LICENCE", FAIL,
                    "arcpy imported but ProductInfo() returned NotInitialized, "
                    "so no licence is set for this account and session")
            else:
                add("LICENCE-LEVEL", OK,
                    "ProductInfo() returned %s for the account running "
                    "importprobe; the account the task runs as was not "
                    "checked" % lic["product"])
        if all(f.severity == OK for f in out):
            out.insert(0, Finding(name, "OK", OK, "found in %s: %s"
                                  % (place, tm["origin"])))
        return out
    alone = t.get("streams_only")
    alone_ok = (alone is not None and "module" in alone
                and alone["module"]["ok"])
    code = classify_error(tm["error_type"], tm["error"],
                          hm["ok"] and t["facts"]["stdout_none"]
                          and not alone_ok)
    if code == "NOT-IMPORTABLE" and hm["ok"] and classify_location(
            hm["top_origin"], h["facts"], lead) == "user-site":
        code = "USER-SITE-ONLY"
    severity = (WARN if optional and code in ("NOT-IMPORTABLE",
                                              "USER-SITE-ONLY") else FAIL)
    side = "imports fine here" if hm["ok"] else "also fails here"
    if alone_ok and _none_attribute(tm["error_type"], tm["error"]):
        side += ("; it also imports with the streams None and no --env, so "
                 "the --env values cause it")
    add(code, severity, "%s in the task context (%s)%s"
        % (_describe(tm, secrets), side, "; the import is optional" if optional
           and severity == WARN else ""))
    return out


def _norm_or_none(path):
    return _norm(path) if path else None


def parse_task_xml(data):
    """{logon_type, user_id, run_level, command} from an exported task XML.

    An XML file with a DOCTYPE or ENTITY is refused, because the standard
    library parser would expand it. A value that is absent is None.
    """
    if data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("the task XML declares a DOCTYPE or ENTITY, which "
                         "this tool will not expand")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("the task XML does not parse: %s" % exc)

    def text_at(*path):
        nodes = [root]
        for want in path:
            nodes = [c for n in nodes for c in n
                     if c.tag.rsplit("}", 1)[-1] == want]
        for node in nodes:
            if node.text and node.text.strip():
                return node.text.strip()
        return None

    return {
        "logon_type": text_at("Principals", "Principal", "LogonType"),
        "user_id": text_at("Principals", "Principal", "UserId"),
        "run_level": text_at("Principals", "Principal", "RunLevel"),
        "command": text_at("Actions", "Exec", "Command"),
    }


def task_findings(info, has_arcpy):
    """Findings about the task definition itself. Only LogonType is judged."""
    logon = info["logon_type"]
    if logon == "InteractiveToken":
        return [Finding("<task>", "TASK-LOGON", OK,
                        "LogonType is InteractiveToken: the task runs only in "
                        "an existing interactive session")]
    severity = WARN if has_arcpy else OK
    if logon is None:
        text = ("the task XML has no LogonType, so the session the task runs "
                "in is not known")
    else:
        text = ("LogonType is %s: the task can run with no interactive "
                "session, and an arcpy licence is set per account" % logon)
    return [Finding("<task>", "TASK-LOGON", severity, text)]


def resolve_streams(choice, command):
    """(mode, why) for the task streams: "none" or "real".

    Only pythonw.exe and a process with no console get None for stdout. A task
    that starts python.exe has a real stream, so an XML command that names a
    console python selects "real" unless the caller chose with --streams.
    """
    if choice:
        return choice, None
    base = re.split(r"[\\/]", (command or "").strip().strip('"'))[-1].lower()
    if re.match(r"^(python[0-9.]*|py)(\.exe)?$", base):
        return "real", ("the task command %s is a console python, which has "
                        "a real stdout; use --streams none for pythonw" % base)
    return "none", None


def verdict_of(findings):
    """FAIL if any finding failed, else WARN if any warned, else OK."""
    severities = set(f.severity for f in findings)
    return FAIL if FAIL in severities else WARN if WARN in severities else OK


def exit_code(verdict):
    return _EXIT[verdict]


_LICENCE_CODES = ("LICENCE", "LICENCE-CHECK", "LICENCE-LEVEL")


def build_report(python, runs, lead, env_names, task=None, soft=(),
                 secrets=(), stream_uses=(), streams=None):
    """The whole result as a dict: contexts, per-module status, findings.

    `soft` names may be plain attributes and get no row when they are not a
    finding. `secrets` are (name, value) pairs removed from every finding and
    from the task command. `stream_uses` are the (line, expression) pairs
    from script_stream_uses. `streams` is {"mode", "why"}. An empty run is a
    WARN, because it says nothing about any import.
    """
    findings = []
    modules = []
    facts = {"here": None, "task": None}
    licence_seen = set()
    for name, optional, h, t in runs:
        found = compare_module(name, optional, h, t, lead, name in soft,
                               secrets)
        # arcpy, arcpy.da and arcpy.mp repeat one licence answer.
        found = [f for f in found if f.code not in _LICENCE_CODES
                 or (f.code, f.text) not in licence_seen]
        licence_seen.update((f.code, f.text) for f in found
                            if f.code in _LICENCE_CODES)
        findings.extend(found)
        for label, result in (("here", h), ("task", t)):
            if facts[label] is None and "crash" not in result:
                facts[label] = result["facts"]
        if name in soft and not found:
            continue
        modules.append({"name": name, "optional": optional,
                        "here": _status(h), "task": _status(t)})
    if not modules:
        findings.append(Finding(
            "<script>", "NOTHING-PROBED", WARN,
            "no import was probed, so this run says nothing about a script "
            "or a module"))
    if stream_uses:
        findings.append(Finding(
            "<script>", "SCRIPT-STREAMS", WARN,
            "the script uses %s when it loads, which raises AttributeError "
            "when that stream is None; this tool does not run the script, so "
            "the imports above say nothing about it"
            % ", ".join("%s (line %d)" % (expr, line)
                        for line, expr in stream_uses[:5])))
    if task is not None:
        findings.extend(task_findings(
            task, any(n.split(".")[0] == "arcpy" for n, _, _, _ in runs)))
    for finding in findings:
        finding.text = redact(finding.text, secrets)
    if task is not None:
        task = dict((key, redact(value, secrets) if value else value)
                    for key, value in task.items())
    return {"python": python, "env_overrides": sorted(env_names),
            "streams": streams or {"mode": "none", "why": None},
            "contexts": facts, "modules": modules,
            "findings": [f.as_dict() for f in findings],
            "task": task, "verdict": verdict_of(findings)}


def _status(result):
    if "crash" in result:
        return "crash"
    return "ok" if result["module"]["ok"] else "fail"


def _context_line(label, facts, env_names):
    if facts is None:
        return "%s: no probe returned a result" % label
    site = {True: "on", False: "off", None: "off (disabled by the interpreter)"}
    line = "%s: python %s, user site %s, stdout %s" % (
        label, facts["version"], site[facts["enable_user_site"]],
        "None" if facts["stdout_none"] else "a real stream")
    if env_names:
        line += ", env set: %s" % ", ".join(env_names)
    return line


def render(report):
    """The report as text lines, ASCII only."""
    lines = ["importprobe: %s, %d module(s)"
             % (report["python"], len(report["modules"]))]
    if not report["modules"]:
        lines.append("  nothing was probed: no import ran, so this says "
                     "nothing about a script or a module")
    else:
        lines.append("  " + _context_line(
            "here", report["contexts"]["here"], []))
        lines.append("  " + _context_line(
            "task", report["contexts"]["task"], report["env_overrides"]))
        why = (report.get("streams") or {}).get("why")
        if why:
            lines.append("  streams: %s" % why)
        width = max([len("module")] + [len(m["name"])
                                        for m in report["modules"]])
        lines.append("")
        lines.append("%-*s  here   task" % (width, "module"))
        for m in report["modules"]:
            lines.append("%-*s  %-5s  %s" % (width, m["name"], m["here"],
                                             m["task"]))
    if report["task"] is not None:
        t = report["task"]
        lines.append("")
        lines.append("task XML: LogonType=%s RunLevel=%s"
                     % (t["logon_type"], t["run_level"]))
    lines.append("")
    rank = {FAIL: 0, WARN: 1, OK: 2}
    for f in sorted(report["findings"], key=lambda f: rank[f["severity"]]):
        lines.append("%-4s  %s  %s: %s" % (f["severity"], f["module"],
                                           f["code"], f["text"]))
    counts = dict((s, sum(1 for f in report["findings"]
                          if f["severity"] == s)) for s in (FAIL, WARN))
    lines.append("")
    lines.append("verdict: %s (%d fail, %d warn)"
                 % (report["verdict"], counts[FAIL], counts[WARN]))
    return [_ascii(line) for line in lines]


def _ascii(text):
    return text.encode("ascii", "replace").decode("ascii")


# ----------------------------------------------------------------------- I/O

def child_env(base, overrides):
    """The environment for a probe process.

    Bytecode writing is off so the probe never writes a .pyc into a script
    directory that the scheduled account might not be able to write to.
    """
    env = dict(base)
    env.update(overrides)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _child_command(python, spec):
    source = ("import sys\nMARKER = %r\n%s\nchild_main(%r, sys.stdout)\n"
              % (MARKER, inspect.getsource(child_main), spec))
    return [python, "-c", source]


def run_context(python, spec, env, timeout):
    """Run one probe process. The result is child_main's dict or a crash."""
    try:
        proc = subprocess.Popen(_child_command(python, spec), env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return {"crash": "cannot start %s: %s" % (python, exc)}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        # subprocess.run() would wait here for the pipes to close, and a
        # grandchild the module started can hold them open for as long as it
        # lives. Give the pipes a moment and leave.
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return {"crash": "timed out after %g s" % timeout}
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    tail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
    return {"crash": "exit %d with no result%s"
            % (proc.returncode, ": " + tail[0] if tail else "")}


def _worked(pair):
    return pair is not None and all("module" in r and r["module"]["ok"]
                                    for r in pair)


def probe(python, entries, base_env, overrides, lead, timeout, fallbacks=None,
          blank=True):
    """[(name, optional, here result, task result)] for every module.

    The here context is a console, which writes through the wide-character
    API, so its streams are UTF-8. A name in `fallbacks` ({name: modules}) is
    skipped when every module it stands in for imported in both contexts,
    because the except clause that imports it would never run. When a task
    run fails like a None stream and --env was given, a third run with the
    streams None and no --env is stored in the task result as "streams_only".
    """
    here_env = child_env(base_env, {})
    here_env["PYTHONIOENCODING"] = "utf-8"
    task_env = child_env(base_env, overrides)
    plain_env = child_env(base_env, {})
    done = {}
    runs = []
    for name, optional in entries:
        if name in (fallbacks or {}) and all(
                _worked(done.get(p)) for p in fallbacks[name]):
            continue
        h = run_context(python, {"module": name, "blank": False,
                                 "lead": lead}, here_env, timeout)
        t = run_context(python, {"module": name, "blank": blank,
                                 "lead": lead}, task_env, timeout)
        if blank and overrides and blames_none(h, t):
            t["streams_only"] = run_context(
                python, {"module": name, "blank": True, "lead": lead},
                plain_env, timeout)
        done[name] = (h, t)
        runs.append((name, optional, h, t))
    return runs


def write_report(path, report):
    """Write the report as JSON. The only write in this file."""
    with open(path, "w") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


# ---------------------------------------------------------------------- CLI

class UsageError(Exception):
    """A command line the parser refused."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _parse(argv):
    ap = _Parser(
        prog="importprobe.py",
        description="Import modules the way a scheduled task would, compare "
                    "with your shell, and give a verdict.",
        epilog="Nothing is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("--module", action="append", default=[], metavar="NAME",
                    help="a module to probe; repeat for several")
    ap.add_argument("--script", metavar="FILE",
                    help="a script whose load-time imports are probed. It is "
                         "parsed and never run.")
    ap.add_argument("--python", metavar="EXE", default=sys.executable,
                    help="the interpreter the task uses: a path, or a name on "
                         "PATH (default: this one)")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                    help="set a variable in the task context only; repeat")
    ap.add_argument("--task-xml", dest="task_xml", metavar="FILE",
                    help="an exported Task Scheduler definition to read")
    ap.add_argument("--streams", choices=("none", "real"),
                    help="the task's stdin, stdout and stderr: none (pythonw, "
                         "no console) or real (python.exe). Default: real when "
                         "the task XML command is a console python, else "
                         "none.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds per probe process (default %g)"
                         % DEFAULT_TIMEOUT)
    ap.add_argument("--out", metavar="FILE",
                    help="where to write the report as JSON, with --apply. An "
                         "existing file is replaced only if it is an earlier "
                         "report.")
    ap.add_argument("--apply", action="store_true",
                    help="write the --out file. Without it nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'an import fails in the task'."""
    try:
        return _run(sys.argv[1:] if argv is None else argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the script." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(argv)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    if args.self_test:
        return self_test()
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64
    if not (args.module or args.script or args.task_xml):
        print("error: name --module, --script or --task-xml. Use --self-test "
              "to verify the tool without them.", file=sys.stderr)
        return 64
    # Written as a negated chain so that nan, which compares false, is refused.
    if not 0 < args.timeout <= 1e6:
        print("error: --timeout must be above zero and at most 1000000.",
              file=sys.stderr)
        return 64
    for source in (args.script, args.task_xml):
        if args.out and source and _same_file(source, args.out):
            print("error: --out must not name the --script or --task-xml "
                  "file.", file=sys.stderr)
            return 64
    if args.apply and not _may_overwrite(args.out):
        print("error: %s exists and is not an importprobe report, so --apply "
              "would destroy it. Name a new file." % args.out, file=sys.stderr)
        return 64
    try:
        overrides = parse_env_pairs(args.env)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    python = args.python if os.path.isfile(args.python) else shutil.which(
        args.python)
    if not python:
        print("error: the interpreter %s is not a file and not a name on "
              "PATH." % args.python, file=sys.stderr)
        return 2
    base_env = os.environ.copy()
    secrets = secret_pairs(overrides, base_env)
    # One import of a built-in module tells an interpreter that cannot run the
    # probe at all from an import that dies, which is a finding and not this.
    check = run_context(python, {"module": "sys", "blank": False,
                                 "lead": None}, child_env(base_env, {}),
                        args.timeout)
    if "crash" in check:
        print(redact("error: the interpreter %s could not run the probe: %s"
                     % (python, check["crash"]), secrets), file=sys.stderr)
        return 2

    imports = []
    subs = {}
    fallbacks = {}
    uses = []
    lead = None
    task = None
    try:
        if args.script:
            # tokenize.open honours a byte order mark and a coding comment.
            with tokenize.open(args.script) as handle:
                text = handle.read()
            imports = script_imports(text, subs, fallbacks)
            uses = script_stream_uses(text)
            lead = os.path.dirname(os.path.abspath(args.script))
        if args.task_xml:
            with open(args.task_xml, "rb") as handle:
                task = parse_task_xml(handle.read())
    except (IOError, OSError, ValueError, SyntaxError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    entries, soft = plan_entries(args.module, imports, subs)
    mode, why = resolve_streams(args.streams, task and task["command"])
    fallbacks = dict((n, v) for n, v in fallbacks.items()
                     if n not in args.module)

    runs = probe(python, entries, base_env, overrides, lead, args.timeout,
                 fallbacks, mode == "none")
    report = build_report(python, runs, lead, list(overrides), task, soft,
                          secrets, uses if mode == "none" else (),
                          {"mode": mode, "why": why})
    for line in render(report):
        print(line)
    if args.out:
        if args.apply:
            try:
                write_report(args.out, report)
            except (IOError, OSError) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print(_ascii("wrote %s" % args.out))
        else:
            print(_ascii("Check only. %s was not written. Re-run with "
                         "--apply." % args.out))
    return exit_code(report["verdict"])


# ------------------------------------------------------------------ self-test

def self_test():
    """Run the assertions in a temp directory that is removed even when one of
    them raises."""
    tmp = tempfile.mkdtemp(prefix="importprobe-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_FIXTURES = {
    # Touches sys.stdout in ways that only work on a real stream.
    "ipfix_reconf": "import sys\nsys.stdout.reconfigure(encoding='utf-8')\n",
    "ipfix_isatty": "import sys\nCOLOUR = sys.stdout.isatty()\n",
    # These two do NOT fail with stdout None. Pinned so the README may say so.
    "ipfix_print": "print('hello from import')\n",
    "ipfix_logging": ("import logging\nlogging.basicConfig()\n"
                      "logging.warning('hello from import')\n"),
    "ipfix_atexit": ("import atexit\n"
                     "atexit.register(print, 'after the result')\n"),
    "ipfix_boom": "raise ValueError('boom at import')\n",
    "ipfix_die": "import os\nos._exit(7)\n",
    "ipfix_diemsg": ("import os, sys\nif sys.stderr is not None:\n"
                     "    sys.stderr.write('dying words\\n')\n"
                     "    sys.stderr.flush()\nos._exit(7)\n"),
    # Writes at import with no trailing newline; print() to None is silent.
    "ipfix_noeol": "print('loading...', end='')\n",
    "ipfix_leak": ("import os\nraise RuntimeError('bad ' + "
                   "os.environ.get('IPFIX_TOKEN', '') + ' ' + "
                   "os.environ.get('IPFIX_AMBIENT_KEY', '') + ' ' + "
                   "os.environ.get('IPFIX_PLAIN', ''))\n"),
    "ipfix_hang": "import time\ntime.sleep(60)\n",
    "ipfix_nofile": "del __file__\n",
    "ipfix_gate": ("import os\nif os.environ.get('IPFIX_GATE') != 'open':\n"
                   "    raise RuntimeError('gate closed')\n"),
    "ipfix_only": "VALUE = 1\n",
    "ipfix_dup": "VALUE = 'a copy'\n",
    "colorsys": "VALUE = 'this file shadows the standard library'\n",
    # Fails only when --env turns IPFIX_MODE off. The None is not a stream.
    "ipfix_envnone": ("import os\n"
                      "thing = None if os.environ.get('IPFIX_MODE') == 'off' "
                      "else os\nthing.getcwd()\n"),
    # A grandchild holds the probe's pipes open after the probe is killed.
    "ipfix_hold": ("import subprocess, sys, time\n"
                   "subprocess.Popen([sys.executable, '-c', "
                   "'import time; time.sleep(12)'], stdout=sys.stdout, "
                   "stderr=sys.stderr)\ntime.sleep(60)\n"),
    "ipfix_arrow": "print('\\u2192')\n",
    "ipfix_dunder": ("import sys\n"
                     "if any(d is not None for d in (sys.__stdin__, "
                     "sys.__stdout__, sys.__stderr__)):\n"
                     "    raise RuntimeError('a dunder stream is set')\n"),
    "ipfix_fakemark": "print('IMPORTPROBE-RESULT {\"bogus\": 1}')\n",
    "ipfix_sysexit": "raise SystemExit('bye')\n",
    "ipfix_envq": ("import os\nraise ValueError('bad value %r' % "
                  "os.environ.get('IPFIX_QUOTED', ''))\n"),
    "ipfix_long": ("import os\nraise RuntimeError('x' * 290 + "
                   "os.environ.get('IPFIX_LONGSEC', ''))\n"),
}

_FAKE_ARCPY = (
    "import os\n"
    "MODE = os.environ.get('IPFIX_LIC', 'ok')\n"
    "if MODE == 'raise':\n"
    "    raise RuntimeError('The Product License has not been initialized.')\n"
    "def ProductInfo():\n"
    "    if MODE == 'broken':\n"
    "        raise RuntimeError('licence service unreachable')\n"
    "    return 'NotInitialized' if MODE == 'uninit' else 'ArcInfo'\n")

_TASK_XML = (
    '<?xml version="1.0" encoding="%s"?>\n'
    '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
    '  <Triggers><LogonTrigger><UserId>trigger-user</UserId>'
    '</LogonTrigger></Triggers>\n'
    '  <Principals><Principal id="Author">\n'
    '    <UserId>svc-example</UserId>%s<RunLevel>LeastPrivilege</RunLevel>\n'
    '  </Principal></Principals>\n'
    '  <Actions><Exec><Command>job.cmd</Command></Exec></Actions>\n'
    '</Task>\n')


def _write(path, text):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "w") as handle:
        handle.write(text)


@contextlib.contextmanager
def _in_process_child_state():
    """Save and restore everything child_main is allowed to change."""
    saved = (sys.path[:], sys.stdin, sys.stdout, sys.stderr, sys.__stdin__,
             sys.__stdout__, sys.__stderr__, dict(sys.modules))
    try:
        yield
    finally:
        sys.path[:] = saved[0]
        (sys.stdin, sys.stdout, sys.stderr, sys.__stdin__, sys.__stdout__,
         sys.__stderr__) = saved[1:7]
        for key in set(sys.modules) - set(saved[7]):
            del sys.modules[key]


def _child_in_process(spec, path_head=""):
    """Call child_main here, as `python -c` would, and return its result."""
    sink = io.StringIO()
    with _in_process_child_state():
        sys.path.insert(0, path_head)
        child_main(spec, sink)
    return json.loads(sink.getvalue()[len(MARKER):])


def _self_test(tmp):
    """Assertions over the core, then real probe processes against synthetic
    modules. No network, no credentials, no arcpy."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("importprobe self-test: temporary files and local child "
          "processes only, no network, no credentials, no arcpy")
    print("-" * 68)

    # ---- the check harness itself
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe_failures = failed[mark:]
    del failed[mark:]
    check(len(probe_failures) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the exception it is looking for")

    # ---- script_imports: which imports count as load-time
    src = ("import os, json.tool\n"
           "from collections import OrderedDict\n"
           "from . import sibling\n"
           "from .pkg import thing\n"
           "try:\n    import optdep\nexcept ImportError:\n    optdep = None\n"
           "try:\n    import warnedmod\nexcept (KeyError, ImportError):\n"
           "    import fallbackmod\n"
           "try:\n    import risky\nexcept KeyError:\n    pass\n"
           "try:\n    import bare\nexcept:\n    pass\n"
           "try:\n    import dotted\nexcept builtins.ImportError:\n    pass\n"
           "import optdep2\ntry:\n    import optdep2\nexcept Exception:\n"
           "    pass\n"
           "try:\n    import both\nexcept ImportError:\n    pass\n"
           "import both\n"
           "if True:\n    import inside_if\nelse:\n    import inside_else\n"
           "class K:\n    import inside_class\n"
           "def late():\n    import hidden_lazy\n"
           "async def alate():\n    import hidden_async\n"
           "try:\n    pass\nexcept ImportError:\n    import in_handler\n"
           "else:\n    import in_else\nfinally:\n    import in_finally\n"
           "try:\n    import kerr\nexcept KeyError:\n    import kerr_handler\n")
    got = dict(script_imports(src))
    check(list(got)[:4] == ["os", "json.tool", "collections", "optdep"]
          and len(script_imports(src)) == len(got),
          "script_imports lists each module once, in first-seen order")
    check(got["os"] is False and got["json.tool"] is False
          and got["collections"] is False,
          "plain and from imports at load time are required")
    check("sibling" not in got and "hidden_lazy" not in got
          and "hidden_async" not in got and None not in got,
          "relative imports and imports inside a function are not probed"
          "  <-- pinned defect")
    check(got["optdep"] is True and got["bare"] is True
          and got["fallbackmod"] is True and got["in_handler"] is True,
          "an import guarded by except ImportError or a bare except is "
          "optional, and so is the fallback import in that handler"
          "  <-- pinned defect")
    check(got["kerr_handler"] is False,
          "an import in a handler that does not catch ImportError is required")
    check(got["warnedmod"] is True and got["risky"] is False
          and got["dotted"] is False,
          "a tuple naming ImportError guards, a handler for KeyError or an "
          "attribute path does not")
    check(got["optdep2"] is False and got["both"] is False,
          "a name imported both guarded and unguarded counts as required"
          "  <-- pinned defect")
    check(got["inside_if"] is False and got["inside_else"] is False
          and got["inside_class"] is False
          and got["in_else"] is False and got["in_finally"] is False,
          "imports inside if, else, class, else and finally blocks are found")
    subs = {}
    script_imports("from pkg import a, b as c\nfrom pkg import *\n"
                   "from . import rel\nfrom .q import r\n"
                   "try:\n    from opt import x\nexcept ImportError:\n"
                   "    pass\nfrom opt import x\nimport plain\n"
                   "from req import y\ntry:\n    from req import y\n"
                   "except ImportError:\n    pass\n", subs)
    check(subs == {"pkg.a": False, "pkg.b": False, "opt.x": False,
                   "req.y": False},
          "from pkg import name records pkg.name as a possible submodule, "
          "skips star and relative imports, and is required when any use is"
          "  <-- pinned defect")
    entries_, soft_ = plan_entries(
        ["m"], [("m", True), ("a", True), ("a", False), ("b", True)],
        {"a.x": False, "b": True, "m.y": True})
    check(entries_ == [("m", False), ("a", False), ("b", True),
                       ("a.x", False), ("m.y", True)]
          and soft_ == set(["a.x", "m.y"]),
          "a name used twice is required if any use is, a --module name is "
          "required, and a from-import name that is also a module is not soft")
    raises(lambda: script_imports("def broken(:\n"),
           "a script that does not parse raises SyntaxError", SyntaxError)
    check(script_imports("") == [], "an empty script imports nothing")
    exits = dict(script_imports(
        "import sys, os\n"
        "try:\n    import dies_exit\nexcept ImportError:\n    sys.exit(1)\n"
        "try:\n    import dies_raise\nexcept ImportError as exc:\n"
        "    raise RuntimeError('no')\n"
        "try:\n    import dies_quit\nexcept ImportError:\n    quit()\n"
        "try:\n    import dies_os\nexcept ImportError:\n    os._exit(2)\n"
        "try:\n    import dies_deep\nexcept ImportError:\n    log()\n"
        "    if True:\n        exit(1)\n"
        "try:\n    import lives_a\nexcept ImportError:\n    parser.exit()\n"
        "try:\n    import lives_b\nexcept ImportError:\n    a.b.exit()\n"
        "try:\n    import lives_c\nexcept ImportError:\n    log()\n"
        "    state = os.getcwd()\n"
        "try:\n    import lives_d\nexcept ImportError:\n    state = 1\n"))
    check(not any(exits[n] for n in ("dies_exit", "dies_raise", "dies_quit",
                                     "dies_os", "dies_deep")),
          "an import whose ImportError handler raises or exits is required, "
          "because the script dies without it  <-- pinned defect")
    check(all(exits[n] for n in ("lives_a", "lives_b", "lives_c", "lives_d")),
          "a handler that logs, assigns or calls another exit method leaves "
          "the import optional")
    check(dict(script_imports(
        "try:\n    import g_a\nexcept ModuleNotFoundError:\n    pass\n"
        "try:\n    import g_b\nexcept Exception:\n    pass\n"
        "try:\n    import g_c\nexcept BaseException:\n    pass\n"))
          == {"g_a": True, "g_b": True, "g_c": True},
          "ModuleNotFoundError, Exception and BaseException each guard an "
          "import")
    typed = dict(script_imports(
        "import typing\nfrom typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import only_typed\nelse:\n"
        "    import run_time\nif typing.TYPE_CHECKING:\n"
        "    import only_typed2\n"))
    check("only_typed" not in typed and "only_typed2" not in typed
          and typed["run_time"] is False,
          "an import under if TYPE_CHECKING never runs, so it is not probed, "
          "and its else branch is  <-- pinned defect")
    new_src = ("import sys\nmatch command:\n    case 1:\n"
               "        import in_match_a\n    case _:\n"
               "        import in_match_b\n"
               "try:\n    import star_guarded\nexcept* ImportError:\n"
               "    pass\n"
               "try:\n    import star_exits\nexcept* ImportError:\n"
               "    sys.exit(1)\n")
    newer = dict(script_imports(new_src)) if sys.version_info >= (3, 11) else {}
    check(sys.version_info < (3, 11) or (
        newer["in_match_a"] is False and newer["in_match_b"] is False
        and newer["star_guarded"] is True and newer["star_exits"] is False),
          "imports in match cases are found and except* guards like except "
          "(vacuous before Python 3.11)  <-- pinned defect")
    fb = {}
    script_imports("try:\n    import tomllib\nexcept ImportError:\n"
                   "    import tomli as tomllib\n"
                   "try:\n    import a1, a2\nexcept ImportError:\n"
                   "    import b1\n    import b1\n"
                   "try:\n    import c1\nexcept ImportError:\n    pass\n"
                   "    import d1\n"
                   "import e1\ntry:\n    import e0\nexcept ImportError:\n"
                   "    import e1\n"
                   "try:\n    import f0\nexcept ImportError:\n    import g1\n"
                   "try:\n    import f9\nexcept ImportError:\n    import g1\n"
                   "try:\n    import h0\nexcept KeyError:\n    import h1\n"
                   "try:\n    pass\nexcept ImportError:\n    import i1\n"
                   "try:\n    import j0\nexcept ImportError:\n    import j1\n"
                   "import j1\n", None, fb)
    check(fb == {"tomli": set(["tomllib"]), "b1": set(["a1", "a2"]),
                 "d1": set(["c1"]), "g1": set(["f0", "f9"])},
          "an import in an except ImportError handler is recorded as the "
          "fallback for the try body, unless it is imported elsewhere too, "
          "the handler does not catch ImportError or the body imports "
          "nothing  <-- pinned defect")

    # ---- script_stream_uses
    uses = script_stream_uses(
        "import sys\nsys.stdout.reconfigure(encoding='utf-8')\n"
        "x = sys.stderr.isatty()\nsys.stdout = None\n"
        "print('x', file=sys.stderr)\n"
        "def later():\n    sys.stdout.write('x')\n"
        "f = lambda: sys.stdout.flush()\n"
        "class K:\n    w = sys.stdin.readline\n"
        "if sys.stdout is not None:\n    sys.stdout.reconfigure()\n"
        "else:\n    sys.stdout.write('x')\n"
        "if sys.stdout.isatty():\n    sys.stdout.write('c')\n"
        "if debug:\n    sys.__stdout__.flush()\n"
        "try:\n    sys.stdout.reconfigure()\nexcept AttributeError:\n"
        "    pass\n"
        "try:\n    sys.stdout.reconfigure()\nexcept (OSError, Exception):\n"
        "    pass\n"
        "try:\n    sys.stdout.reconfigure()\nexcept:\n    pass\n"
        "try:\n    sys.stdout.reconfigure(1)\nexcept OSError:\n"
        "    sys.stderr.flush()\nfinally:\n    sys.stdout.flush()\n"
        "async def a():\n    sys.stdout.write('x')\n")
    check([e for _, e in uses] == [
        "sys.stdout.reconfigure", "sys.stderr.isatty", "sys.stdin.readline",
        "sys.stdout.isatty", "sys.stdout.write", "sys.__stdout__.flush",
        "sys.stdout.reconfigure", "sys.stderr.flush", "sys.stdout.flush"]
          and uses[0][0] == 2,
          "load-time use of a standard stream is found with its line, and "
          "functions, assignments, print(file=) and code guarded by a None "
          "test or except AttributeError are not  <-- pinned defect")
    check(script_stream_uses("x = 1\n") == [],
          "a script that never touches a stream has no uses")
    raises(lambda: script_stream_uses("def broken(:\n"),
           "a script that does not parse raises SyntaxError", SyntaxError)

    # ---- parse_env_pairs
    check(parse_env_pairs(["A=1", "B=x=y", "C="]) == {"A": "1", "B": "x=y",
                                                       "C": ""},
          "env pairs split at the first equals sign and allow an empty value")
    raises(lambda: parse_env_pairs(["NOEQUALS"]),
           "an env item with no equals sign is refused")
    raises(lambda: parse_env_pairs(["=value"]),
           "an env item with no name is refused")
    try:
        parse_env_pairs(["A=1", "=hunter2-typo"])
    except ValueError as exc:
        refusal = str(exc)
    check("hunter2-typo" not in refusal and "item 2" in refusal,
          "a refused env item is numbered and never echoed, because it may "
          "be the secret  <-- pinned defect")
    check(set(secret_pairs(
        {"PYTHONPATH": "/long/path/here", "PYTHONNOUSERSITE": "1",
         "MY_TOKEN": "abcd", "PLAIN": "abcdef", "SHORT": "abc",
         "PLAINSHORT": "abcde", "PYTHONTOKEN": "tttt"},
        {"AMBIENT_PASSWORD": "pw12", "HOME": "/home/x", "KEY": "abc"}))
          == set([("AMBIENT_PASSWORD", "pw12"), ("MY_TOKEN", "abcd"),
                  ("PLAIN", "abcdef"), ("PYTHONTOKEN", "tttt")]),
          "secret_pairs hides secret-named values of four characters or more "
          "and long --env values, but not PYTHON* settings or short values")
    check(redact("a abc-long abc b", [("N1", "abc"), ("N2", "abc-long")])
          == "a <redacted:N2> <redacted:N1> b"
          and redact("keep", []) == "keep",
          "redact replaces the longest secret first and leaves other text")
    quoted = 'pa"ss\\word99'
    check(all("word99" not in redact(text, [("P", quoted)]) for text in (
        "bad value %r" % quoted, json.dumps(quoted), quoted,
        ascii(u"x" + quoted + u"\u00e9"))),
          "redact also removes the repr(), JSON and ascii() forms of a value, "
          "which double the backslashes  <-- pinned defect")
    check(redact("abc", [("E", "")]) == "abc",
          "an empty secret value replaces nothing")
    clipped = _clip("x" * 290 + "secret-value-1", [("S", "secret-value-1")])
    check(len(clipped) == 300 and "secret" not in clipped
          and _clip("short", []) == "short",
          "text is redacted before it is cut to 300 characters, so a secret "
          "that straddles the cut leaks no prefix  <-- pinned defect")
    check(set(secret_pairs({"A_TOKEN": "abc", "B_TOKEN": "abcd"}, {}))
          == set([("B_TOKEN", "abcd")]),
          "a secret-named value of three characters is not hidden and one of "
          "four is")

    # ---- classify_location and hidden_copies on synthetic facts
    ap = os.path.abspath
    facts = {"user_site": ap("/u/site"), "site_dirs": [ap("/g/site")],
             "stdlib": ap("/g/lib")}
    check(classify_location(None, facts, None) == "unknown",
          "no origin classifies as unknown")
    check(classify_location("built-in", facts, None) == "built-in",
          "the built-in marker classifies as built-in")
    check(classify_location(ap("/u/site/a.py"), facts, None) == "user-site",
          "a file under the user site is user-site")
    check(classify_location(ap("/g/site/a.py"), facts, None)
          == "site-packages", "a file under a site dir is site-packages")
    check(classify_location(ap("/s/a.py"), facts, ap("/s")) == "script-dir",
          "a file beside the script is script-dir")
    check(classify_location(ap("/g/lib/a.py"), facts, None) == "stdlib",
          "a file under the stdlib is stdlib")
    check(classify_location(ap("/elsewhere/a.py"), facts, None) == "other",
          "any other file is other")
    check(classify_location(ap("/u/site-extra/a.py"),
                            dict(facts, user_site=None), None) == "other",
          "a user site of None matches nothing and a sibling directory with "
          "the same prefix is not under a root  <-- pinned defect")
    check(classify_location(ap("/g/lib/site-packages/a.py"), dict(
        facts, site_dirs=[ap("/g/lib/site-packages")]), None)
        == "site-packages",
          "site-packages inside the stdlib directory wins over stdlib")
    prefix_facts = dict(facts, site_dirs=[ap("/g"), ap("/g/lib/site-packages")])
    check(classify_location(ap("/g/lib/json/a.py"), prefix_facts, None)
          == "stdlib" and classify_location(
              ap("/g/lib/site-packages/a.py"), prefix_facts, None)
          == "site-packages",
          "a site directory that is the interpreter prefix does not turn "
          "every stdlib file into site-packages  <-- pinned defect")
    check(classify_location(ap("/u/site/a.py"), dict(
        facts, site_dirs=[ap("/u")]), None) == "user-site"
        and classify_location(ap("/s/a.py"), dict(
            facts, site_dirs=[ap("/s")]), ap("/s")) == "script-dir",
          "on equal depth the user site and the script directory win")
    check(hidden_copies({"top_origin": ap("/a/x.py"),
                         "candidates": [ap("/a/x.py"), ap("/b/x.py")]})
          == [ap("/b/x.py")],
          "the winning copy is not hidden, the later one is")
    check(hidden_copies({"top_origin": None, "candidates": [ap("/b/x.py")]})
          == [ap("/b/x.py")], "with no winner every copy is reported")

    # ---- classify_error
    check(classify_error("RuntimeError", "The Product License has not been "
                         "initialized.", False) == "LICENCE",
          "the Esri licence sentence classifies as LICENCE")
    check(classify_error("RuntimeError", "the product licence is not "
                         "initialised", False) == "LICENCE",
          "the british spelling classifies as LICENCE too")
    check(classify_error("ModuleNotFoundError", "No module named 'x'", True)
          == "NOT-IMPORTABLE", "ModuleNotFoundError is NOT-IMPORTABLE")
    none_msg = "'NoneType' object has no attribute 'write'"
    check(classify_error("AttributeError", none_msg, True) == "STDOUT-NONE",
          "a NoneType attribute error that only the task context raises is "
          "STDOUT-NONE")
    check(classify_error("AttributeError", none_msg, False) == "IMPORT-ERROR",
          "the same error that also happens with a real stream is not blamed "
          "on stdout  <-- pinned defect")
    check(classify_error("ValueError", "boom", True) == "IMPORT-ERROR",
          "anything else is IMPORT-ERROR")
    check(classify_error("AttributeError", "no attribute 'x'", True)
          == "IMPORT-ERROR",
          "an AttributeError that is not about NoneType is not blamed on "
          "stdout  <-- pinned defect")

    # ---- compare_module on synthetic results
    base_facts = dict(facts, executable="py", version="3.12.0",
                      enable_user_site=True, stdout_none=False)

    def res(ok=True, top=None, cands=None, etype=None, err=None, tainted=False,
            lic=None, facts_=None, origin=None):
        return {"facts": facts_ or base_facts, "licence": lic,
                "module": {"ok": ok, "origin": origin or top,
                           "top_origin": top,
                           "candidates": cands if cands is not None
                           else ([top] if top else []),
                           "tainted": tainted, "error_type": etype,
                           "error": err, "where": "f.py:1"}}

    good = res(top=ap("/g/site/x.py"))
    codes = lambda fs: [f.code for f in fs]
    check(codes(compare_module("x", False, good, good, None)) == ["OK"],
          "the same clean result in both contexts is one OK finding")
    check(codes(compare_module("x", False, good, res(ok=False, etype="E",
                                                     err="m"), None))
          == ["IMPORT-ERROR"], "a task failure is a FAIL finding")
    check(compare_module("x", False, good, good, None)[0].text
          == "found in site-packages: %s" % ap("/g/site/x.py"),
          "the OK finding names the place the module was found")
    check(codes(compare_module("x", False, res(ok=False, etype="E", err="m"),
                               good, None)) == ["PASSES-ONLY-IN-TASK"],
          "an import that works only in the task context is a WARN")
    check(codes(compare_module("x", False, good, res(top=ap("/u/site/x.py"),
                                                     cands=[ap("/u/site/x.py")]),
                               None)) == ["ORIGIN-DIFFERS"],
          "a different file in the two contexts is ORIGIN-DIFFERS")
    shadow = res(top=ap("/u/site/x.py"),
                 cands=[ap("/u/site/x.py"), ap("/g/site/x.py")])
    check(codes(compare_module("x", False, shadow, shadow, None))
          == ["SHADOWS"],
          "a user-site winner with a hidden copy is SHADOWS")
    late = res(top=ap("/g/site/x.py"),
               cands=[ap("/g/site/x.py"), ap("/g/lib/x.py")])
    check(codes(compare_module("x", False, late, late, None)) == ["OK"],
          "a site-packages winner with a later copy is not flagged")
    check(codes(compare_module("x", False, good, res(
        top=ap("/g/site/x.py"), tainted=True), None)) == ["OK", "UNPROBED"],
          "a module the probe loaded itself is marked UNPROBED, and it does "
          "not warn because nothing differs")
    arc = lambda lic: compare_module("arcpy", False, good, res(
        top=ap("/g/site/x.py"), lic=lic), None)
    check(codes(arc({"product": "ArcInfo", "error": None})) ==
          ["OK", "LICENCE-LEVEL"],
          "a licence level is reported beside the OK")
    check(codes(arc({"product": "NotInitialized", "error": None}))
          == ["LICENCE"],
          "NotInitialized is a LICENCE FAIL, not an OK")
    check(codes(arc({"product": None, "error": "RuntimeError: x"}))
          == ["LICENCE-CHECK"],
          "a ProductInfo() that raises is a LICENCE-CHECK WARN")
    miss = res(ok=False, etype="ModuleNotFoundError", err="No module named 'x'")
    check(codes(compare_module("x", False, miss, miss, None))
          == ["NOT-IMPORTABLE"]
          and compare_module("x", False, miss, miss, None)[0].severity
          == FAIL, "a module missing in both contexts is a FAIL")
    opt = compare_module("x", True, miss, miss, None)
    check(opt[0].severity == WARN and "optional" in opt[0].text,
          "a missing optional import is only a WARN")
    check(compare_module("x", True, good, res(
        ok=False, etype="ValueError", err="m"), None)[0].severity == FAIL,
          "an optional import that fails for another reason is still a FAIL")
    only = compare_module("x", False, res(top=ap("/u/site/x.py")), miss, None)
    check(codes(only) == ["USER-SITE-ONLY"],
          "missing in the task but found in the user site here is "
          "USER-SITE-ONLY  <-- pinned defect")
    elsewhere = compare_module("x", False, good, miss, None)
    check(codes(elsewhere) == ["NOT-IMPORTABLE"],
          "missing in the task but found in site-packages here is not "
          "blamed on the user site")
    crash_t = compare_module("x", False, good, {"crash": "exit 7"}, None)
    check(codes(crash_t) == ["CRASH"] and "task context" in crash_t[0].text
          and "exit 7" in crash_t[0].text, "a task process that died is CRASH")
    crash_h = compare_module("x", False, {"crash": "exit 9"}, good, None)
    check("in the here context" in crash_h[0].text and "exit 9" in crash_h[0].text,
          "a crash only in the here context says here")
    both = compare_module("x", False, {"crash": "exit 9"}, {"crash": "exit 7"},
                          None)[0]
    check("here and task context" in both.text and "exit 7" in both.text
          and "exit 9" not in both.text and both.severity == FAIL,
          "a crash in both contexts says so and quotes the task's")
    check(compare_module("x", False, good, {"crash": "exit 7"}, None)[0]
          .severity == FAIL,
          "a CRASH is a FAIL  <-- pinned defect")
    check(compare_module("x", False, res(ok=False, etype="E", err="m"), good,
                         None)[0].severity == WARN
          and compare_module("x", False, good, res(top=ap("/u/site/x.py"),
                                                   cands=[ap("/u/site/x.py")]),
                             None)[0].severity == WARN
          and compare_module("x", False, shadow, shadow, None)[0].severity
          == WARN,
          "PASSES-ONLY-IN-TASK, ORIGIN-DIFFERS and SHADOWS are warnings "
          "and not failures  <-- pinned defect")
    check([(f.code, f.severity) for f in arc(
        {"product": "NotInitialized", "error": None})] == [("LICENCE", FAIL)]
          and [(f.code, f.severity) for f in arc(
              {"product": None, "error": "RuntimeError: x"})]
          == [("LICENCE-CHECK", WARN)]
          and [(f.code, f.severity) for f in arc(
              {"product": "ArcInfo", "error": None})]
          == [("OK", OK), ("LICENCE-LEVEL", OK)]
          and "account the task runs as was not checked" in arc(
              {"product": "ArcInfo", "error": None})[1].text,
          "a failed licence is a FAIL, a ProductInfo() error is a WARN, a "
          "level is OK and says only this account was checked"
          "  <-- pinned defect")
    ns_win = res(top=ap("/u/site/n"), cands=[ap("/u/site/n"), ap("/g/site/n")])
    ns_win["module"]["namespace_parts"] = [ap("/u/site/n"), ap("/g/site/n")]
    reg_win = res(top=ap("/u/site/n/__init__.py"),
                  cands=[ap("/u/site/n/__init__.py"), ap("/g/site/n")])
    reg_win["module"]["namespace_parts"] = [ap("/g/site/n")]
    check(codes(compare_module("n", False, ns_win, ns_win, None)) == ["OK"]
          and codes(compare_module("n", False, reg_win, reg_win, None))
          == ["SHADOWS"],
          "the parts of a namespace package are not hidden copies, but a "
          "regular package that hides a namespace part is  <-- pinned defect")
    check(hidden_copies(ns_win["module"]) == []
          and hidden_copies(dict(ns_win["module"], namespace_parts=[]))
          == [ap("/g/site/n")],
          "hidden_copies reports no copy when a namespace package wins")
    opt_user = compare_module("x", True, res(top=ap("/u/site/x.py")), miss,
                              None)[0]
    req_user = compare_module("x", False, res(top=ap("/u/site/x.py")), miss,
                              None)[0]
    check((opt_user.code, opt_user.severity) == ("USER-SITE-ONLY", WARN)
          and "optional" in opt_user.text
          and (req_user.code, req_user.severity) == ("USER-SITE-ONLY", FAIL),
          "USER-SITE-ONLY is a FAIL for a required import and a WARN for an "
          "optional one  <-- pinned defect")
    none_facts = dict(base_facts, stdout_none=True)
    none_t = res(ok=False, etype="AttributeError", err=none_msg,
                 facts_=none_facts)
    check(codes(compare_module("x", False, good, none_t, None))
          == ["STDOUT-NONE"]
          and codes(compare_module("x", False, res(
              ok=False, etype="AttributeError", err=none_msg), none_t, None))
          == ["IMPORT-ERROR"]
          and codes(compare_module("x", False, good, res(
              ok=False, etype="AttributeError", err=none_msg), None))
          == ["IMPORT-ERROR"],
          "STDOUT-NONE needs the import to work here and the task streams to "
          "be None; a NoneType error that also happens here, or with real "
          "streams, is IMPORT-ERROR  <-- pinned defect")
    from_env = compare_module("x", False, good, dict(
        none_t, streams_only=good), None)[0]
    check(from_env.code == "IMPORT-ERROR"
          and "--env values cause it" in from_env.text
          and codes(compare_module("x", False, good, dict(
              none_t, streams_only=res(ok=False, etype="E", err="m")), None))
          == ["STDOUT-NONE"]
          and codes(compare_module("x", False, good, dict(
              none_t, streams_only={"crash": "x"}), None)) == ["STDOUT-NONE"],
          "a NoneType error that vanishes with the streams None and no --env "
          "is blamed on the --env values; one that stays is STDOUT-NONE"
          "  <-- pinned defect")
    check(blames_none(good, none_t)
          and not blames_none(good, {"crash": "x"})
          and not blames_none({"crash": "x"}, none_t)
          and not blames_none(res(ok=False, etype="E", err="m"), none_t)
          and not blames_none(good, res(ok=False, etype="E", err="m"))
          and not blames_none(good, good),
          "blames_none is true only when here works and the task fails with "
          "a NoneType attribute error")
    secret_t = res(ok=False, etype="RuntimeError",
                   err="y" * 290 + "s3cr3t-value")
    leaky = compare_module("x", False, good, secret_t, None, False,
                           [("S", "s3cr3t-value")])[0].text
    check("s3cr" not in leaky and "yyy<redacted" in leaky,
          "a module error is redacted before it is cut  <-- pinned defect")
    long_t = res(ok=False, etype="RuntimeError", err="z" * 400)
    check(compare_module("x", False, good, long_t, None)[0].text.count("z")
          == 300,
          "a module error is cut to 300 characters")
    leaky_lic = compare_module("arcpy", False, good, res(
        top=ap("/g/site/x.py"), lic={"product": None, "error":
                                     "RuntimeError: " + "q" * 400 + "tok-1234"}),
                               None, False, [("T", "tok-1234")])
    check(all("tok-1234" not in f.text and len(f.text) < 400
              for f in leaky_lic),
          "a ProductInfo() error is redacted and cut too")
    arcs = [("arcpy", False, good, res(top=ap("/g/site/x.py"), lic={
        "product": "NotInitialized", "error": None})),
            ("arcpy.da", False, good, res(top=ap("/g/site/y.py"), lic={
                "product": "NotInitialized", "error": None}))]
    dup = build_report("py", arcs, None, [])
    check([f["code"] for f in dup["findings"]].count("LICENCE") == 1,
          "arcpy and arcpy.da repeat one licence answer, which is reported "
          "once  <-- pinned defect")
    lvl = [("arcpy", False, good, res(top=ap("/g/site/x.py"), lic={
        "product": "ArcInfo", "error": None})),
           ("arcpy.sa", False, good, res(top=ap("/g/site/y.py"), lic={
               "product": "ArcInfo", "error": None}))]
    check([f["code"] for f in build_report("py", lvl, None, [])["findings"]]
          .count("LICENCE-LEVEL") == 1,
          "the licence level is reported once for several arcpy modules")

    soft_miss = res(ok=False, etype="ModuleNotFoundError",
                    err="No module named 'pkg.thing'")
    parent_miss = res(ok=False, etype="ModuleNotFoundError",
                      err="No module named 'pkg'")
    other_miss = res(ok=False, etype="ModuleNotFoundError",
                     err="No module named 'other'")
    check(compare_module("pkg.thing", False, soft_miss, soft_miss, None,
                         True) == []
          and compare_module("pkg.thing", False, parent_miss, parent_miss,
                             None, True) == [],
          "a soft name missing in both contexts is a plain attribute, or its "
          "package reports itself, so it is no finding  <-- pinned defect")
    check(codes(compare_module("pkg.thing", False, soft_miss, soft_miss,
                               None)) == ["NOT-IMPORTABLE"],
          "the same result for a name that is not soft is a FAIL")
    check(codes(compare_module("pkg.thing", False, other_miss, other_miss,
                               None, True)) == ["NOT-IMPORTABLE"]
          and codes(compare_module("pkg.thing", False, good, soft_miss, None,
                                   True)) == ["NOT-IMPORTABLE"]
          and codes(compare_module("pkg.thing", False, soft_miss, res(
              ok=False, etype="ValueError", err="No module named 'pkg.thing'"),
              None, True)) == ["IMPORT-ERROR"],
          "a soft name is still a finding when another module is missing, "
          "when it works here, or when the error is not a missing module")
    check(names_missing("a.b", "ModuleNotFoundError", "No module named 'a.b'")
          and names_missing("a.b", "ModuleNotFoundError",
                            "No module named 'a'")
          and not names_missing("ab.c", "ModuleNotFoundError",
                                "No module named 'a'")
          and not names_missing("a.b", "ModuleNotFoundError", "other text"),
          "names_missing matches the module or a package above it, not a "
          "name that only shares a prefix")

    # ---- verdict and exit codes
    mk = lambda sev: [Finding("m", "C", sev, "t")]
    check(verdict_of(mk(OK)) == OK and verdict_of(mk(WARN) + mk(OK)) == WARN
          and verdict_of(mk(FAIL) + mk(WARN)) == FAIL
          and verdict_of([]) == OK,
          "the verdict is the worst severity, and no findings is OK")
    check([exit_code(v) for v in (OK, FAIL, WARN)] == [0, 1, 3],
          "OK exits 0, FAIL exits 1, WARN exits 3")

    # ---- task XML
    def xml_bytes(logon, enc="UTF-16", command="job.cmd"):
        body = "<LogonType>%s</LogonType>" % logon if logon else ""
        text = (_TASK_XML % (enc, body)).replace("job.cmd", command)
        return (codecs.BOM_UTF16_LE + text.encode("utf-16-le")
                if enc == "UTF-16" else text.encode("utf-8"))

    pw = parse_task_xml(xml_bytes("Password"))
    check(pw == {"logon_type": "Password", "user_id": "svc-example",
                 "run_level": "LeastPrivilege", "command": "job.cmd"},
          "a UTF-16 task export is read, and the principal user is not the "
          "logon trigger's user  <-- pinned defect")
    check(parse_task_xml(xml_bytes("InteractiveToken", "UTF-8"))["logon_type"]
          == "InteractiveToken", "a UTF-8 task export is read")
    check(parse_task_xml(xml_bytes(None))["logon_type"] is None,
          "a missing LogonType reads as None")
    check(parse_task_xml(xml_bytes(" "))["logon_type"] is None,
          "a blank LogonType reads as None")
    raises(lambda: parse_task_xml(b'<!DOCTYPE t [<!ENTITY a "b">]><Task/>'),
           "a task XML with a DOCTYPE is refused, not expanded"
           "  <-- pinned defect")
    raises(lambda: parse_task_xml(b"<Task><open></Task>"),
           "a task XML that does not parse is refused")
    raises(lambda: parse_task_xml(b"\xff\xfe\x00"),
           "bytes that are not text are refused")
    tf = lambda logon, arc_: task_findings(parse_task_xml(
        xml_bytes(logon)), arc_)[0]
    check(tf("InteractiveToken", True).severity == OK,
          "InteractiveToken is OK")
    check(tf("Password", True).severity == WARN
          and tf("S4U", True).severity == WARN,
          "Password and S4U warn when the script imports arcpy")
    check(tf("Password", False).severity == OK,
          "Password is not a warning for a script that does not use arcpy")
    check(tf(None, True).severity == WARN and "no LogonType" in tf(
        None, True).text, "no LogonType warns when the session is unknown")
    check([resolve_streams(None, c)[0] for c in (
        "C:\\Python313\\python.exe", "/usr/bin/python3.12", "py.exe",
        '"C:\\Program Files\\Python\\python.exe"', "python")]
          == ["real"] * 5
          and [resolve_streams(None, c)[0] for c in (
              "C:\\Python313\\pythonw.exe", "job.cmd", None, "")]
          == ["none"] * 4
          and "console python" in resolve_streams(None, "python.exe")[1]
          and resolve_streams("none", "python.exe") == ("none", None)
          and resolve_streams("real", None) == ("real", None),
          "a task command that names python.exe selects real streams, "
          "pythonw.exe or a wrapper keeps None, and --streams wins  "
          "<-- pinned defect")
    arc_runs = [("arcpy.da", False, good, good)]
    check(build_report("py", arc_runs, None, [], parse_task_xml(
        xml_bytes("Password")))["findings"][-1]["severity"] == WARN
          and build_report("py", [("arcpyx.da", False, good, good)], None, [],
                           parse_task_xml(xml_bytes("Password")))[
                               "findings"][-1]["severity"] == OK,
          "a script that imports only arcpy.da still gets the TASK-LOGON "
          "warning, and a package that only starts with arcpy does not")
    stream_rep = build_report("py", [("m", False, good, good)], None, [], None,
                              (), (), [(n, "sys.stdout.write")
                                       for n in range(2, 9)])
    sf = [f for f in stream_rep["findings"] if f["code"] == "SCRIPT-STREAMS"]
    check(len(sf) == 1 and sf[0]["severity"] == WARN
          and "sys.stdout.write (line 2)" in sf[0]["text"]
          and "line 6)" in sf[0]["text"] and "line 7)" not in sf[0]["text"]
          and stream_rep["verdict"] == WARN,
          "load-time use of a stream is one WARN naming up to five lines, "
          "because exit 0 would say a script is fine that this tool did not "
          "run  <-- pinned defect")
    cmd_rep = build_report("py", [("m", False, good, good)], None, [], {
        "logon_type": None, "user_id": None, "run_level": None,
        "command": "job.cmd --pw s3cr3t-value"}, (), [("S", "s3cr3t-value")])
    check(cmd_rep["task"]["command"] == "job.cmd --pw <redacted:S>"
          and cmd_rep["task"]["user_id"] is None
          and cmd_rep["streams"] == {"mode": "none", "why": None}
          and build_report("py", [("m", False, good, good)], None, [], None,
                           (), (), (), {"mode": "real", "why": "w"})[
                               "streams"] == {"mode": "real", "why": "w"},
          "a secret value in the task command is redacted in the report  "
          "<-- pinned defect")

    # ---- the child, called in this process as `python -c` would call it
    spec = lambda name, blank=False, lead=None: {
        "module": name, "blank": blank, "lead": lead}
    inproc = _child_in_process(spec("json"))
    check(inproc["module"]["ok"] and inproc["facts"]["stdout_none"] is False
          and inproc["module"]["top_origin"].endswith("__init__.py"),
          "the child imports a package and reports its file")
    check(_child_in_process(spec("sys"))["module"]["origin"] == "built-in",
          "a built-in module reports built-in")
    dead = _child_in_process(spec("ipfix_missing_xyz"))["module"]
    check(not dead["ok"] and dead["error_type"] == "ModuleNotFoundError"
          and dead["where"] is None,
          "a missing module reports its error and no location, because the "
          "location would be the import machinery")
    blank = _child_in_process(spec("json", True))
    check(blank["facts"]["stdout_none"] is True,
          "the blank context really sets sys.stdout to None")
    import site
    kept = site.getsitepackages
    del site.getsitepackages
    try:
        legacy = _child_in_process(spec("json"))
    finally:
        site.getsitepackages = kept
    check(legacy["module"]["ok"] and legacy["facts"]["site_dirs"] == [],
          "an interpreter whose site module lacks getsitepackages is probed "
          "with no site directories, not as a crash  <-- pinned defect")
    importlib.import_module("sysconfig")
    kept_mod = sys.modules.pop("sysconfig")
    try:
        taint = _child_in_process(spec("sysconfig"))["module"]["tainted"]
    finally:
        sys.modules["sysconfig"] = kept_mod
    check(taint is True and _child_in_process(spec("json"))["module"][
        "tainted"] is False,
          "a module the probe imports for itself is marked tainted, and a "
          "module it does not import is not")
    lead_dir = os.path.join(tmp, "lead")
    _write(os.path.join(lead_dir, "colorsys.py"), _FIXTURES["colorsys"])
    _write(os.path.join(lead_dir, "ipfix_nofile.py"), _FIXTURES["ipfix_nofile"])
    _write(os.path.join(lead_dir, "ipfix_ns", "inner.py"), "X = 1\n")
    shadowed = _child_in_process(spec("colorsys", lead=lead_dir))["module"]
    check(shadowed["top_origin"] == os.path.join(lead_dir, "colorsys.py")
          and len(shadowed["candidates"]) == 2,
          "a file beside the script wins over the stdlib and both are listed")
    here_dir = os.getcwd()
    os.chdir(lead_dir)
    try:
        no_cwd = _child_in_process(spec("colorsys"))["module"]
    finally:
        os.chdir(here_dir)
    check(len(no_cwd["candidates"]) == 1
          and not no_cwd["top_origin"].startswith(lead_dir),
          "the current directory is not searched, because a task runs a "
          "script file and not -c  <-- pinned defect")
    ns = _child_in_process(spec("ipfix_ns", lead=lead_dir))["module"]
    check(ns["ok"] and ns["origin"] == os.path.join(lead_dir, "ipfix_ns"),
          "a namespace package reports its directory")
    nf = _child_in_process(spec("ipfix_nofile", lead=lead_dir))["module"]
    check(nf["ok"] and nf["origin"] is None,
          "a module that deletes its own __file__ reports no origin")
    other_dir = os.path.join(tmp, "other")
    _write(os.path.join(other_dir, "ipfix_ns", "inner2.py"), "X = 2\n")
    two_ns = _child_in_process(spec("ipfix_ns", lead=lead_dir),
                               path_head=other_dir)["module"]
    check(sorted(two_ns["candidates"]) == sorted([
        os.path.join(lead_dir, "ipfix_ns"), os.path.join(other_dir, "ipfix_ns")]),
          "a namespace directory in two places is listed as two copies")
    _write(os.path.join(lead_dir, "ipfix_pos.py"), "X = 'lead'\n")
    _write(os.path.join(other_dir, "ipfix_pos.py"), "X = 'other'\n")
    importlib.invalidate_caches()
    pos = _child_in_process(spec("ipfix_pos", lead=lead_dir),
                            path_head=other_dir)["module"]
    check(pos["top_origin"] == os.path.join(lead_dir, "ipfix_pos.py"),
          "the script directory goes before every other sys.path entry")
    stdlib_dir = os.path.dirname(os.__file__)
    with_path_head = _child_in_process(spec("json"), path_head=stdlib_dir)
    check(with_path_head["module"]["ok"]
          and len(with_path_head["module"]["candidates"]) == 1,
          "a first sys.path entry that is not empty is left alone, and a "
          "directory listed twice yields one candidate")

    # ---- real probe processes against synthetic modules
    scrub = ("PYTHONNOUSERSITE", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONHOME",
             "PYTHONSTARTUP", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE",
             "IPFIX_LIC", "IPFIX_GATE")
    env = dict((k, v) for k, v in os.environ.items() if k not in scrub)
    userbase = os.path.join(tmp, "userbase")
    sp = os.path.join(tmp, "sp")
    sp2 = os.path.join(tmp, "sp2")
    hid = os.path.join(tmp, "hid")
    # A venv turns the user site off by design, so the real probes use the
    # base interpreter. In a plain install the two are the same file.
    exe = getattr(sys, "_base_executable", sys.executable)
    env["PYTHONUSERBASE"] = userbase
    env["PYTHONPATH"] = sp
    learn = run_context(exe, spec("sys"), child_env(env, {}), 60)
    user_site = learn["facts"]["user_site"]
    check(_under(user_site, userbase),
          "PYTHONUSERBASE moves the user site, so the probe can plant a "
          "module there without touching the real one")
    _write(os.path.join(sp, "arcpy", "__init__.py"), _FAKE_ARCPY)
    _write(os.path.join(sp, "arcpy", "sa.py"), "VALUE = 1\n")
    _write(os.path.join(sp, "ipfancy", "__init__.py"), "ATTR = 1\n")
    _write(os.path.join(sp, "ipfancy", "colour.py"),
           "import sys\nCOLOUR = sys.stdout.isatty()\n")
    for name, body in _FIXTURES.items():
        if name not in ("ipfix_only", "ipfix_dup", "colorsys"):
            _write(os.path.join(sp, name + ".py"), body)
    _write(os.path.join(user_site, "ipfix_only.py"), _FIXTURES["ipfix_only"])
    _write(os.path.join(user_site, "ipfix_dup.py"), _FIXTURES["ipfix_dup"])
    _write(os.path.join(user_site, "ipfix_hidden.pth"), hid + "\n")
    _write(os.path.join(hid, "ipfix_dup.py"), "VALUE = 'the hidden copy'\n")
    _write(os.path.join(sp2, "ipfix_dup.py"), "VALUE = 'a third copy'\n")
    _write(os.path.join(user_site, "ipfix_nsx", "a.py"), "A = 1\n")
    _write(os.path.join(hid, "ipfix_nsx", "b.py"), "B = 2\n")

    def run(names, overrides=None, lead=None, base=None, timeout=60,
            optional=()):
        entries = [(n, n in optional) for n in names]
        runs = probe(exe, entries, base or env, overrides or {},
                     lead, timeout)
        return build_report(exe, runs, lead,
                            list(overrides or {})), runs

    def by_module(report):
        table = {}
        for f in report["findings"]:
            table.setdefault(f["module"], []).append(f["code"])
        return table

    rep, runs = run(["sys", "json"])
    check(rep["verdict"] == OK and by_module(rep) == {
        "sys": ["OK"], "json": ["OK"]},
          "a clean interpreter reports OK for the built-in and a stdlib "
          "package")
    check(rep["contexts"]["here"]["stdout_none"] is False
          and rep["contexts"]["task"]["stdout_none"] is True,
          "the task context ran with sys.stdout None and the here context "
          "with a real stream")
    check(runs[0][3]["facts"]["enable_user_site"] is True,
          "the user site is enabled in the task context unless told "
          "otherwise")

    rep, _ = run(["ipfix_reconf", "ipfix_isatty"])
    check(by_module(rep) == {"ipfix_reconf": ["STDOUT-NONE"],
                             "ipfix_isatty": ["STDOUT-NONE"]}
          and rep["verdict"] == FAIL,
          "reconfigure() and isatty() at import work in a shell and die "
          "with stdout None  <-- pinned defect")
    rep, _ = run(["ipfix_print", "ipfix_logging"])
    check(by_module(rep) == {"ipfix_print": ["OK"], "ipfix_logging": ["OK"]},
          "print() and a default logging handler do not fail with stdout and "
          "stderr None, they are silent  <-- pinned defect")

    rep, _ = run(["ipfix_only"], {"PYTHONNOUSERSITE": "1"})
    check(by_module(rep) == {"ipfix_only": ["USER-SITE-ONLY"]},
          "a package installed only in the user site vanishes when the task "
          "sets PYTHONNOUSERSITE  <-- pinned defect")
    rep, _ = run(["ipfix_only"])
    check(by_module(rep) == {"ipfix_only": ["OK"]},
          "and it imports when the task leaves the user site on")
    rep, _ = run(["ipfix_dup"])
    check(by_module(rep) == {"ipfix_dup": ["SHADOWS"]}
          and rep["verdict"] == WARN
          and hid in rep["findings"][0]["text"],
          "a user-site copy that hides another copy is SHADOWS, naming the "
          "hidden one  <-- pinned defect")
    rep, _ = run(["ipfix_dup"], {"PYTHONPATH": sp + os.pathsep + sp2})
    check(by_module(rep)["ipfix_dup"] == ["ORIGIN-DIFFERS"],
          "a different PYTHONPATH in the task resolves a different copy")
    rep, _ = run(["colorsys"], lead=lead_dir)
    check(by_module(rep) == {"colorsys": ["SHADOWS"]},
          "a file beside the script that shares a stdlib name is SHADOWS")

    # The stub sits first on the path. A machine with the real arcpy still has
    # it as a hidden copy, so SHADOWS and OK are left out of these comparisons.
    def licence_codes(mode, name="arcpy"):
        rep_, _ = run([name], {"IPFIX_LIC": mode} if mode else {},
                      lead=sp)
        return rep_, [c for c in by_module(rep_)[name]
                      if c not in ("OK", "SHADOWS")]

    rep, found = licence_codes(None)
    check(found == ["LICENCE-LEVEL"],
          "the stub arcpy with a licence reports its level")
    rep, found = licence_codes("raise")
    check(found == ["LICENCE"]
          and "imports fine here" in rep["findings"][0]["text"],
          "an arcpy import that raises the licence message only in the task "
          "is LICENCE  <-- pinned defect")
    check(licence_codes("uninit")[1] == ["LICENCE"],
          "an arcpy that imports but reports NotInitialized is LICENCE")
    check(licence_codes("broken")[1] == ["LICENCE-CHECK"],
          "a ProductInfo() that raises is LICENCE-CHECK")
    check(licence_codes("uninit", "arcpy.sa")[1] == ["LICENCE"],
          "a script that imports only arcpy.sa still gets the licence check"
          "  <-- pinned defect")

    rep, _ = run(["ipfix_boom"])
    check(by_module(rep) == {"ipfix_boom": ["IMPORT-ERROR"]}
          and "also fails here" in rep["findings"][0]["text"]
          and "ipfix_boom.py:1" in rep["findings"][0]["text"],
          "an import that fails everywhere is IMPORT-ERROR, not blamed on "
          "the task")
    rep, _ = run(["ipfix_gate"], {"IPFIX_GATE": "open"})
    check(by_module(rep) == {"ipfix_gate": ["PASSES-ONLY-IN-TASK"]},
          "an import that fails here and works in the task is a WARN")
    rep, _ = run(["ipfix_absent_a", "ipfix_absent_b"],
                 optional=("ipfix_absent_b",))
    check([f["severity"] for f in rep["findings"]] == [FAIL, WARN],
          "a missing required import fails and a missing optional one warns")
    rep, _ = run(["ipfix_die"])
    check(by_module(rep) == {"ipfix_die": ["CRASH"]}
          and "exit 7" in rep["findings"][0]["text"]
          and rep["modules"][0]["here"] == "crash"
          and rep["modules"][0]["task"] == "crash",
          "a module that kills the process is CRASH with its exit code")
    dm = run_context(exe, spec("ipfix_diemsg"), child_env(env, {}), 30)
    check(dm == {"crash": "exit 7 with no result: dying words"},
          "a dead probe reports the last line of its stderr")
    noeol = run_context(exe, spec("ipfix_noeol"), child_env(env, {}), 30)
    check(noeol["module"]["ok"] and run(["ipfix_noeol"])[0]["verdict"] == OK,
          "text a module writes at import without a newline does not hide "
          "the result line  <-- pinned defect")
    rep, _ = run(["json", "ipfix_reconf"])
    check(rep["modules"] == [
        {"name": "json", "optional": False, "here": "ok", "task": "ok"},
        {"name": "ipfix_reconf", "optional": False, "here": "ok",
         "task": "fail"}], "the report records ok, fail and crash per context")
    shown = render(rep)
    check("ipfix_reconf  ok     fail" in shown
          and "json          ok     ok" in shown
          and shown[-1] == "verdict: FAIL (1 fail, 0 warn)"
          and [i for i, l in enumerate(shown) if l.startswith("FAIL")][0]
          < [i for i, l in enumerate(shown) if l.startswith("OK")][0],
          "the table shows each context, findings list the worst first, and "
          "the verdict counts them once")
    rep, _ = run(["ipfix_hang"], timeout=1.5)
    check(by_module(rep) == {"ipfix_hang": ["CRASH"]}
          and "timed out" in rep["findings"][0]["text"],
          "a module that hangs is CRASH after the timeout")
    gone = run_context(os.path.join(tmp, "no-such-python"), spec("sys"),
                       env, 10)
    check("cannot start" in gone["crash"],
          "an interpreter that cannot start is a crash result")
    chatty = run_context(exe, spec("ipfix_atexit"), child_env(env, {}), 30)
    check("facts" in chatty,
          "output a module writes after the result line does not hide it"
          "  <-- pinned defect")
    check(isinstance(_child_in_process(spec("importlib"))["module"][
              "tainted"], bool), "the tainted flag is a boolean")

    rep, _ = run(["ipfix_nsx.a", "ipfix_nsx.b"])
    check(by_module(rep) == {"ipfix_nsx.a": ["OK"], "ipfix_nsx.b": ["OK"]},
          "a namespace package split between the user site and another "
          "directory merges and is not SHADOWS  <-- pinned defect")
    rep, _ = run(["ipfix_envnone"], {"IPFIX_MODE": "off"})
    check(by_module(rep) == {"ipfix_envnone": ["IMPORT-ERROR"]}
          and "--env values cause it" in rep["findings"][0]["text"],
          "a NoneType error that an --env value causes is not blamed on the "
          "streams  <-- pinned defect")
    rep, _ = run(["ipfix_reconf"], {"IPFIX_X": "1"})
    check(by_module(rep) == {"ipfix_reconf": ["STDOUT-NONE"]},
          "an --env value that is not the cause leaves a stream error as "
          "STDOUT-NONE")
    rep, _ = run(["ipfix_arrow"], base=dict(env, PYTHONIOENCODING="ascii"))
    check(by_module(rep) == {"ipfix_arrow": ["OK"]},
          "the here context writes UTF-8 like a console, so a module that "
          "prints a character outside the locale code page is not a "
          "difference  <-- pinned defect")
    rep, _ = run(["ipfix_dunder"])
    check(by_module(rep) == {"ipfix_dunder": ["PASSES-ONLY-IN-TASK"]},
          "the task context sets sys.__stdin__, __stdout__ and __stderr__ "
          "to None as well")
    fake = run_context(exe, spec("ipfix_fakemark"), child_env(env, {}), 30)
    check("facts" in fake and "bogus" not in fake,
          "a result line that a module prints itself does not replace the "
          "real one, because the last marker line wins")
    rep, _ = run(["ipfix_sysexit"])
    check(by_module(rep) == {"ipfix_sysexit": ["IMPORT-ERROR"]}
          and "SystemExit: bye" in rep["findings"][0]["text"],
          "a module that raises SystemExit at import is a finding and not a "
          "dead probe")
    started = time.time()
    rep, _ = run(["ipfix_hold"], timeout=1.5)
    check(by_module(rep) == {"ipfix_hold": ["CRASH"]}
          and time.time() - started < 9,
          "a timeout does not wait for a grandchild that holds the probe's "
          "pipes open  <-- pinned defect")
    skipped = probe(exe, [("json", False), ("ipfix_fb_a", True)], env, {},
                    None, 60, {"ipfix_fb_a": set(["json"])}, True)
    needed = probe(exe, [("ipfix_absent_k", False), ("json", True)], env, {},
                   None, 60, {"json": set(["ipfix_absent_k"])}, True)
    check([r[0] for r in skipped] == ["json"]
          and [r[0] for r in needed] == ["ipfix_absent_k", "json"],
          "a fallback is skipped when the module it replaces imports in both "
          "contexts, and probed when that module is missing")

    # arcpy's licence path, in this process, against the stub
    def arcpy_here(mode, name="arcpy"):
        os.environ["IPFIX_LIC"] = mode
        try:
            return _child_in_process(spec(name), path_head=sp)
        finally:
            del os.environ["IPFIX_LIC"]

    check(arcpy_here("ok")["licence"] == {"product": "ArcInfo", "error": None},
          "the child records the licence level of an arcpy that imports")
    check(arcpy_here("broken")["licence"]["error"].startswith("RuntimeError"),
          "the child records a ProductInfo() that raises")
    check(arcpy_here("uninit", "arcpy.sa")["licence"]
          == {"product": "NotInitialized", "error": None},
          "the child checks the licence for a submodule of arcpy too")
    lic_dead = arcpy_here("raise")
    check(lic_dead["licence"] is None and not lic_dead["module"]["ok"]
          and "Product License" in lic_dead["module"]["error"],
          "the child records an arcpy import that raises the licence message")

    # ---- the CLI, in this process and as a real command
    def cli(argv, environ=None, stdout=None):
        if "--python" not in argv:
            argv = ["--python", exe] + list(argv)
        out = io.StringIO() if stdout is None else stdout
        err = io.StringIO()
        saved = os.environ.copy()
        os.environ.clear()
        os.environ.update(environ or env)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(
                    err):
                code = main(argv)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, getattr(out, "getvalue", str)(), err.getvalue()

    code, out, err = cli(["--module", "json"])
    check(code == 0 and "verdict: OK (0 fail, 0 warn)" in out,
          "main exits 0 and prints a verdict for a clean module")
    check("here: python" in out and "task: python" in out
          and "stdout None" in out and "user site on" in out,
          "the report states both contexts")
    code, out, err = cli(["--module", "ipfix_reconf", "--env",
                          "IPFIX_SECRET=hunter2-value"])
    check(code == 1 and "STDOUT-NONE" in out,
          "main exits 1 when an import dies in the task context")
    check("hunter2-value" not in out + err and "IPFIX_SECRET" in out,
          "an env value never reaches the output, only its name"
          "  <-- pinned defect")
    code, out, _ = cli(["--module", "ipfix_dup"])
    check(code == 3, "main exits 3 on a warning only")

    script = os.path.join(tmp, "nightly.py")
    _write(script, "import json\nfrom colorsys import rgb_to_hls\n"
           "try:\n    import ipfix_absent_c\nexcept ImportError:\n    pass\n"
           "def later():\n    import ipfix_absent_d\n")
    shutil.copy(os.path.join(lead_dir, "colorsys.py"),
                os.path.join(tmp, "colorsys.py"))
    code, out, err = cli(["--script", script])
    check(code == 3 and "ipfix_absent_c" in out and "ipfix_absent_d" not in out
          and "SHADOWS" in out and "optional" in out,
          "--script probes load-time imports with the script directory first, "
          "and flags the shadowed stdlib name")
    check(not os.path.exists(os.path.join(tmp, "__pycache__")),
          "probing wrote no bytecode beside the script")
    marker = os.path.join(tmp, "script-ran.txt")
    ran = os.path.join(tmp, "ran.py")
    _write(ran, "import json\nopen(%r, 'w').write('ran')\n" % marker)
    check(cli(["--script", ran])[0] == 0 and not os.path.exists(marker),
          "the script is parsed and never run  <-- pinned defect")
    bom = os.path.join(tmp, "bom.py")
    with open(bom, "wb") as handle:
        handle.write(codecs.BOM_UTF8 + b"import json\n")
    check(cli(["--script", bom])[0] == 0,
          "a script saved with a byte order mark is read")
    xml = os.path.join(tmp, "task.xml")
    with open(xml, "wb") as handle:
        handle.write(xml_bytes("Password"))
    code, out, _ = cli(["--module", "arcpy", "--task-xml", xml])
    check(code == 3 and "TASK-LOGON" in out and "LogonType=Password" in out,
          "--task-xml reports a Password logon as a warning for an arcpy "
          "script")
    code, out, _ = cli(["--task-xml", xml])
    check(code == 3 and "TASK-LOGON" in out and "module(s)" in out
          and "NOTHING-PROBED" in out and "verdict: WARN" in out,
          "--task-xml alone reads the task, probes nothing and exits 3, not "
          "0  <-- pinned defect")

    report_path = os.path.join(tmp, "report.json")
    code, out, _ = cli(["--module", "ipfix_absent_q"])
    check(code == 1 and "NOT-IMPORTABLE" in out
          and "verdict: FAIL (1 fail, 0 warn)" in out,
          "main exits 1 for a missing required module  <-- pinned defect")
    fallback = os.path.join(tmp, "fallback.py")
    _write(fallback, "try:\n    import json\nexcept ImportError:\n"
           "    import ipfix_absent_e\n")
    code, out, _ = cli(["--script", fallback])
    check(code == 0 and "ipfix_absent_e" not in out and "1 module(s)" in out,
          "a fallback import is not probed while the import it replaces "
          "works, so a healthy script is not a WARN  <-- pinned defect")
    code, out, _ = cli(["--script", fallback, "--module", "ipfix_absent_e"])
    check(code == 1 and "NOT-IMPORTABLE" in out and "optional" not in out,
          "a fallback that is also named with --module is required")
    both_gone = os.path.join(tmp, "both_gone.py")
    _write(both_gone, "try:\n    import ipfix_absent_f\nexcept ImportError:\n"
           "    import ipfix_absent_g\n")
    code, out, _ = cli(["--script", both_gone])
    check(code == 3 and "ipfix_absent_f" in out and "ipfix_absent_g" in out,
          "a fallback is probed when the import it replaces is missing")
    task_only = os.path.join(tmp, "task_only.py")
    _write(task_only, "try:\n    import ipfix_only\nexcept ImportError:\n"
           "    import ipfix_absent_h\n")
    code, out, _ = cli(["--script", task_only, "--env", "PYTHONNOUSERSITE=1"])
    check(code == 3 and "ipfix_absent_h" in out and "USER-SITE-ONLY" in out,
          "a fallback is probed when the import it replaces fails only in "
          "the task context")
    dies = os.path.join(tmp, "dies.py")
    _write(dies, "import sys\ntry:\n    import ipfix_absent_x\n"
           "except ImportError:\n    print('missing')\n    sys.exit(1)\n"
           "try:\n    import ipfix_only\nexcept ImportError:\n"
           "    raise SystemExit(1)\n")
    code, out, _ = cli(["--script", dies, "--env", "PYTHONNOUSERSITE=1"])
    check(code == 1 and "optional" not in out and "USER-SITE-ONLY" in out
          and "NOT-IMPORTABLE" in out,
          "a missing import whose handler exits is a FAIL and exits 1, not "
          "an optional WARN  <-- pinned defect")
    nothing = os.path.join(tmp, "nothing.py")
    _write(nothing, "x = 1\ndef f():\n    import json\n")
    code, out, _ = cli(["--script", nothing])
    check(code == 3 and "NOTHING-PROBED" in out
          and "nothing was probed" in out,
          "a script with no load-time import is a WARN that says nothing was "
          "probed, not a clean OK  <-- pinned defect")
    lib = os.path.join(tmp, "uselib.py")
    _write(lib, "from ipfancy import ATTR, colour\n")
    code, out, _ = cli(["--script", lib])
    check(code == 1 and "STDOUT-NONE" in out and "ipfancy.colour" in out
          and "ipfancy.ATTR" not in out and "2 module(s)" in out,
          "from pkg import sub probes the submodule, and a plain attribute is "
          "no row and no finding  <-- pinned defect")
    attr = os.path.join(tmp, "attr.py")
    _write(attr, "from collections import OrderedDict\n")
    code, out, _ = cli(["--script", attr])
    check(code == 0 and "1 module(s)" in out,
          "from a stdlib package import a class is one module and exits 0")
    gone = os.path.join(tmp, "gone.py")
    _write(gone, "from ipfix_absent_s import thing\n")
    code, out, _ = cli(["--script", gone])
    check(code == 1 and out.count("NOT-IMPORTABLE") == 1,
          "a missing package is reported once, not again for its name")
    code, out, err = cli(["--module", "ipfix_leak", "--env",
                          "IPFIX_TOKEN=s3cr3t-tok3n", "--env",
                          "IPFIX_PLAIN=plainvalue1", "--out", report_path,
                          "--apply"],
                         dict(env, IPFIX_AMBIENT_KEY="ambient-k3y-value"))
    with open(report_path) as handle:
        leaked = handle.read()
    check(code == 1 and "<redacted:IPFIX_TOKEN>" in out
          and "<redacted:IPFIX_PLAIN>" in out
          and "<redacted:IPFIX_AMBIENT_KEY>" in out
          and not any(v in out + err + leaked for v in (
              "s3cr3t-tok3n", "plainvalue1", "ambient-k3y-value")),
          "an env value a module echoes in its error is redacted in the "
          "output and in the JSON report  <-- pinned defect")
    os.remove(report_path)
    code, out, err = cli(["--module", "json", "--env", "=hunter2-typo-secret"])
    check(code == 64 and "hunter2-typo-secret" not in out + err,
          "a mistyped --env never echoes its value  <-- pinned defect")
    notpy = os.path.join(tmp, "notpy.txt")
    _write(notpy, "not an interpreter\n")
    code, out, err = cli(["--module", "json", "--python", notpy])
    check(code == 2 and "could not run the probe" in err and not out,
          "an interpreter that cannot run the probe exits 2, not the 1 that "
          "means an import fails  <-- pinned defect")
    code, out, err = cli(["--module", "json", "--timeout", "0.001"])
    check(code == 2 and "timed out" in err,
          "a probe that exceeds the timeout before any import is exit 2")
    check(all(cli(["--module", "json", "--timeout", t])[0] == 64
              for t in ("nan", "inf", "1e7", "-1")),
          "a timeout that is not a number, infinite, huge or negative is a "
          "usage error  <-- pinned defect")
    before = open(script).read()
    code, _, err = cli(["--script", script, "--out", script, "--apply"])
    check(code == 64 and "must not name" in err and open(script).read()
          == before, "--out may not be the --script file, so --apply cannot "
          "overwrite the script  <-- pinned defect")
    code, _, err = cli(["--task-xml", xml, "--out", xml])
    check(code == 64 and "must not name" in err,
          "--out may not be the --task-xml file either")
    linked = os.path.join(tmp, "linked.py")
    os.link(script, linked)
    code, _, err = cli(["--script", script, "--out", linked, "--apply"])
    check(code == 64 and "must not name" in err and open(script).read()
          == before,
          "--out may not be a hard link to the --script file  "
          "<-- pinned defect")
    helper = os.path.join(tmp, "helper_out.py")
    _write(helper, "VALUE = 'keep me'\n")
    code, _, err = cli(["--module", "json", "--out", helper, "--apply"])
    check(code == 64 and "not an importprobe report" in err
          and open(helper).read() == "VALUE = 'keep me'\n",
          "--apply will not overwrite an existing file that is not a report"
          "  <-- pinned defect")
    other_json = os.path.join(tmp, "other.json")
    _write(other_json, '{"a": 1}')
    list_json = os.path.join(tmp, "list.json")
    _write(list_json, "[1]")
    check(cli(["--module", "json", "--out", other_json, "--apply"])[0] == 64
          and cli(["--module", "json", "--out", list_json, "--apply"])[0] == 64
          and cli(["--module", "json", "--out", tmp, "--apply"])[0] == 64,
          "a JSON file without the report keys, a JSON list and a directory "
          "are not overwritten either")
    code, _, err = cli(["--module", "json", "--apply"])
    check(code == 64 and "--apply needs --out" in err,
          "--apply without --out is a usage error even when a module is "
          "named  <-- pinned defect")
    real_self = globals()["self_test"]
    globals()["self_test"] = lambda: 5
    try:
        passthrough = cli(["--self-test"])[0]
    finally:
        globals()["self_test"] = real_self
    check(passthrough == 5, "--self-test returns the self-test exit code")

    code, out, _ = cli(["--module", "json", "--out", report_path])
    check(code == 0 and "Check only" in out
          and not os.path.exists(report_path),
          "--out without --apply writes nothing  <-- pinned defect")
    code, out, _ = cli(["--module", "json", "--out", report_path, "--apply"])
    saved = json.load(open(report_path))
    check(code == 0 and saved["verdict"] == OK
          and saved["modules"][0]["name"] == "json",
          "--apply writes the report as JSON")
    code, out, err = cli(["--module", "json", "--out", report_path, "--apply"])
    check(code == 0 and "wrote" in out,
          "--apply replaces an earlier report")
    code, out, err = cli(["--module", "json", "--out",
                          os.path.join(tmp, "nodir", "r.json"), "--apply"])
    check(code == 2 and "could not write" in err,
          "a report that cannot be written exits 2")
    jp_path = os.path.join(tmp, u"\u65e5\u672c.json")
    narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    code = cli(["--module", "json", "--out", jp_path, "--apply"],
               stdout=narrow)[0]
    narrow.flush()
    check(code == 0 and os.path.exists(jp_path)
          and b"wrote " in narrow.buffer.getvalue(),
          "an --out path outside the console code page is written and "
          "printed with replacements, not an exit 70  <-- pinned defect")
    code, out, err = cli(["--module", "json", "--out", jp_path])
    check(code == 0 and "Check only" in out and "?" in out,
          "the check-only line is ASCII too")

    os.remove(report_path)
    for flag in ("--ap", "--app", "--appl"):
        code, out, err = cli(["--module", "json", "--out", report_path, flag])
        check(code == 64 and not os.path.exists(report_path),
              "the prefix %s of --apply is refused by the parser  "
              "<-- pinned defect" % flag)
    code, _, err = cli(["--module", "json", "--ou", report_path, "--apply"])
    check(code == 64 and "--ou" in err,
          "a prefix of --out is refused too")
    check(cli(["--apply"])[0] == 64, "--apply without --out is a usage error")
    check(cli([])[0] == 64, "naming nothing to probe is a usage error")
    check(cli(["--module", "json", "--timeout", "0"])[0] == 64,
          "a zero timeout is a usage error")
    check(cli(["--module", "json", "--env", "BAD"])[0] == 64,
          "a malformed --env is a usage error")
    check(cli(["--module", "json", "--python",
               os.path.join(tmp, "nope")])[0] == 2,
          "a missing interpreter exits 2")
    check(cli(["--script", os.path.join(tmp, "nope.py")])[0] == 2,
          "a missing script exits 2")
    broken_py = os.path.join(tmp, "broken.py")
    _write(broken_py, "def broken(:\n")
    check(cli(["--script", broken_py])[0] == 2,
          "a script that does not parse exits 2")
    bad_xml = os.path.join(tmp, "bad.xml")
    _write(bad_xml, "<Task><open></Task>")
    check(cli(["--task-xml", bad_xml])[0] == 2,
          "a task XML that does not parse exits 2")
    check(cli(["--self-test", "--bogus"])[0] == 64,
          "an unknown flag is a usage error, not a crash")

    class Boom(Exception):
        pass

    real_probe = globals()["probe"]

    def exploding(*_a, **_k):
        raise Boom("probe")

    globals()["probe"] = exploding
    try:
        code, _, err = cli(["--module", "json"])
    finally:
        globals()["probe"] = real_probe
    check(code == 70 and "unexpected Boom: probe" in err,
          "an unexpected exception exits 70, not the 1 that means an import "
          "fails  <-- pinned defect")

    streamy = os.path.join(tmp, "streamy.py")
    _write(streamy, "import sys\nsys.stdout.reconfigure(encoding='utf-8')\n"
           "import json\n")
    code, out, _ = cli(["--script", streamy])
    check(code == 3 and "SCRIPT-STREAMS" in out
          and "sys.stdout.reconfigure (line 2)" in out,
          "a script that touches sys.stdout when it loads is a WARN and "
          "not a clean exit 0  <-- pinned defect")
    code, out, _ = cli(["--script", streamy, "--streams", "real"])
    check(code == 0 and "SCRIPT-STREAMS" not in out,
          "--streams real leaves that script alone, because the stream "
          "exists")
    check(cli(["--script", lib, "--streams", "real"])[0] == 0
          and cli(["--script", lib, "--streams", "none"])[0] == 1
          and cli(["--module", "json", "--streams", "bogus"])[0] == 64,
          "--streams real removes the STDOUT-NONE failure, none keeps it, "
          "and any other value is a usage error")
    task_py = os.path.join(tmp, "task_py.xml")
    with open(task_py, "wb") as handle:
        handle.write(xml_bytes("InteractiveToken",
                               command="C:\\Python313\\python.exe"))
    code, out, _ = cli(["--script", lib, "--task-xml", task_py])
    check(code == 0 and "streams: the task command python.exe" in out
          and "task: python" in out and "stdout None" not in out,
          "a task XML whose command is python.exe is probed with real "
          "streams, because python.exe has a console  <-- pinned defect")
    task_pw = os.path.join(tmp, "task_pw.xml")
    with open(task_pw, "wb") as handle:
        handle.write(xml_bytes("InteractiveToken",
                               command="C:\\Python313\\pythonw.exe"))
    code, out, _ = cli(["--script", lib, "--task-xml", task_pw])
    check(code == 1 and "STDOUT-NONE" in out and "streams:" not in out,
          "a task XML whose command is pythonw.exe keeps the None streams")
    check(cli(["--script", lib, "--task-xml", task_py, "--streams",
               "none"])[0] == 1,
          "--streams none overrides the python.exe command")
    cp = os.path.join(tmp, "cp1252.py")
    with open(cp, "wb") as handle:
        handle.write(b"# -*- coding: cp1252 -*-\n# caf\xe9\nimport json\n")
    nocoding = os.path.join(tmp, "nocoding.py")
    with open(nocoding, "wb") as handle:
        handle.write(b"# coding: no-such-codec-xyz\nimport json\n")
    badbytes = os.path.join(tmp, "badbytes.py")
    with open(badbytes, "wb") as handle:
        handle.write(b"import json\n# caf\xe9\n")
    check(cli(["--script", cp])[0] == 0 and cli(["--script", nocoding])[0] == 2
          and cli(["--script", badbytes])[0] == 2,
          "a script with a coding comment is read in that encoding, and an "
          "unknown codec or undecodable bytes exit 2  <-- pinned defect")
    code, out, err = cli(["--module", "json", "--python",
                          os.path.basename(exe)],
                         dict(env, PATH=os.path.dirname(exe)))
    check(code == 0 and "verdict: OK" in out,
          "--python may be a bare name found on PATH  <-- pinned defect")
    code, _, err = cli(["--module", "json", "--python", "ipfix-no-such-py"])
    check(code == 2 and "not a file and not a name on PATH" in err,
          "a bare name that is not on PATH exits 2 and says so")
    secret_py = os.path.join(tmp, "ipfix-s3cr3t-nm-notpy.txt")
    _write(secret_py, "not an interpreter\n")
    code, out, err = cli(["--module", "json", "--python", secret_py, "--env",
                          "IPFIX_SECRET=ipfix-s3cr3t-nm"])
    check(code == 2 and "ipfix-s3cr3t-nm" not in out + err
          and "<redacted:IPFIX_SECRET>" in err,
          "the error printed when the interpreter cannot start is redacted"
          "  <-- pinned defect")
    code, out, err = cli(["--module", "ipfix_envq", "--env",
                          'IPFIX_QUOTED=pa"ss\\word99'])
    check(code == 1 and "ipfix_envq" in out and "word99" not in out + err
          and "<redacted:IPFIX_QUOTED>" in out,
          "a value that a module shows through repr(), with its backslash "
          "doubled, is still redacted  <-- pinned defect")
    code, out, err = cli(["--module", "ipfix_long", "--env",
                          "IPFIX_LONGSEC=longsecret-abcdef-12345"])
    check(code == 1 and "longsecret" not in out + err
          and "abcdef" not in out + err,
          "a secret that straddles the 300 character cut leaks no prefix  "
          "<-- pinned defect")
    task_cmd = os.path.join(tmp, "task_cmd.xml")
    with open(task_cmd, "wb") as handle:
        handle.write(xml_bytes("InteractiveToken",
                               command="job-s3cr3t-cmd.cmd"))
    cmd_report = os.path.join(tmp, "cmd_report.json")
    code, out, err = cli(["--module", "json", "--task-xml", task_cmd, "--env",
                          "IPFIX_SECRET=s3cr3t-cmd", "--out", cmd_report,
                          "--apply"])
    with open(cmd_report) as handle:
        cmd_json = handle.read()
    check(code == 0 and "s3cr3t-cmd" not in cmd_json + out + err
          and "svc-example" in cmd_json,
          "a known secret inside the task command is redacted in the JSON "
          "report, and the report holds the task account name")

    # The command as a user types it, in a real process.
    here_file = os.path.abspath(__file__)
    proc = subprocess.run([exe, here_file, "--module", "json",
                           "--module", "ipfix_reconf"], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = proc.stdout.decode("ascii")
    check(proc.returncode == 1 and "ipfix_reconf" in text
          and "verdict: FAIL" in text and not proc.stderr,
          "the command run as a process exits 1 and prints an ASCII verdict")

    # ---- render
    lines = render({"python": "py", "env_overrides": [], "modules": [],
                    "contexts": {"here": None, "task": None},
                    "findings": [], "task": None, "verdict": OK})
    check(lines[0] == "importprobe: py, 0 module(s)"
          and lines[-1] == "verdict: OK (0 fail, 0 warn)",
          "an empty report renders its header and verdict")
    odd = render({"python": "py", "env_overrides": ["A"], "modules": [
        {"name": "m", "optional": False, "here": "crash", "task": "crash"}],
        "contexts": {"here": None, "task": dict(
            base_facts, enable_user_site=None, stdout_none=True)},
        "findings": [{"module": "m", "code": "C", "severity": FAIL,
                      "text": u"caf\u00e9"}], "task": None, "verdict": FAIL})
    check(all(ord(c) < 128 for line in odd for c in line)
          and any("no probe returned a result" in line for line in odd)
          and any("disabled by the interpreter" in line and "env set: A"
                  in line for line in odd),
          "the report is ASCII even when a module's error text is not  "
          "<-- pinned defect")

    mixed = render({"python": "py", "env_overrides": [], "modules": [],
                    "contexts": {"here": None, "task": None},
                    "findings": [
                        {"module": "m", "code": "A", "severity": OK,
                         "text": "t"},
                        {"module": "m", "code": "B", "severity": WARN,
                         "text": "t"},
                        {"module": "m", "code": "C", "severity": FAIL,
                         "text": "t"}], "task": None, "verdict": FAIL})
    check([l.split()[0] for l in mixed if l[:2] in ("OK", "WA", "FA")]
          == ["FAIL", "WARN", "OK"]
          and mixed[-1] == "verdict: FAIL (1 fail, 1 warn)"
          and any("nothing was probed" in l for l in mixed),
          "findings print FAIL, WARN, OK in that order and the counts are "
          "exact  <-- pinned defect")
    soft_run = [("pkg.thing", False, soft_miss, soft_miss),
                ("pkg", False, good, good)]
    soft_rep = build_report("py", soft_run, None, [], None, set(["pkg.thing"]))
    check([m["name"] for m in soft_rep["modules"]] == ["pkg"]
          and soft_rep["contexts"]["here"] is not None
          and soft_rep["verdict"] == OK,
          "a soft name that is not a finding gets no row")
    empty_rep = build_report("py", [], None, [])
    check(empty_rep["verdict"] == WARN
          and [f["code"] for f in empty_rep["findings"]] == ["NOTHING-PROBED"],
          "an empty run is a WARN, not a clean OK, whatever asked for it  "
          "<-- pinned defect")

    # ---- summary and importing the module runs nothing
    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    spec_ = importlib.util.spec_from_file_location("importprobe_imported",
                                                   here_file)
    imported = importlib.util.module_from_spec(spec_)
    buf = io.StringIO()
    before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec_.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = before
    check(buf.getvalue() == "" and callable(imported.compare_module),
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


if __name__ == "__main__":
    sys.exit(main())
