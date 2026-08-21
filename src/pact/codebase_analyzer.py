"""Codebase analyzer — mechanical AST-based analysis (no LLM).

Discovers source/test files, extracts function signatures, computes
cyclomatic complexity, maps test coverage, and detects security-sensitive
branches. All stdlib, no external dependencies.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import textwrap
import posixpath
from pathlib import Path

from pact.schemas_testgen import (
    CodebaseAnalysis,
    CoverageEntry,
    CoverageMap,
    ExtractedFunction,
    ExtractedParameter,
    SecurityAuditReport,
    SecurityFinding,
    SecurityRiskLevel,
    SourceFile,
    TestFile,
)

logger = logging.getLogger(__name__)

# Directories to always skip
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env",
    "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".pact", ".claude",
    ".tox", ".nox",
    "dist", "build", "egg-info",
})

# Security-sensitive variable names (lowercase)
SECURITY_VARIABLES = frozenset({
    "admin", "is_admin", "role", "roles", "permission", "permissions",
    "authorized", "auth", "token", "tokens", "privilege", "privileges",
    "credential", "credentials", "secret", "secrets", "password",
    "passwords", "api_key", "api_keys", "sudo", "root", "grant",
    "superuser", "is_superuser", "is_staff", "is_authenticated",
})

# Security-sensitive function call patterns (lowercase prefixes)
SECURITY_CALL_PREFIXES = (
    "grant_", "revoke_", "elevate_", "authenticate", "authorize",
    "set_role", "set_permission", "check_permission", "check_role",
    "verify_token", "validate_token", "check_auth",
)


# ── File Discovery ─────────────────────────────────────────────────


def _should_skip_dir(name: str) -> bool:
    """Check if a directory should be skipped."""
    return name in SKIP_DIRS or name.endswith(".egg-info")


def _resolve_file_list(
    root: Path,
    value: str,
) -> list[str] | None:
    """Resolve an ``--include`` / ``--exclude`` value to a list of paths.

    *value* may be:
    - ``"-"`` → read relative paths from stdin (one per line)
    - An existing **file** → read relative paths from it
    - An existing **directory** → return ``None`` (caller should use as subtree root)
    - Otherwise → return ``None``
    """
    import sys

    if value == "-":
        return [line.strip() for line in sys.stdin if line.strip()]

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_file():
        return [line.strip() for line in candidate.read_text().splitlines() if line.strip()]
    return None  # Caller treats as directory


def discover_source_files(
    root: str | Path,
    language: str = "python",
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[SourceFile]:
    """Walk directory tree and find source files, skipping common non-source dirs.

    Args:
        root: Project root directory.
        language: Source language.
        include: If provided, each entry is either a subdirectory to restrict
            the walk to, or a file/stdin (``-``) containing relative paths.
            When explicit file paths are resolved, only those files are used
            (directory walk is skipped).
        exclude: Additional directory names to skip during the walk.

    Returns list of SourceFile with path set but functions not yet extracted.
    """
    root = Path(root).resolve()
    ext_map = {"python": ".py", "typescript": ".ts", "javascript": ".js", "rust": ".rs"}
    ext = ext_map.get(language, ".py")

    extra_skip = frozenset(exclude) if exclude else frozenset()

    # ── Resolve includes ──────────────────────────────────────────
    explicit_files: list[str] | None = None
    include_dirs: list[Path] | None = None

    if include:
        explicit_files = []
        include_dirs = []
        for inc in include:
            resolved = _resolve_file_list(root, inc)
            if resolved is not None:
                explicit_files.extend(resolved)
            else:
                # Treat as directory
                d = Path(inc)
                if not d.is_absolute():
                    d = root / d
                if d.is_dir():
                    include_dirs.append(d)
                # else: silently skip non-existent paths
        if not explicit_files:
            explicit_files = None
        if not include_dirs:
            include_dirs = None

    # ── Fast path: explicit file list from stdin / file ───────────
    if explicit_files:
        source_files: list[SourceFile] = []
        for rel_path in explicit_files:
            # Normalise to be relative to root
            p = Path(rel_path)
            if p.is_absolute():
                try:
                    rel_path = str(p.relative_to(root))
                except ValueError:
                    continue  # Outside root — skip
            if not rel_path.endswith(ext):
                continue
            source_files.append(SourceFile(path=rel_path))
        return source_files

    # ── Standard walk (optionally scoped to include_dirs) ─────────
    walk_roots = include_dirs if include_dirs else [root]
    source_files = []

    for walk_root in walk_roots:
        for dirpath, dirnames, filenames in os.walk(walk_root):
            # Filter out skip dirs in-place to prevent os.walk from descending
            dirnames[:] = [
                d for d in dirnames
                if not _should_skip_dir(d) and d not in extra_skip
            ]
            # Rust builds into target/ — always skip
            if language == "rust":
                dirnames[:] = [d for d in dirnames if d != "target"]

            rel_dir = Path(dirpath).relative_to(root)
            # Skip test directories for source discovery
            if any(part.startswith("test") for part in rel_dir.parts):
                continue

            for fname in sorted(filenames):
                if not fname.endswith(ext):
                    continue
                # Skip test files
                if fname.startswith("test_") or fname.endswith(f"_test{ext}"):
                    continue
                # Skip TypeScript/JavaScript test and declaration files
                if language in ("typescript", "javascript"):
                    if fname.endswith(f".test{ext}") or fname.endswith(f".spec{ext}"):
                        continue
                    if fname.endswith(".d.ts"):
                        continue
                fpath = Path(dirpath) / fname
                rel_path = str(fpath.relative_to(root))
                source_files.append(SourceFile(path=rel_path))

    return source_files


def discover_tests(root: str | Path, language: str = "python") -> list[TestFile]:
    """Find test files in the codebase."""
    root = Path(root).resolve()
    ext_map = {"python": ".py", "typescript": ".ts", "javascript": ".js", "rust": ".rs"}
    ext = ext_map.get(language, ".py")
    test_files: list[TestFile] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        if language == "rust":
            dirnames[:] = [d for d in dirnames if d != "target"]

        for fname in sorted(filenames):
            if not fname.endswith(ext):
                continue

            fpath = Path(dirpath) / fname
            rel_parts = Path(dirpath).relative_to(root).parts

            if language == "rust":
                # Rust tests: files in tests/ dir, or any .rs file containing #[test]
                in_tests_dir = "tests" in rel_parts
                has_test_attr = False
                if not in_tests_dir:
                    try:
                        source = fpath.read_text(encoding="utf-8", errors="replace")
                        has_test_attr = "#[test]" in source or "#[cfg(test)]" in source
                    except (OSError, UnicodeDecodeError):
                        pass
                is_test = in_tests_dir or has_test_attr
            elif language in ("typescript", "javascript"):
                is_test = _is_ts_test_file(fname, rel_parts, ext)
            else:
                is_test = (
                    fname.startswith("test_")
                    or fname.endswith(f"_test{ext}")
                    or "tests" in rel_parts
                )
            if not is_test:
                continue

            rel_path = str(fpath.relative_to(root))
            tf = TestFile(path=rel_path)

            # Parse test file for function names and imports
            if language == "python":
                try:
                    source = fpath.read_text(encoding="utf-8", errors="replace")
                    tree = ast.parse(source, filename=str(fpath))
                    tf.test_functions = _extract_test_function_names(tree)
                    tf.imported_modules = _extract_imports(tree)
                    tf.referenced_names = _extract_referenced_names(tree)
                except (SyntaxError, UnicodeDecodeError):
                    logger.debug("Failed to parse test file: %s", rel_path)
            elif language == "rust":
                try:
                    source = fpath.read_text(encoding="utf-8", errors="replace")
                    tf.test_functions = _extract_rust_test_function_names(source)
                    tf.referenced_names = _extract_rust_referenced_names(source)
                except (OSError, UnicodeDecodeError):
                    logger.debug("Failed to parse Rust test file: %s", rel_path)
            elif language in ("typescript", "javascript"):
                try:
                    source = fpath.read_text(encoding="utf-8", errors="replace")
                    tf.test_functions = _extract_ts_test_function_names(source)
                    tf.imported_modules = _extract_ts_imports(source)
                    tf.referenced_names = _extract_ts_referenced_names(source)
                except (OSError, UnicodeDecodeError):
                    logger.debug("Failed to parse TS/JS test file: %s", rel_path)

            test_files.append(tf)

    return test_files


def _extract_test_function_names(tree: ast.Module) -> list[str]:
    """Extract test function names from AST (test_ prefix or inside Test classes)."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.append(node.name)
    return names


def _extract_imports(tree: ast.Module) -> list[str]:
    """Extract imported module names from AST."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def _extract_referenced_names(tree: ast.Module) -> list[str]:
    """Extract all Name references in the AST (for coverage matching)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return sorted(names)


# ── Rust Helpers (regex-based, no external parser) ─────────────────


# Regex for Rust function definitions: `fn name(` or `pub fn name(`
_RUST_FN_RE = re.compile(
    r"^[ \t]*(?:pub(?:\(crate\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*[<(]",
    re.MULTILINE,
)
# Regex for Rust struct/enum/trait/impl definitions
_RUST_STRUCT_RE = re.compile(r"^[ \t]*(?:pub(?:\(crate\))?\s+)?struct\s+(\w+)", re.MULTILINE)
_RUST_ENUM_RE = re.compile(r"^[ \t]*(?:pub(?:\(crate\))?\s+)?enum\s+(\w+)", re.MULTILINE)
_RUST_TRAIT_RE = re.compile(r"^[ \t]*(?:pub(?:\(crate\))?\s+)?trait\s+(\w+)", re.MULTILINE)
_RUST_IMPL_RE = re.compile(r"^[ \t]*impl(?:<[^>]*>)?\s+(\w+)", re.MULTILINE)

# Regex to find #[test] annotated functions
_RUST_TEST_FN_RE = re.compile(
    r"#\[test\]\s*(?:#\[.*?\]\s*)*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)",
    re.DOTALL,
)

# Regex for Rust use statements (for reference matching)
_RUST_USE_RE = re.compile(r"^[ \t]*use\s+([\w:]+)", re.MULTILINE)


# ── TypeScript/JavaScript Helpers (regex-based) ───────────────────


# Function declarations: export function foo(...), async function* bar(...)
_TS_FN_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s*(\*?)\s*(\w+)\s*(?:<[^>]*>)?\s*\(",
    re.MULTILINE,
)

# Class declarations: export class Foo extends Bar
_TS_CLASS_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\S+?))?(?:\s*[<{(]|\s+implements)",
    re.MULTILINE,
)

# Interface declarations
_TS_INTERFACE_RE = re.compile(
    r"^[ \t]*(?:export\s+)?interface\s+(\w+)",
    re.MULTILINE,
)

# Type alias declarations
_TS_TYPE_ALIAS_RE = re.compile(
    r"^[ \t]*(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*=",
    re.MULTILINE,
)

# Const declarations (classified further by _classify_ts_const)
_TS_CONST_RE = re.compile(
    r"^[ \t]*(?:export\s+)?const\s+(\w+)\b",
    re.MULTILINE,
)

# Test function patterns (it/test blocks in describe)
_TS_TEST_FN_RE = re.compile(
    r"""(?:it|test)\s*\(\s*['"`](.+?)['"`]""",
)

# Import-from patterns.
#
# Matches both `import ... from "spec"` and re-exporting `export ... from "spec"`,
# and spans the multi-line clause style that formatters emit:
#
#     import {
#       alpha,
#       beta,
#     } from "./mod.ts"
#
# The clause between the keyword and `from` is restricted to characters that can
# legally appear in an import clause, plus block and line comments, which both
# formatters and hand-written code put inside a clause:
#
#     import {
#       alpha,
#       // beta is deprecated
#       gamma,
#     } from "./mod.ts"
#
# Each comment is matched in an atomic group so the engine cannot backtrack into
# its interior. Without that, a commented-out `// from "./decoy.ts"` could be
# partly consumed and its specifier captured instead of the real one.
#
# A lookahead stops the lazy match at the next statement so a bodiless
# `export { x }` cannot swallow the import after it. The line anchor keeps a
# fully commented-out import statement out entirely.
_TS_IMPORT_FROM_RE = re.compile(
    r"""^[ \t]*(?:import|export)\s"""
    r"""(?:(?!^[ \t]*(?:import|export)\b)"""
    r"""(?:(?>/\*[\s\S]*?\*/)|(?>//[^\n]*)|[\w\s{},*$]))*?"""
    r"""\bfrom\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Dynamic import: `import("spec")`, with or without `await`.
#
# The lookbehind rejects anything where `import` is only the tail of a longer
# identifier (`notimport(...)`) or a member access (`loader.import(...)`).
#
# Only quoted string literals are captured. A computed specifier such as
# `import(`./${name}.ts`)` is not statically resolvable, so it is skipped rather
# than recorded as a junk module path. After the closing quote, only a closing
# parenthesis or an options comma is valid for a statically resolvable argument.
_TS_DYNAMIC_IMPORT_RE = re.compile(
    r"""(?<![\w$.])import\s*\(\s*['"]([^'"]+)['"]\s*(?=[,)])""",
)

# Side-effect import: `import "spec"` — no clause, no `from`, evaluated purely
# for what loading the module does (installing a polyfill, registering a plugin,
# running a decorator shim).
#
# Requiring the quote to follow `import` directly is what keeps this pattern off
# every other import form: a clause, a default binding, or `type` all put a
# non-quote token there. Line-anchoring keeps the specifier of a clause wrapped
# onto its own line from being read as a bare side-effect import.
_TS_SIDE_EFFECT_IMPORT_RE = re.compile(
    r"""^[ \t]*import\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Import names: import { foo, bar } from '...'
_TS_IMPORT_NAMES_RE = re.compile(
    r"""import\s*\{([^}]+)\}\s*from""",
)


def _extract_rust_test_function_names(source: str) -> list[str]:
    """Extract test function names from Rust source (functions with #[test] attribute)."""
    return _RUST_TEST_FN_RE.findall(source)


def _extract_rust_referenced_names(source: str) -> list[str]:
    """Extract referenced names from Rust source for coverage matching."""
    names: set[str] = set()
    # Collect function calls and identifiers
    for m in _RUST_FN_RE.finditer(source):
        names.add(m.group(1))
    # Collect use paths — take the last segment as the referenced name
    for m in _RUST_USE_RE.finditer(source):
        path = m.group(1)
        last_segment = path.rsplit("::", 1)[-1]
        if last_segment != "*":
            names.add(last_segment)
    return sorted(names)


def _find_ts_assignment(source: str, start: int) -> int:
    """Find the assignment ``=`` in a const declaration, skipping ``=>``, ``==``, ``===``.

    Tracks parenthesis and angle-bracket depth so that ``=>`` inside a
    function-type annotation (e.g. ``const f: (x: string) => Effect<A> = ...``)
    is not mistaken for the assignment operator.
    """
    i = start
    depth_paren = 0
    depth_angle = 0
    end = min(start + 500, len(source))
    while i < end:
        c = source[i]
        if c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren -= 1
        elif c == "<":
            depth_angle += 1
        elif c == ">":
            if depth_angle > 0:
                depth_angle -= 1
            # else: comparison operator, ignore
        elif c == "=" and depth_paren == 0 and depth_angle == 0:
            # Skip =>, ==, ===
            nxt = source[i + 1] if i + 1 < len(source) else ""
            if nxt in (">", "="):
                i += 2
                continue
            return i
        elif c in ("\n",) and depth_paren == 0 and depth_angle == 0:
            # Still on the declaration (multiline type annotation), keep going
            pass
        i += 1
    return -1


def _classify_ts_const(after_eq: str) -> str:
    """Classify what follows ``= `` in a const assignment.

    Returns one of: ``"effect_gen"``, ``"pipe"``, ``"layer"``, ``"schema"``,
    ``"arrow"``, or ``""`` (plain constant — skip).
    """
    s = after_eq.lstrip()
    if re.match(r"Effect\.gen\s*\(", s):
        return "effect_gen"
    if re.match(r"pipe\s*\(", s):
        return "pipe"
    if re.match(r"Layer\.\w+\s*\(", s):
        return "layer"
    if re.match(r"Schema\.\w+", s):
        return "schema"
    if re.match(r"(?:async\s+)?\(", s) or re.match(r"(?:async\s+)?\w+\s*=>", s):
        return "arrow"
    return ""


def _extract_ts_type_annotation(source: str, colon_pos: int, eq_pos: int) -> str:
    """Extract the type annotation between ``:`` and ``=`` in a const declaration."""
    text = source[colon_pos + 1:eq_pos].strip()
    return text


def _extract_ts_params(source: str, paren_start: int) -> list[ExtractedParameter]:
    """Extract parameters from a TypeScript function starting at ``(``.

    Handles ``name: Type``, ``name?: Type``, ``...rest: Type[]``, and defaults.
    """
    if paren_start >= len(source) or source[paren_start] != "(":
        return []

    # Find matching close paren
    depth = 0
    end = paren_start
    for i in range(paren_start, min(paren_start + 1000, len(source))):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        return []

    param_text = source[paren_start + 1:end].strip()
    if not param_text:
        return []

    params: list[ExtractedParameter] = []
    # Split on commas, respecting nested generics
    parts = _split_ts_params(param_text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Handle destructured params: { foo, bar }: Type
        if part.startswith("{") or part.startswith("["):
            params.append(ExtractedParameter(name=part.split(":")[0].strip(), type_annotation="", default=""))
            continue
        # Handle ...rest: Type
        name = part
        type_ann = ""
        default = ""
        if "=" in part:
            name_type, default = part.split("=", 1)
            default = default.strip()
            part = name_type.strip()
        if ":" in part:
            name, type_ann = part.split(":", 1)
            name = name.strip().rstrip("?")
            type_ann = type_ann.strip()
        else:
            name = part.strip().rstrip("?")
        params.append(ExtractedParameter(name=name, type_annotation=type_ann, default=default))

    return params


def _split_ts_params(text: str) -> list[str]:
    """Split TypeScript parameter list on commas, respecting ``<>``, ``()``, ``{}``."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in ("<", "(", "{", "["):
            depth += 1
            current.append(ch)
        elif ch in (">", ")", "}", "]"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _extract_ts_return_type_after_paren(source: str, paren_start: int) -> str:
    """Extract the return type annotation after a function's parameter list.

    Looks for ``: ReturnType`` between the closing ``)`` and the opening ``{`` or ``=>``.
    """
    if paren_start >= len(source) or source[paren_start] != "(":
        return ""

    # Find closing paren
    depth = 0
    close_paren = paren_start
    for i in range(paren_start, min(paren_start + 1000, len(source))):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                close_paren = i
                break
    else:
        return ""

    # Look for : Type before { or =>
    rest = source[close_paren + 1:close_paren + 300]
    # Match `: Type` up to `{` or `=>`
    m = re.match(r"\s*:\s*([^{=]+?)(?:\s*\{|\s*=>)", rest)
    if m:
        return m.group(1).strip()
    # Also match `: Type {` for function declarations
    m = re.match(r"\s*:\s*([^{]+?)(?:\s*\{)", rest)
    if m:
        return m.group(1).strip()
    return ""


# Decision points for TypeScript/JavaScript cyclomatic complexity.
#
# `else` alone is not a branch -- an `else if` is counted by its own `if`, and a
# `default:` clause is the fall-through, not a decision. `do ... while` is
# counted once, by its `while`. `??` is listed before the ternary `?` so the
# nullish operator is not read as two ternaries, and the ternary excludes `?.`
# (optional chaining) and `?:` (optional property/parameter).
_TS_DECISION_RE = re.compile(
    r"""\b(?:if|for|while|case|catch)\b|&&|\|\||\?\?|\?(?![.?:])"""
)

_TS_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _mask_ts_literals(source: str) -> str:
    """Blank out comment and string-literal contents, preserving every offset.

    Returns a string the same length as ``source`` in which the interior of
    every line comment, block comment, single/double-quoted string, and
    template literal is replaced by spaces. Newlines and string delimiters are
    kept so line numbers and bracket offsets still line up.

    Scanning the masked copy means keywords and operators appearing inside
    strings and comments cannot be mistaken for code.
    """
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, stop: int) -> None:
        for k in range(max(start, 0), min(stop, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        char = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if char == "/" and nxt == "/":
            end = source.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif char == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif char in "'\"`":
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == char:
                    break
                j += 1
            blank(i + 1, j)
            i = min(j + 1, n)
        else:
            i += 1

    return "".join(out)


def _mask_ts_comments(source: str) -> str:
    """Blank comments without changing offsets or quoted module specifiers."""
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, stop: int) -> None:
        for k in range(start, min(stop, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        char = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if char in "'\"`":
            quote = char
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
        elif char == "/" and nxt == "/":
            end = source.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif char == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        else:
            i += 1

    return "".join(out)


def _match_bracket(masked: str, open_index: int) -> int:
    """Index of the bracket closing the one at ``open_index``, or -1."""
    open_char = masked[open_index]
    close_char = _TS_BRACKET_PAIRS.get(open_char)
    if close_char is None:
        return -1

    depth = 0
    for j in range(open_index, len(masked)):
        if masked[j] == open_char:
            depth += 1
        elif masked[j] == close_char:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _find_ts_outer_arrow(masked: str, start: int) -> int:
    """Find an arrow token outside nested parameters, defaults, and types."""
    closing_for = {"(": ")", "[": "]", "{": "}", "<": ">"}
    stack: list[str] = []
    end = min(start + 2000, len(masked))
    i = start

    while i < end:
        char = masked[i]
        if char == "=" and i + 1 < end and masked[i + 1] == ">":
            if not stack:
                return i
            i += 2
            continue
        if char in closing_for:
            stack.append(closing_for[char])
        elif stack and char == stack[-1]:
            stack.pop()
        i += 1

    return -1


def _ts_block_body(masked: str, search_from: int) -> str:
    """Body of the next `{ ... }` block, if one starts the next statement.

    Returns "" when a `;` intervenes -- an overload signature or an ambient
    `declare function` has no body, and the next `{` belongs to something else.
    """
    brace = masked.find("{", search_from)
    if brace < 0:
        return ""

    semicolon = masked.find(";", search_from)
    if 0 <= semicolon < brace:
        return ""

    close = _match_bracket(masked, brace)
    return masked[brace : close + 1] if close > 0 else masked[brace:]


def _ts_expression_body(masked: str, start: int) -> str:
    """Expression body of a concise arrow, up to the end of the statement."""
    depth = 0
    for j in range(start, len(masked)):
        char = masked[j]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return masked[start:j]
            depth -= 1
        elif depth == 0 and (char == ";" or char == "\n"):
            return masked[start:j]
    return masked[start:]


def _compute_ts_complexity(body: str) -> int:
    """McCabe cyclomatic complexity for a masked TypeScript/JavaScript body."""
    return 1 + len(_TS_DECISION_RE.findall(body))


def extract_functions_typescript(
    file_path: str | Path, source: str | None = None,
) -> list[ExtractedFunction]:
    """Parse a TypeScript/JavaScript file and extract function/class/type definitions.

    Uses regex-based extraction (no external parser). Recognises Effect-TS
    patterns: ``Effect.gen``, ``pipe``, ``Layer.*``, ``Schema.*``,
    ``Context.Tag``, and ``Data.TaggedError``.
    """
    file_path = Path(file_path)
    if source is None:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("Failed to read: %s", file_path)
            return []

    source_lines = source.splitlines()
    # Offset-preserving copy with comment and string contents blanked, so
    # bracket matching and decision-point counting never see keywords or
    # brackets that live inside a literal.
    masked = _mask_ts_literals(source)
    functions: list[ExtractedFunction] = []
    matched_lines: set[int] = set()  # line numbers already captured

    # ── 1. Function declarations ──────────────────────────────────
    for m in _TS_FN_RE.finditer(source):
        is_generator = bool(m.group(1))
        name = m.group(2)
        line_number = source[:m.start()].count("\n") + 1
        if line_number in matched_lines:
            continue
        line_text = source_lines[line_number - 1] if line_number <= len(source_lines) else ""
        is_exported = "export" in line_text.split("function")[0]
        is_async = "async" in line_text.split("function")[0]

        paren_pos = source.index("(", m.start() + len(m.group(0)) - 1)
        params = _extract_ts_params(source, paren_pos)
        return_type = _extract_ts_return_type_after_paren(source, paren_pos)

        params_close = _match_bracket(masked, paren_pos)
        body = _ts_block_body(masked, params_close + 1) if params_close > 0 else ""

        decorators: list[str] = []
        if is_exported:
            decorators.append("export")
        if is_generator:
            decorators.append("generator")

        functions.append(ExtractedFunction(
            name=name,
            params=params,
            return_type=return_type,
            complexity=_compute_ts_complexity(body),
            body_source="",
            line_number=line_number,
            is_async=is_async,
            is_method=False,
            class_name="",
            decorators=decorators,
            docstring="",
        ))
        matched_lines.add(line_number)

    # ── 2. Const assignments (arrow fns, Effect.gen, pipe, Layer, Schema) ─
    for m in _TS_CONST_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        if line_number in matched_lines:
            continue

        line_text = source_lines[line_number - 1] if line_number <= len(source_lines) else ""
        is_exported = "export " in line_text[:line_text.find("const")]

        # Find the assignment =
        eq_pos = _find_ts_assignment(source, m.end())
        if eq_pos < 0:
            continue

        after_eq = source[eq_pos + 1:eq_pos + 200]
        kind = _classify_ts_const(after_eq)
        if not kind:
            continue  # Plain constant, not a function

        decorators = []
        if is_exported:
            decorators.append("export")

        # Extract type annotation if present (between name and =)
        type_ann_text = source[m.end():eq_pos].strip()
        return_type = ""
        if type_ann_text.startswith(":"):
            return_type = type_ann_text[1:].strip()

        params: list[ExtractedParameter] = []
        is_async = False
        body = ""

        if kind == "arrow":
            stripped = after_eq.lstrip()
            is_async = stripped.startswith("async")
            # Find the opening paren
            paren_idx = stripped.find("(")
            if paren_idx >= 0:
                abs_paren = eq_pos + 1 + (len(after_eq) - len(stripped)) + paren_idx
                params = _extract_ts_params(source, abs_paren)
                if not return_type:
                    return_type = _extract_ts_return_type_after_paren(source, abs_paren)
            else:
                # Single-param arrow without parens: `const f = x => ...`
                single_match = re.match(r"\s*(?:async\s+)?(\w+)\s*=>", after_eq)
                if single_match:
                    params = [ExtractedParameter(name=single_match.group(1))]

            # The body is whatever follows `=>`: either a block or a single
            # expression. Search the mask so a `=>` inside a default-parameter
            # string cannot be mistaken for the arrow.
            arrow = _find_ts_outer_arrow(masked, eq_pos + 1)
            if arrow >= 0:
                after_arrow = arrow + 2
                rest = masked[after_arrow:]
                lead = len(rest) - len(rest.lstrip())
                if rest.lstrip().startswith("{"):
                    body = _ts_block_body(masked, after_arrow + lead)
                else:
                    body = _ts_expression_body(masked, after_arrow)
        elif kind in ("effect_gen", "pipe", "layer", "schema"):
            decorators.append(kind)
            # `Effect.gen(function*() { ... })`, `pipe(a, b)`, `Layer.effect(...)`,
            # `Schema.Struct({ ... })` -- the body is the balanced call span.
            call_open = masked.find("(", eq_pos)
            if call_open >= 0:
                call_close = _match_bracket(masked, call_open)
                if call_close > 0:
                    body = masked[call_open : call_close + 1]

        functions.append(ExtractedFunction(
            name=name,
            params=params,
            return_type=return_type,
            complexity=_compute_ts_complexity(body),
            body_source="",
            line_number=line_number,
            is_async=is_async or kind == "effect_gen",
            is_method=False,
            class_name="",
            decorators=decorators,
            docstring="",
        ))
        matched_lines.add(line_number)

    # ── 3. Class declarations ─────────────────────────────────────
    for m in _TS_CLASS_RE.finditer(source):
        name = m.group(1)
        extends = m.group(2) or ""
        line_number = source[:m.start()].count("\n") + 1
        if line_number in matched_lines:
            continue

        line_text = source_lines[line_number - 1] if line_number <= len(source_lines) else ""
        is_exported = "export " in line_text.split("class")[0]

        decorators = []
        if is_exported:
            decorators.append("export")

        # Classify Effect-TS class patterns
        if "Context.Tag" in extends or "Context.Tag" in line_text:
            decorators.append("service")
            ret_type = "service"
        elif "Data.TaggedError" in extends or "Data.TaggedError" in line_text:
            decorators.append("tagged_error")
            ret_type = "tagged_error"
        elif "Data.Tagged" in extends or "Data.tagged" in extends:
            decorators.append("tagged_data")
            ret_type = "tagged_data"
        else:
            decorators.append("class")
            ret_type = "class"

        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type=ret_type,
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=decorators,
            docstring="",
        ))
        matched_lines.add(line_number)

    # ── 4. Interface declarations ─────────────────────────────────
    for m in _TS_INTERFACE_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        if line_number in matched_lines:
            continue

        line_text = source_lines[line_number - 1] if line_number <= len(source_lines) else ""
        is_exported = "export " in line_text.split("interface")[0]

        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type="interface",
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=(["export", "interface"] if is_exported else ["interface"]),
            docstring="",
        ))
        matched_lines.add(line_number)

    # ── 5. Type alias declarations ────────────────────────────────
    for m in _TS_TYPE_ALIAS_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        if line_number in matched_lines:
            continue

        line_text = source_lines[line_number - 1] if line_number <= len(source_lines) else ""
        is_exported = "export " in line_text.split("type")[0]

        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type="type",
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=(["export", "type"] if is_exported else ["type"]),
            docstring="",
        ))
        matched_lines.add(line_number)

    return functions


def _extract_ts_test_function_names(source: str) -> list[str]:
    """Extract test function names from TypeScript test source (it/test blocks)."""
    names: list[str] = []
    for m in _TS_TEST_FN_RE.finditer(source):
        names.append(m.group(1))
    return names


def _extract_ts_imports(source: str) -> list[str]:
    """Extract imported module paths from TypeScript source.

    Covers static `import ... from "spec"`, dynamic `import("spec")`, and the
    bare side-effect form `import "spec"`. This remains a lexical heuristic,
    not a complete TypeScript parser. Results come back in source order.
    """
    uncommented = _mask_ts_comments(source)
    matches = [
        *_TS_IMPORT_FROM_RE.finditer(uncommented),
        *_TS_DYNAMIC_IMPORT_RE.finditer(uncommented),
        *_TS_SIDE_EFFECT_IMPORT_RE.finditer(uncommented),
    ]
    matches.sort(key=lambda m: m.start())
    return [m.group(1) for m in matches]


def _extract_ts_referenced_names(source: str) -> list[str]:
    """Extract referenced identifiers from TypeScript source for coverage matching."""
    names: set[str] = set()
    # Extract imported names: import { foo, bar } from ...
    for m in _TS_IMPORT_NAMES_RE.finditer(source):
        for name in m.group(1).split(","):
            name = name.strip()
            # Handle `as` aliases: import { foo as bar }
            if " as " in name:
                name = name.split(" as ")[0].strip()
            if name:
                names.add(name)
    # Extract function calls and identifiers (simple heuristic)
    for m in re.finditer(r"\b(\w+)\s*\(", source):
        word = m.group(1)
        if word not in ("if", "for", "while", "switch", "catch", "function",
                        "import", "export", "return", "describe", "it", "test",
                        "expect", "const", "let", "var", "new", "typeof", "async"):
            names.add(word)
    return sorted(names)


def _is_ts_test_file(fname: str, rel_parts: tuple[str, ...], ext: str) -> bool:
    """Check if a TypeScript/JavaScript file is a test file."""
    return (
        fname.endswith(f".test{ext}")
        or fname.endswith(f".spec{ext}")
        or fname.startswith("test_")
        or fname.endswith(f"_test{ext}")
        or "__tests__" in rel_parts
        or "tests" in rel_parts
    )


# ── Function Extraction ────────────────────────────────────────────


def extract_functions(file_path: str | Path, source: str | None = None) -> list[ExtractedFunction]:
    """Parse a Python file and extract all function definitions."""
    file_path = Path(file_path)
    if source is None:
        source = file_path.read_text(encoding="utf-8", errors="replace")

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        logger.debug("Failed to parse: %s", file_path)
        return []

    source_lines = source.splitlines()
    functions: list[ExtractedFunction] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = _extract_single_function(node, source_lines)
            functions.append(func)

    return functions


def extract_functions_rust(file_path: str | Path, source: str | None = None) -> list[ExtractedFunction]:
    """Parse a Rust file and extract function/struct/enum/trait/impl definitions.

    Prefers tree-sitter when available (accurate scope tracking via impl blocks
    and generics-aware parsing) and falls back to regex when tree-sitter is
    unavailable or fails on a particular file. Public signature is unchanged.
    """
    file_path = Path(file_path)
    if source is None:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("Failed to read: %s", file_path)
            return []

    # Prefer tree-sitter if available — handles impl-block scope and generics
    # correctly, where the regex path mishandles them.
    ts_result = _extract_functions_rust_via_tree_sitter(source)
    if ts_result is not None:
        return ts_result

    return _extract_functions_rust_via_regex(source)


def _extract_functions_rust_via_tree_sitter(source: str) -> list[ExtractedFunction] | None:
    """Extract Rust definitions using tree-sitter.

    Returns ``None`` if tree-sitter (or the rust grammar) is unavailable, or
    if parsing fails — caller should fall back to regex. Returns a list (which
    may be empty) when extraction succeeds.
    """
    try:
        from tree_sitter import Parser
        import tree_sitter_rust
        from tree_sitter import Language
    except ImportError:
        return None
    except Exception:
        logger.debug("tree-sitter import failed", exc_info=True)
        return None

    try:
        ts_lang = Language(tree_sitter_rust.language())
        parser = Parser(ts_lang)
        tree = parser.parse(source.encode("utf-8", errors="replace"))
    except Exception:
        logger.debug("tree-sitter parse failed for rust source", exc_info=True)
        return None

    source_bytes = source.encode("utf-8", errors="replace")
    functions: list[ExtractedFunction] = []

    def _node_text(node) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _named_child(node, type_name: str):
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def _parse_rust_params(params_node) -> tuple[list[ExtractedParameter], bool]:
        """Return (params, is_method) by walking a parameters node."""
        params: list[ExtractedParameter] = []
        is_method = False
        if params_node is None:
            return params, is_method
        for child in params_node.children:
            if child.type == "self_parameter":
                is_method = True
                params.append(ExtractedParameter(
                    name=_node_text(child).strip(),
                    type_annotation="",
                    default="",
                ))
            elif child.type == "parameter":
                # parameter has a pattern (the name) and a type
                name = ""
                type_ann = ""
                pat = _named_child(child, "identifier")
                if pat is None:
                    # Could be wrapped in mutable_specifier or pattern
                    for sub in child.children:
                        if sub.type == "identifier":
                            pat = sub
                            break
                if pat is not None:
                    name = _node_text(pat)
                # Type comes after the colon
                seen_colon = False
                for sub in child.children:
                    if seen_colon:
                        type_ann = _node_text(sub).strip()
                        break
                    if sub.type == ":":
                        seen_colon = True
                params.append(ExtractedParameter(
                    name=name,
                    type_annotation=type_ann,
                    default="",
                ))
        return params, is_method

    def _walk(node, parent_impl: str = "", parent_trait: str = "") -> None:
        nt = node.type

        if nt in ("function_item", "function_signature_item"):
            name_node = _named_child(node, "identifier")
            if name_node is None:
                return
            name = _node_text(name_node)
            line_number = node.start_point[0] + 1

            # Detect modifiers: pub, async
            is_pub = False
            is_async = False
            for child in node.children:
                if child.type == "visibility_modifier":
                    txt = _node_text(child)
                    if "pub" in txt:
                        is_pub = True
                elif child.type == "function_modifiers":
                    if "async" in _node_text(child):
                        is_async = True
                elif child.type == "async":
                    is_async = True

            params_node = _named_child(node, "parameters")
            params, is_method = _parse_rust_params(params_node)

            # Return type — child after parameters, after a `->`, before body/`;`
            return_type = ""
            saw_arrow = False
            for child in node.children:
                if saw_arrow:
                    if child.type in ("block", ";"):
                        break
                    txt = _node_text(child).strip()
                    if txt:
                        return_type = txt
                        break
                if child.type == "->":
                    saw_arrow = True

            decorators: list[str] = []
            if is_pub:
                decorators.append("pub")

            class_name = parent_impl or parent_trait

            functions.append(ExtractedFunction(
                name=name,
                params=params,
                return_type=return_type,
                complexity=1,
                body_source="",
                line_number=line_number,
                is_async=is_async,
                is_method=is_method,
                class_name=class_name,
                decorators=decorators,
                docstring="",
            ))
            # Don't descend into function body; nested functions are uncommon and
            # excluded from regex extractor too.
            return

        if nt == "struct_item":
            name_node = _named_child(node, "type_identifier")
            if name_node is not None:
                functions.append(ExtractedFunction(
                    name=_node_text(name_node),
                    params=[],
                    return_type="struct",
                    complexity=1,
                    body_source="",
                    line_number=node.start_point[0] + 1,
                    is_async=False,
                    is_method=False,
                    class_name="",
                    decorators=["struct"],
                    docstring="",
                ))
            return

        if nt == "enum_item":
            name_node = _named_child(node, "type_identifier")
            if name_node is not None:
                functions.append(ExtractedFunction(
                    name=_node_text(name_node),
                    params=[],
                    return_type="enum",
                    complexity=1,
                    body_source="",
                    line_number=node.start_point[0] + 1,
                    is_async=False,
                    is_method=False,
                    class_name="",
                    decorators=["enum"],
                    docstring="",
                ))
            return

        if nt == "trait_item":
            name_node = _named_child(node, "type_identifier")
            trait_name = _node_text(name_node) if name_node else ""
            if trait_name:
                functions.append(ExtractedFunction(
                    name=trait_name,
                    params=[],
                    return_type="trait",
                    complexity=1,
                    body_source="",
                    line_number=node.start_point[0] + 1,
                    is_async=False,
                    is_method=False,
                    class_name="",
                    decorators=["trait"],
                    docstring="",
                ))
            # Descend so trait method declarations get attributed correctly
            for child in node.children:
                _walk(child, parent_impl=parent_impl, parent_trait=trait_name)
            return

        if nt == "impl_item":
            # Find the type being implemented. tree-sitter-rust marks it as
            # ``type_identifier`` (or a generic_type wrapping one). We pick the
            # first type_identifier we encounter at the impl level.
            impl_name = ""
            for child in node.children:
                if child.type == "type_identifier":
                    impl_name = _node_text(child)
                    break
                if child.type == "generic_type":
                    inner = _named_child(child, "type_identifier")
                    if inner is not None:
                        impl_name = _node_text(inner)
                        break
            if impl_name:
                functions.append(ExtractedFunction(
                    name=impl_name,
                    params=[],
                    return_type="impl",
                    complexity=1,
                    body_source="",
                    line_number=node.start_point[0] + 1,
                    is_async=False,
                    is_method=False,
                    class_name="",
                    decorators=["impl"],
                    docstring="",
                ))
            for child in node.children:
                _walk(child, parent_impl=impl_name, parent_trait=parent_trait)
            return

        # Default: descend into children, preserving scope
        for child in node.children:
            _walk(child, parent_impl=parent_impl, parent_trait=parent_trait)

    try:
        _walk(tree.root_node)
    except Exception:
        logger.debug("tree-sitter walk failed", exc_info=True)
        return None

    return functions


def _extract_functions_rust_via_regex(source: str) -> list[ExtractedFunction]:
    """Regex-based fallback for Rust function/type extraction."""
    source_lines = source.splitlines()
    functions: list[ExtractedFunction] = []

    # Extract fn definitions
    for m in _RUST_FN_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        # Determine visibility
        line_text = source_lines[line_number - 1] if line_number <= len(source_lines) else ""
        is_pub = line_text.lstrip().startswith("pub")
        is_async = "async" in line_text.split("fn")[0] if "fn" in line_text else False

        # Try to extract simple parameter list
        params = _extract_rust_params(source, m.end() - 1)

        # Try to extract return type
        return_type = _extract_rust_return_type(source, m.end() - 1)

        functions.append(ExtractedFunction(
            name=name,
            params=params,
            return_type=return_type,
            complexity=1,  # No AST-based complexity for Rust
            body_source="",
            line_number=line_number,
            is_async=is_async,
            is_method="self" in line_text.split(")")[0] if ")" in line_text else False,
            class_name="",
            decorators=["pub"] if is_pub else [],
            docstring="",
        ))

    # Extract struct definitions
    for m in _RUST_STRUCT_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type="struct",
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=["struct"],
            docstring="",
        ))

    # Extract enum definitions
    for m in _RUST_ENUM_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type="enum",
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=["enum"],
            docstring="",
        ))

    # Extract trait definitions
    for m in _RUST_TRAIT_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type="trait",
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=["trait"],
            docstring="",
        ))

    # Extract impl blocks
    for m in _RUST_IMPL_RE.finditer(source):
        name = m.group(1)
        line_number = source[:m.start()].count("\n") + 1
        functions.append(ExtractedFunction(
            name=name,
            params=[],
            return_type="impl",
            complexity=1,
            body_source="",
            line_number=line_number,
            is_async=False,
            is_method=False,
            class_name="",
            decorators=["impl"],
            docstring="",
        ))

    return functions


def _extract_rust_params(source: str, paren_start: int) -> list[ExtractedParameter]:
    """Extract parameters from a Rust function starting at the opening paren."""
    # Find matching closing paren
    if paren_start >= len(source) or source[paren_start] != "(":
        return []

    depth = 0
    end = paren_start
    for i in range(paren_start, min(paren_start + 500, len(source))):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        return []

    param_text = source[paren_start + 1:end].strip()
    if not param_text:
        return []

    params: list[ExtractedParameter] = []
    # Split on commas (naive — doesn't handle commas inside generics)
    # but good enough for signature extraction
    for part in param_text.split(","):
        part = part.strip()
        if not part:
            continue
        # Handle &self, &mut self, self
        if part in ("self", "&self", "&mut self"):
            params.append(ExtractedParameter(name=part, type_annotation="", default=""))
            continue
        # name: Type pattern
        if ":" in part:
            name, type_ann = part.split(":", 1)
            params.append(ExtractedParameter(
                name=name.strip(),
                type_annotation=type_ann.strip(),
                default="",
            ))
        else:
            params.append(ExtractedParameter(name=part.strip(), type_annotation="", default=""))

    return params


def _extract_rust_return_type(source: str, paren_start: int) -> str:
    """Extract return type from a Rust function (the -> Type before the opening brace)."""
    # Find closing paren first
    if paren_start >= len(source) or source[paren_start] != "(":
        return ""

    depth = 0
    close_paren = paren_start
    for i in range(paren_start, min(paren_start + 500, len(source))):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                close_paren = i
                break
    else:
        return ""

    # Look for -> between close_paren and opening brace or newline
    rest = source[close_paren + 1:close_paren + 200]
    arrow_match = re.search(r"\s*->\s*([^{]+)", rest)
    if arrow_match:
        return arrow_match.group(1).strip().rstrip("{").strip()
    return ""


def _extract_single_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> ExtractedFunction:
    """Extract a single function from an AST node."""
    # Determine if this is a method (nested inside ClassDef)
    class_name = ""
    is_method = False
    # We'll check parent via the _parent attribute if set, but ast.walk doesn't set it.
    # Instead, we detect methods by checking if 'self' or 'cls' is first param.
    params = _extract_params(node.args)
    if params and params[0].name in ("self", "cls"):
        is_method = True

    # Decorators
    decorators = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            decorators.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            decorators.append(ast.dump(dec))
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                decorators.append(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                decorators.append(dec.func.attr)

    # Return type
    return_type = ""
    if node.returns:
        return_type = _annotation_to_str(node.returns)

    # Docstring
    docstring = ast.get_docstring(node) or ""

    # Body source
    body_source = ""
    try:
        start = node.lineno - 1
        end = node.end_lineno or start + 1
        body_source = "\n".join(source_lines[start:end])
    except (IndexError, AttributeError):
        pass

    # Complexity
    complexity = compute_complexity(node)

    return ExtractedFunction(
        name=node.name,
        params=params,
        return_type=return_type,
        complexity=complexity,
        body_source=body_source,
        line_number=node.lineno,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_method=is_method,
        class_name=class_name,
        decorators=decorators,
        docstring=docstring,
    )


def _extract_params(args: ast.arguments) -> list[ExtractedParameter]:
    """Extract parameters from function arguments."""
    params: list[ExtractedParameter] = []

    # Defaults are right-aligned with args
    num_args = len(args.args)
    num_defaults = len(args.defaults)
    default_offset = num_args - num_defaults

    for i, arg in enumerate(args.args):
        annotation = _annotation_to_str(arg.annotation) if arg.annotation else ""
        default = ""
        if i >= default_offset:
            default_node = args.defaults[i - default_offset]
            default = _node_to_str(default_node)
        params.append(ExtractedParameter(
            name=arg.arg,
            type_annotation=annotation,
            default=default,
        ))

    # *args
    if args.vararg:
        annotation = _annotation_to_str(args.vararg.annotation) if args.vararg.annotation else ""
        params.append(ExtractedParameter(name=f"*{args.vararg.arg}", type_annotation=annotation))

    # **kwargs
    if args.kwarg:
        annotation = _annotation_to_str(args.kwarg.annotation) if args.kwarg.annotation else ""
        params.append(ExtractedParameter(name=f"**{args.kwarg.arg}", type_annotation=annotation))

    return params


def _annotation_to_str(node: ast.expr | None) -> str:
    """Convert an annotation AST node to a string."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return ""


def _node_to_str(node: ast.expr) -> str:
    """Convert an AST expression node to a string."""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        return "..."


# ── Cyclomatic Complexity ──────────────────────────────────────────


class _ComplexityVisitor(ast.NodeVisitor):
    """Count decision points for cyclomatic complexity."""

    def __init__(self) -> None:
        self.complexity = 1  # Base complexity

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Each additional boolean operand adds a decision point
        self.complexity += len(node.values) - 1
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        # Each case is a decision point (minus the first which is the base)
        if hasattr(node, "cases"):
            self.complexity += len(node.cases) - 1
        self.generic_visit(node)


def compute_complexity(node: ast.AST) -> int:
    """Compute McCabe cyclomatic complexity for a function AST node."""
    visitor = _ComplexityVisitor()
    visitor.visit(node)
    return visitor.complexity


# ── Coverage Mapping ───────────────────────────────────────────────

# Extensions stripped when turning a source path into a dotted module name.
_MODULE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".rs")

# Candidate suffixes appended to an extensionless relative specifier, in the
# order a TypeScript/JavaScript resolver would try them.
_SPECIFIER_SUFFIXES = (
    "", ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts",
    "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/mod.ts",
)


def _module_name(path: str) -> str:
    """Turn a project-relative source path into a dotted module name."""
    module = path.replace("/", ".").replace("\\", ".")
    for ext in _MODULE_EXTENSIONS:
        if module.endswith(ext):
            return module[: -len(ext)]
    return module


def _posix_path(path: str) -> str:
    """Rewrite a project-relative path with forward slashes.

    ``discover_source_files`` builds paths with ``Path.relative_to``, which uses
    the host separator, so on Windows a path arrives here with backslashes. An
    import specifier is POSIX in every source file on every host, so the two
    have to be reconciled before they can be joined.
    """
    return path.replace("\\", "/")


def _resolve_relative_specifier(
    importer_path: str,
    specifier: str,
    known_paths_by_posix: dict[str, str],
) -> str | None:
    """Resolve a relative import specifier to a known project-relative path.

    TypeScript and JavaScript import each other by path, not by dotted module
    name -- ``import { foo } from "../foo.ts"``. Resolving that against the
    importing file's directory is the only way to know which module it names.

    ``known_paths_by_posix`` maps each source path, rewritten POSIX-style, to
    the path as the project recorded it. Building it once at the call site keeps
    the per-import cost constant, and returning the recorded spelling is what
    lets ``_module_name`` match — a file keyed under one spelling cannot be
    found by another.

    Returns None when the specifier names nothing in the project (a missing
    file, or a path that climbs out of the project root).
    """
    if not specifier.startswith("."):
        return None

    base = posixpath.dirname(_posix_path(importer_path))
    joined = posixpath.normpath(posixpath.join(base, specifier))

    # normpath leaves a leading ".." segment when the specifier climbs past the
    # root. Matching the segment, not the prefix, keeps a file whose name simply
    # begins with dots (``..rc.ts``) from being read as an escape.
    if joined == ".." or joined.startswith("../") or posixpath.isabs(joined):
        return None

    for suffix in _SPECIFIER_SUFFIXES:
        hit = known_paths_by_posix.get(joined + suffix)
        if hit is not None:
            return hit

    return None


def map_test_coverage(
    source_files: list[SourceFile],
    test_files: list[TestFile],
) -> CoverageMap:
    """Map test coverage using dual heuristic: import analysis + name matching.

    Heuristic 1: Import analysis — test file imports source module -> coverage link
    Heuristic 2: Name matching — strip test_ prefix from test names -> match source functions
    """
    # Build lookup: module_name -> list of function names
    # e.g. "src/auth.py" -> module "src.auth" -> functions ["login", "logout"]
    module_to_functions: dict[str, set[str]] = {}
    func_to_file: dict[str, str] = {}
    func_to_complexity: dict[str, int] = {}

    known_paths_by_posix = {_posix_path(sf.path): sf.path for sf in source_files}

    for sf in source_files:
        module = _module_name(sf.path)
        func_names = {f.name for f in sf.functions}
        module_to_functions[module] = func_names
        for f in sf.functions:
            key = f"{sf.path}::{f.name}"
            func_to_file[key] = sf.path
            func_to_complexity[key] = f.complexity

    # Build set of covered function names from test files
    covered_functions: set[str] = set()  # "path::func_name"

    for tf in test_files:
        # Heuristic 1: Which source modules does this test import?
        imported_source_modules: set[str] = set()
        for imp in tf.imported_modules:
            # TypeScript/JavaScript import by path, not by dotted module name.
            # Resolve those against the test file's directory; a relative
            # specifier that resolves to nothing names nothing in the project,
            # so it must not fall through to the dotted-name heuristic below.
            if imp.startswith("."):
                resolved = _resolve_relative_specifier(
                    tf.path, imp, known_paths_by_posix
                )
                if resolved is not None:
                    imported_source_modules.add(_module_name(resolved))
                continue

            for module_name in module_to_functions:
                # Check if import matches or is a sub-import
                if imp == module_name or module_name.endswith(f".{imp}") or imp.endswith(f".{module_name.split('.')[-1]}"):
                    imported_source_modules.add(module_name)

        # Heuristic 2: Name matching — strip test_ prefix
        tested_names: set[str] = set()
        for test_name in tf.test_functions:
            if test_name.startswith("test_"):
                bare_name = test_name[5:]  # strip "test_"
                tested_names.add(bare_name)

        # Also check referenced names from the AST
        referenced = set(tf.referenced_names)

        # Mark functions as covered
        for sf in source_files:
            module = _module_name(sf.path)

            for func in sf.functions:
                key = f"{sf.path}::{func.name}"
                # Covered if: test imports the module AND (test name matches OR function name is referenced)
                if module in imported_source_modules:
                    if func.name in tested_names or func.name in referenced:
                        covered_functions.add(key)
                # Also covered if test name directly matches (even without import analysis)
                if func.name in tested_names:
                    covered_functions.add(key)

    # Build coverage entries
    entries: list[CoverageEntry] = []
    for sf in source_files:
        for func in sf.functions:
            key = f"{sf.path}::{func.name}"
            is_covered = key in covered_functions
            test_count = 0
            if is_covered:
                # Count how many test functions reference this name
                for tf in test_files:
                    for tn in tf.test_functions:
                        if tn.startswith("test_") and tn[5:] == func.name:
                            test_count += 1
                        elif func.name in tf.referenced_names:
                            test_count += 1
                            break
                test_count = max(test_count, 1)

            entries.append(CoverageEntry(
                function_name=func.name,
                file_path=sf.path,
                complexity=func.complexity,
                test_count=test_count,
                covered=is_covered,
            ))

    return CoverageMap(entries=entries)


# ── Security Pattern Detection ─────────────────────────────────────


def detect_security_patterns(func: ExtractedFunction, source: str = "") -> list[SecurityFinding]:
    """Detect security-sensitive patterns in a function via AST conditional analysis.

    Looks for conditionals that reference security-relevant names
    (auth, role, permission, token, etc.).
    """
    findings: list[SecurityFinding] = []

    # Parse the function body
    body = source or func.body_source
    if not body:
        return findings

    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return findings

    # Walk AST looking for conditionals with security-relevant names
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp)):
            continue

        test_node = node.test if isinstance(node, ast.If) else node.test
        names_in_condition = _collect_names(test_node)
        calls_in_condition = _collect_calls(test_node)

        # Check for security-sensitive variable names
        for name in names_in_condition:
            if name.lower() in SECURITY_VARIABLES:
                findings.append(SecurityFinding(
                    function_name=func.name,
                    file_path="",  # Set by caller
                    line_number=func.line_number,
                    complexity=func.complexity,
                    pattern_matched=f"variable: {name}",
                    suggestion=f"Ensure branch on '{name}' is tested with both truthy and falsy values",
                ))
                break  # One finding per conditional

        # Check for security-sensitive function calls
        for call_name in calls_in_condition:
            lower_call = call_name.lower()
            if any(lower_call.startswith(prefix) for prefix in SECURITY_CALL_PREFIXES):
                findings.append(SecurityFinding(
                    function_name=func.name,
                    file_path="",  # Set by caller
                    line_number=func.line_number,
                    complexity=func.complexity,
                    pattern_matched=f"call: {call_name}",
                    suggestion=f"Ensure call to '{call_name}' in conditional is tested",
                ))
                break

    return findings


def _collect_names(node: ast.AST) -> list[str]:
    """Collect all Name and Attribute nodes from an expression."""
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.append(child.id)
        elif isinstance(child, ast.Attribute):
            names.append(child.attr)
    return names


def _collect_calls(node: ast.AST) -> list[str]:
    """Collect all function call names from an expression."""
    calls: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                calls.append(child.func.attr)
    return calls


def _assign_risk_levels(
    findings: list[SecurityFinding],
    coverage_map: CoverageMap,
) -> None:
    """Assign risk levels to security findings based on coverage and complexity."""
    covered_funcs = {
        (e.file_path, e.function_name)
        for e in coverage_map.entries if e.covered
    }
    complexity_map = {
        (e.file_path, e.function_name): e.complexity
        for e in coverage_map.entries
    }

    for finding in findings:
        key = (finding.file_path, finding.function_name)
        is_covered = key in covered_funcs
        complexity = complexity_map.get(key, finding.complexity)
        finding.covered = is_covered

        if not is_covered and complexity >= 5:
            finding.risk_level = SecurityRiskLevel.critical
        elif not is_covered:
            finding.risk_level = SecurityRiskLevel.high
        elif is_covered:
            # Has coverage but security branches may not be specifically tested
            finding.risk_level = SecurityRiskLevel.low
        else:
            finding.risk_level = SecurityRiskLevel.medium


# ── Main Orchestrator ──────────────────────────────────────────────


def analyze_codebase(
    root: str | Path,
    language: str = "python",
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> CodebaseAnalysis:
    """Run full mechanical analysis on a codebase.

    Steps:
    1. Discover source files
    2. Extract functions from each source file
    3. Discover test files
    4. Map test coverage
    5. Detect security patterns
    6. Assign risk levels

    Args:
        root: Project root directory.
        language: Programming language.
        include: Passed to :func:`discover_source_files` — directories,
            file-lists, or ``-`` for stdin.
        exclude: Additional directory names to skip.

    Returns a CodebaseAnalysis with everything populated.
    """
    root = Path(root).resolve()

    # Step 1: Discover source files
    source_files = discover_source_files(root, language, include=include, exclude=exclude)

    # Step 2: Extract functions
    for sf in source_files:
        full_path = root / sf.path
        if not full_path.exists():
            continue

        if language == "python":
            source = full_path.read_text(encoding="utf-8", errors="replace")
            sf.functions = extract_functions(full_path, source)

            # Extract imports and classes
            try:
                tree = ast.parse(source, filename=str(full_path))
                sf.imports = _extract_imports(tree)
                sf.classes = [
                    node.name for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef)
                ]
            except SyntaxError:
                pass

        elif language == "rust":
            source = full_path.read_text(encoding="utf-8", errors="replace")
            sf.functions = extract_functions_rust(full_path, source)

            # Extract struct/enum/trait names as "classes" for coverage mapping
            sf.classes = [
                f.name for f in sf.functions
                if f.decorators and f.decorators[0] in ("struct", "enum", "trait")
            ]
            # Extract use statements as imports
            sf.imports = [m.group(1) for m in _RUST_USE_RE.finditer(source)]

        elif language in ("typescript", "javascript"):
            source = full_path.read_text(encoding="utf-8", errors="replace")
            sf.functions = extract_functions_typescript(full_path, source)

            # Extract class/interface/type names for coverage mapping
            sf.classes = [
                f.name for f in sf.functions
                if f.decorators and any(
                    d in ("class", "interface", "type", "service",
                           "tagged_error", "tagged_data", "schema")
                    for d in f.decorators
                )
            ]
            # Extract imports
            sf.imports = _extract_ts_imports(source)

    # Step 3: Discover tests
    test_files = discover_tests(root, language)

    # Step 4: Map coverage
    coverage = map_test_coverage(source_files, test_files)

    # Step 5: Detect security patterns
    all_findings: list[SecurityFinding] = []
    for sf in source_files:
        for func in sf.functions:
            func_findings = detect_security_patterns(func)
            for finding in func_findings:
                finding.file_path = sf.path
            all_findings.extend(func_findings)

    # Step 6: Assign risk levels
    _assign_risk_levels(all_findings, coverage)

    security = SecurityAuditReport(findings=all_findings)

    # Step 7: Build tool index (optional enrichment from ctags/cscope/tree-sitter/kindex)
    tool_idx = None
    try:
        from pact.tool_index import build_tool_index
        all_function_names = [f.name for sf in source_files for f in sf.functions]
        tool_idx = build_tool_index(root, language, function_names=all_function_names)
    except Exception:
        logger.debug("Tool index build failed, continuing without it", exc_info=True)

    return CodebaseAnalysis(
        root_path=str(root),
        language=language,
        source_files=source_files,
        test_files=test_files,
        coverage=coverage,
        security=security,
        tool_index=tool_idx,
    )
