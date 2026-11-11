"""Obsidian-style link resolution and rewriting for `move` (SPEC §6.4, plan D12).

Handled: `[[x]]`, `[[x|alias]]`, `[[x#heading]]`, `![[x]]` (wikilinks resolve by path suffix,
so a bare name matches any folder; ties go to the linking page's folder, then the shortest
path), and relative Markdown links `[t](x.md#h)`. Fenced code and inline code are left alone.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable
from urllib.parse import quote, unquote

_WIKILINK = re.compile(
    r"(?P<bang>!?)\[\[(?P<target>[^\]\|#\n]+?)(?P<heading>#[^\]\|\n]*)?(?P<alias>\|[^\]\n]*)?\]\]"
)
_MDLINK = re.compile(r"(?P<label>!?\[[^\]\n]*\])\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"\n]*\")?\)")
_INLINE_CODE = re.compile(r"(`+).*?\1")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


class VaultFiles:
    """The set of vault-relative file paths used to resolve links."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = frozenset(paths)

    def with_move(self, old: str, new: str) -> VaultFiles:
        return VaultFiles((self.paths - {old}) | {new})

    def resolve_wikilink(self, target: str, source: str) -> str | None:
        name = target.strip()
        for wanted in (name + ".md", name) if not name.endswith(".md") else (name,):
            matches = [p for p in self.paths if p == wanted or p.endswith("/" + wanted)]
            if matches:
                folder = posixpath.dirname(source)
                same = [p for p in matches if posixpath.dirname(p) == folder]
                return (same or sorted(matches, key=lambda p: (p.count("/"), p)))[0]
        return None

    def resolve_mdlink(self, target: str, source: str) -> str | None:
        path = unquote(target.split("#", 1)[0])
        if not path or path.startswith("/") or _SCHEME.match(path):
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
        return resolved if resolved in self.paths else None


def _outside_code(text: str, transform: Callable[[str], str]) -> str:
    """Apply `transform` to every part of `text` outside fenced and inline code."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        pos = 0
        for m in _INLINE_CODE.finditer(line):
            out.append(transform(line[pos : m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(transform(line[pos:]))
    return "".join(out)


def _is_relative(target: str) -> bool:
    path = target.split("#", 1)[0]
    return bool(path) and not path.startswith("/") and not _SCHEME.match(path)


def _requote(original: str, path: str) -> str:
    return quote(path, safe="/") if "%" in original else path


def links_to(text: str, source: str, target: str, files: VaultFiles) -> bool:
    found = False

    def scan(part: str) -> str:
        nonlocal found
        if any(
            files.resolve_wikilink(m.group("target"), source) == target
            for m in _WIKILINK.finditer(part)
        ):
            found = True
        if any(
            files.resolve_mdlink(m.group("target"), source) == target
            for m in _MDLINK.finditer(part)
        ):
            found = True
        return part

    _outside_code(text, scan)
    return found


def _wikilink_name(new: str, source: str, original: str, after: VaultFiles) -> str:
    """Shortest name that still resolves to `new` from `source`, in the original's style."""
    keep_ext = original.strip().endswith(".md")
    full = new if keep_ext or not new.endswith(".md") else new[:-3]
    if "/" in original.strip():
        return full
    base = posixpath.basename(full)
    return base if after.resolve_wikilink(base, source) == new else full


def rewrite_links(text: str, source: str, old: str, new: str, files: VaultFiles) -> str:
    """Re-point every link in `text` (a page at `source`) that resolves to `old`."""
    after = files.with_move(old, new)

    def wiki(m: re.Match[str]) -> str:
        if files.resolve_wikilink(m.group("target"), source) != old:
            return m.group(0)
        name = _wikilink_name(new, source, m.group("target"), after)
        return f"{m.group('bang')}[[{name}{m.group('heading') or ''}{m.group('alias') or ''}]]"

    def md(m: re.Match[str]) -> str:
        target = m.group("target")
        if files.resolve_mdlink(target, source) != old:
            return m.group(0)
        fragment = "#" + target.split("#", 1)[1] if "#" in target else ""
        rel = posixpath.relpath(new, posixpath.dirname(source) or ".")
        return f"{m.group('label')}({_requote(target, rel)}{fragment}{m.group('title') or ''})"

    return _outside_code(text, lambda part: _MDLINK.sub(md, _WIKILINK.sub(wiki, part)))


def rebase_relative_links(text: str, old_path: str, new_path: str) -> str:
    """Relative Markdown links inside a moved page keep pointing at the same files."""

    def md(m: re.Match[str]) -> str:
        target = m.group("target")
        if not _is_relative(target):
            return m.group(0)
        path, _, fragment = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(old_path), unquote(path)))
        rel = posixpath.relpath(resolved, posixpath.dirname(new_path) or ".")
        return f"{m.group('label')}({_requote(path, rel)}{'#' + fragment if fragment else ''}{m.group('title') or ''})"

    return _outside_code(text, lambda part: _MDLINK.sub(md, part))
