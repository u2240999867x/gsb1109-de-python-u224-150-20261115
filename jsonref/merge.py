"""Three-way merge for JSON documents that use JSON references.

The merge keeps ``$ref`` objects symbolic instead of dereferencing the output.
That distinction matters:

* a correction made to a shared target stays on the target and remains visible
  to every reference that still points at it;
* a reference that one side replaced with a literal is an independent local
  copy and does not inherit later corrections to the original target;
* a reference that one side redirected no longer observes its old target;
* two values that merely happen to be equal remain independent unless an
  explicit ``$ref`` relates them.

The helpers in this module never expand a reference graph.  Chained references
are followed with a visited set, so recursive and mutually recursive documents
terminate.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import math
import posixpath
import sys
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urldefrag


class _Missing:
    __slots__ = ()

    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        return False


MISSING = _Missing()


class _Unresolved:
    __slots__ = ()

    def __repr__(self):
        return "UNRESOLVED"


UNRESOLVED = _Unresolved()


class MergeError(Exception):
    """Raised when a merge cannot be completed or is explicitly rejected."""

    def __init__(self, message, conflicts=()):
        super().__init__(message)
        self.conflicts = list(conflicts)


def _is_ref(value):
    return isinstance(value, Mapping) and isinstance(value.get("$ref"), str)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equal(left, right, _seen=None):
    """Value equality with JSON types kept separate and NaN equal to NaN.

    ``_seen`` breaks cycles in Python objects; JSON documents themselves are
    finite, but callers may pass objects that already contain recursive
    structures.
    """
    if left is MISSING or right is MISSING:
        return left is right
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True
    if type(left) is not type(right):
        if _is_number(left) and _is_number(right):
            return left == right
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if len(left) != len(right):
            return False
        seen = _seen if _seen is not None else set()
        key = (id(left), id(right))
        if key in seen:
            return True
        seen.add(key)
        try:
            return all(
                other_key in right
                and _equal(value, right[other_key], seen)
                for other_key, value in left.items()
            )
        finally:
            seen.remove(key)
    if isinstance(left, Sequence) and not isinstance(left, str):
        if len(left) != len(right):
            return False
        seen = _seen if _seen is not None else set()
        key = (id(left), id(right))
        if key in seen:
            return True
        seen.add(key)
        try:
            return all(_equal(a, b, seen) for a, b in zip(left, right))
        finally:
            seen.remove(key)
    return left == right


def _copy(value):
    if value is MISSING or value is UNRESOLVED:
        return value
    return copy.deepcopy(value)


def _escape_pointer_part(part):
    return str(part).replace("~", "~0").replace("/", "~1")


def _json_pointer(path):
    return "".join("/" + _escape_pointer_part(part) for part in path)


def _jsonable(value):
    if value is MISSING:
        return {"missing": True}
    if value is UNRESOLVED:
        return {"unresolved": True}
    return _copy(value)


def _get_path(document, path):
    value = document
    if value is MISSING:
        return MISSING
    for part in path:
        if isinstance(value, Mapping):
            if part not in value:
                return MISSING
            value = value[part]
        elif isinstance(value, Sequence) and not isinstance(value, str):
            if not isinstance(part, int) or part < 0 or part >= len(value):
                return MISSING
            value = value[part]
        else:
            return MISSING
    return value


@dataclass(frozen=True)
class MergeConflict:
    """One unresolved difference, including both sides' original change.

    ``base``, ``ours`` and ``theirs`` are the values at ``path`` in the three
    inputs.  ``ours_change`` and ``theirs_change`` classify what each side did:
    ``value``, ``delete``, ``reference-redirect``, ``local-copy``,
    ``add-reference`` or ``keep-reference``.
    """

    path: tuple
    kind: str
    base: Any = MISSING
    ours: Any = MISSING
    theirs: Any = MISSING
    document: str = ""
    message: str = ""
    ours_change: Any = None
    theirs_change: Any = None

    @property
    def json_pointer(self):
        return _json_pointer(self.path)

    def to_dict(self):
        return {
            "document": self.document,
            "path": list(self.path),
            "json_pointer": self.json_pointer,
            "kind": self.kind,
            "message": self.message,
            "base": _jsonable(self.base),
            "ours": _jsonable(self.ours),
            "theirs": _jsonable(self.theirs),
            "ours_change": _copy(self.ours_change),
            "theirs_change": _copy(self.theirs_change),
        }

    def __str__(self):
        location = self.document or "<document>"
        if self.path:
            location += "#" + self.json_pointer
        return "%s: %s" % (location, self.message or self.kind)


@dataclass
class MergeResult:
    merged: Any
    conflicts: list = field(default_factory=list)
    analysis: dict = field(default_factory=dict)

    @property
    def clean(self):
        return not self.conflicts

    def raise_for_conflicts(self):
        if self.conflicts:
            raise MergeError(
                "%d merge conflict(s)" % len(self.conflicts), self.conflicts
            )
        return self.merged

    def to_dict(self):
        return {
            "clean": self.clean,
            "merged": _jsonable(self.merged),
            "analysis": _copy(self.analysis),
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


@dataclass
class DirectoryMergeResult:
    merged_files: dict
    conflicts: list
    analysis: dict = field(default_factory=dict)
    added: set = field(default_factory=set)
    deleted: set = field(default_factory=set)
    modified: set = field(default_factory=set)
    unchanged: set = field(default_factory=set)
    sources: dict = field(default_factory=dict)
    base_dir: Optional[Path] = None
    ours_dir: Optional[Path] = None
    theirs_dir: Optional[Path] = None

    @property
    def clean(self):
        return not self.conflicts

    def raise_for_conflicts(self):
        if self.conflicts:
            raise MergeError(
                "%d merge conflict(s)" % len(self.conflicts), self.conflicts
            )
        return self.merged_files

    def write(self, output_dir):
        """Write the merged tree, preserving bytes of untouched files."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in self.deleted:
            target = output_dir / name
            if target.is_file():
                target.unlink()
        for name, value in self.merged_files.items():
            target = output_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = self.sources.get(name)
            if name in self.unchanged and source is not None:
                target.write_bytes(Path(source).read_bytes())
            else:
                target.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        return output_dir

    def to_dict(self):
        return {
            "clean": self.clean,
            "added": sorted(self.added),
            "deleted": sorted(self.deleted),
            "modified": sorted(self.modified),
            "unchanged": sorted(self.unchanged),
            "analysis": _copy(self.analysis),
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "files": {
                name: _copy(value)
                for name, value in sorted(self.merged_files.items())
            },
        }


class ReferenceResolver:
    """Resolve local JSON pointers and relative file references."""

    def __init__(self, documents=None, document=""):
        self.documents = documents if documents is not None else {}
        self.document = document

    def _document_key(self, target, document):
        target = unquote(target)
        if target in self.documents:
            return target
        if document:
            candidate = posixpath.normpath(
                posixpath.join(posixpath.dirname(document), target)
            )
            if candidate in self.documents:
                return candidate
        candidate = posixpath.normpath(target)
        if candidate in self.documents:
            return candidate
        stripped = target.lstrip("./")
        if stripped in self.documents:
            return stripped
        return None

    def resolve_path(self, reference, document=None):
        """Return ``(document, path, value)`` or ``UNRESOLVED``."""
        document = self.document if document is None else document
        target, fragment = urldefrag(reference)
        if target:
            document = self._document_key(target, document)
            if document is None:
                return UNRESOLVED
        if document not in self.documents:
            return UNRESOLVED
        value = self.documents[document]
        path = []
        if fragment:
            parts = unquote(fragment.lstrip("/")).split("/")
            for raw in parts:
                part = raw.replace("~1", "/").replace("~0", "~")
                if isinstance(value, Sequence) and not isinstance(value, str):
                    try:
                        part = int(part)
                    except ValueError:
                        return UNRESOLVED
                if isinstance(value, Mapping):
                    if part not in value:
                        return UNRESOLVED
                elif isinstance(value, Sequence) and not isinstance(value, str):
                    if part < 0 or part >= len(value):
                        return UNRESOLVED
                else:
                    return UNRESOLVED
                value = value[part]
                path.append(part)
        return document, tuple(path), value

    def resolve(self, reference, document=None):
        resolved = self.resolve_path(reference, document)
        if resolved is UNRESOLVED:
            return UNRESOLVED
        document, _path, value = resolved
        return value, document

    def dereference(self, reference, document=None):
        """Follow a chain of references and return the final raw value.

        ``reference`` may be a URI string or a reference object; the latter is
        accepted so callers can pass a document node directly.
        """
        document = self.document if document is None else document
        if _is_ref(reference):
            reference = reference.get("$ref")
        seen = set()
        current = {"$ref": reference}
        while _is_ref(current):
            key = (document, current.get("$ref"))
            if key in seen:
                return UNRESOLVED
            seen.add(key)
            resolved = self.resolve(current.get("$ref"), document)
            if resolved is UNRESOLVED:
                return UNRESOLVED
            current, document = resolved
        return current

    def chain(self, reference, document=None):
        """Return the target locations visited before the chain ends.

        Each entry is ``{"document": ..., "path": (...)}}``.  The last entry
        may carry ``"cycle": True`` or ``"unresolved": True`` when the chain
        does not end at a concrete value.  The visited set prevents recursive
        documents from expanding forever.
        """
        document = self.document if document is None else document
        seen = set()
        reference = {"$ref": reference}
        result = []
        while _is_ref(reference):
            key = (document, reference.get("$ref"))
            if key in seen:
                result.append({"document": document, "path": (), "cycle": True})
                break
            seen.add(key)
            resolved = self.resolve_path(reference.get("$ref"), document)
            if resolved is UNRESOLVED:
                result.append({"document": document, "path": (), "unresolved": True})
                break
            document, path, value = resolved
            result.append({"document": document, "path": path})
            reference = value
        return result

    def key(self, reference, document=None):
        """Return a stable identity for the object a reference denotes.

        Two different spellings of the same local target compare equal.  Two
        separate locations that merely hold equal values do not: only an
        actual reference chain relates objects.
        """
        document = self.document if document is None else document
        entries = self.chain(reference, document)
        if entries and not entries[-1].get("cycle") and not entries[-1].get(
            "unresolved"
        ):
            target = entries[-1]
            return ("object", target["document"], target["path"])
        if entries and entries[-1].get("cycle"):
            return (
                "cycle",
                tuple((entry["document"], entry["path"]) for entry in entries),
            )
        target, fragment = urldefrag(reference)
        if target:
            document = self._document_key(target, document) or posixpath.normpath(
                posixpath.join(posixpath.dirname(document), unquote(target))
            )
        return ("uri", document, unquote(fragment))

    def describe_target(self, reference, document=None):
        key = self.key(reference, document)
        if key[0] == "object":
            return {"kind": "object", "document": key[1], "path": list(key[2])}
        if key[0] == "cycle":
            return {
                "kind": "cycle",
                "chain": [
                    {"document": item_document, "path": list(path)}
                    for item_document, path in key[1]
                ],
            }
        return {"kind": "unresolved", "document": key[1], "fragment": key[2]}


class _MergeContext:
    def __init__(
        self,
        document="",
        prefer="ours",
        base_resolver=None,
        ours_resolver=None,
        theirs_resolver=None,
    ):
        if prefer not in ("ours", "theirs"):
            raise ValueError("prefer must be 'ours' or 'theirs'")
        self.document = document
        self.prefer = prefer
        self.base_resolver = base_resolver or ReferenceResolver()
        self.ours_resolver = ours_resolver or ReferenceResolver()
        self.theirs_resolver = theirs_resolver or ReferenceResolver()
        self.conflicts = []

    def _describe_change(self, base_value, side_value, resolver):
        if side_value is MISSING:
            return {"kind": "delete"}
        if _is_ref(base_value) and _is_ref(side_value):
            if side_value.get("$ref") == base_value.get("$ref"):
                siblings = sorted(
                    key for key in side_value if key != "$ref"
                )
                return {
                    "kind": "keep-reference",
                    "reference": side_value.get("$ref"),
                    "sibling_keys": siblings,
                }
            return {
                "kind": "reference-redirect",
                "from": base_value.get("$ref"),
                "to": side_value.get("$ref"),
            }
        if _is_ref(base_value) and not _is_ref(side_value):
            # A local copy is classified against the common base target; a
            # target corrected on one side must not turn an untouched copy
            # into a different kind of change.
            target = self.base_resolver.dereference(
                {"$ref": base_value.get("$ref")}
            )
            return {
                "kind": "local-copy",
                "from": base_value.get("$ref"),
                "matches_target": target is not UNRESOLVED
                and _equal(side_value, target),
                "value": _copy(side_value),
            }
        if not _is_ref(base_value) and _is_ref(side_value):
            return {
                "kind": "add-reference",
                "reference": side_value.get("$ref"),
            }
        return {"kind": "value", "value": _copy(side_value)}

    def _conflict(
        self,
        path,
        kind,
        base,
        ours,
        theirs,
        message,
        ours_change=None,
        theirs_change=None,
    ):
        if ours_change is None:
            ours_change = self._describe_change(base, ours, self.ours_resolver)
        if theirs_change is None:
            theirs_change = self._describe_change(base, theirs, self.theirs_resolver)
        self.conflicts.append(
            MergeConflict(
                path=tuple(path),
                kind=kind,
                base=_copy(base),
                ours=_copy(ours),
                theirs=_copy(theirs),
                document=self.document,
                message=message,
                ours_change=ours_change,
                theirs_change=theirs_change,
            )
        )
        chosen = ours if self.prefer == "ours" else theirs
        if chosen is MISSING and base is not MISSING:
            # Deleting a value that only one side retained would silently drop
            # the other side's change.  Keep the surviving value instead; the
            # conflict still records both original states.
            chosen = theirs if self.prefer == "ours" else ours
        return _copy(chosen)

    def merge(self, base, ours, theirs, path=()):
        if _equal(ours, theirs):
            return _copy(ours)
        if _equal(base, ours):
            return _copy(theirs)
        if _equal(base, theirs):
            return _copy(ours)

        if ours is MISSING or theirs is MISSING:
            if ours is MISSING and theirs is MISSING:
                return MISSING
            if ours is MISSING:
                return self._conflict(
                    path,
                    "delete-modify",
                    base,
                    ours,
                    theirs,
                    "ours deleted this value while theirs modified it",
                )
            return self._conflict(
                path,
                "delete-modify",
                base,
                ours,
                theirs,
                "theirs deleted this value while ours modified it",
            )

        if _is_ref(base) or _is_ref(ours) or _is_ref(theirs):
            return self._merge_reference(base, ours, theirs, path)

        if isinstance(base, Mapping) and isinstance(ours, Mapping) and isinstance(
            theirs, Mapping
        ):
            return self._merge_mapping(base, ours, theirs, path)

        if (
            isinstance(base, Sequence)
            and not isinstance(base, str)
            and isinstance(ours, Sequence)
            and not isinstance(ours, str)
            and isinstance(theirs, Sequence)
            and not isinstance(theirs, str)
        ):
            return self._merge_sequence(base, ours, theirs, path)

        return self._conflict(
            path,
            "value",
            base,
            ours,
            theirs,
            "both sides changed the value differently",
        )

    def _merge_mapping(self, base, ours, theirs, path):
        result = OrderedDict()
        keys = list(base)
        for key in list(ours) + list(theirs):
            if key not in keys:
                keys.append(key)
        for key in keys:
            merged = self.merge(
                base.get(key, MISSING),
                ours.get(key, MISSING),
                theirs.get(key, MISSING),
                path + (key,),
            )
            if merged is not MISSING:
                result[key] = merged
        return dict(result)

    @staticmethod
    def _lcs_matches(base, side):
        n, m = len(base), len(side)
        if n * m > 4_000_000:
            # Large arrays fall back to an exact prefix/suffix trim rather
            # than an unbounded dynamic program.
            prefix = 0
            while prefix < n and prefix < m and _equal(base[prefix], side[prefix]):
                prefix += 1
            suffix = 0
            while (
                suffix < n - prefix
                and suffix < m - prefix
                and _equal(base[n - 1 - suffix], side[m - 1 - suffix])
            ):
                suffix += 1
            matches = {index: index for index in range(prefix)}
            matches.update(
                {
                    n - suffix + offset: m - suffix + offset
                    for offset in range(suffix)
                }
            )
            return matches
        table = [[0] * (m + 1) for _ in range(n + 1)]
        for i in range(n - 1, -1, -1):
            row = table[i]
            next_row = table[i + 1]
            for j in range(m - 1, -1, -1):
                if _equal(base[i], side[j]):
                    row[j] = next_row[j + 1] + 1
                else:
                    row[j] = max(next_row[j], row[j + 1])
        matches = {}
        i = j = 0
        while i < n and j < m:
            if _equal(base[i], side[j]):
                matches[i] = j
                i += 1
                j += 1
            elif table[i + 1][j] >= table[i][j + 1]:
                i += 1
            else:
                j += 1
        return matches

    @classmethod
    def _sequence_ops(cls, base, side):
        # An array that keeps its length is compared by position.  LCS alone
        # cannot match an element whose contents were edited, which would
        # turn a plain modification into a delete plus insert and hide a
        # delete-versus-modify conflict.
        if len(base) == len(side):
            return {index: index for index in range(len(base))}, {}
        matches = cls._lcs_matches(base, side)
        inserts = {}
        cursor = 0
        for index in range(len(base)):
            if index not in matches:
                continue
            match = matches[index]
            if match > cursor:
                inserts.setdefault(index, []).extend(side[cursor:match])
            cursor = match + 1
        if cursor < len(side):
            inserts.setdefault(len(base), []).extend(side[cursor:])
        return matches, inserts

    def _merge_sequence(self, base, ours, theirs, path):
        ours_match, ours_insert = self._sequence_ops(base, ours)
        theirs_match, theirs_insert = self._sequence_ops(base, theirs)
        result = []
        for gap in range(len(base) + 1):
            ours_new = ours_insert.get(gap, [])
            theirs_new = theirs_insert.get(gap, [])
            if ours_new and theirs_new:
                if _equal(ours_new, theirs_new):
                    result.extend(_copy(ours_new))
                else:
                    # Distinct insertions can coexist; keeping both preserves
                    # every non-conflicting addition.
                    result.extend(_copy(ours_new))
                    result.extend(_copy(theirs_new))
            elif ours_new:
                result.extend(_copy(ours_new))
            elif theirs_new:
                result.extend(_copy(theirs_new))
            if gap == len(base):
                break

            ours_has = gap in ours_match
            theirs_has = gap in theirs_match
            if ours_has and theirs_has:
                merged_element = self.merge(
                    base[gap],
                    ours[ours_match[gap]],
                    theirs[theirs_match[gap]],
                    path + (gap,),
                )
                if merged_element is not MISSING:
                    result.append(merged_element)
            elif ours_has:
                value = ours[ours_match[gap]]
                if _equal(base[gap], value):
                    continue
                merged_element = self._conflict(
                    path + (gap,),
                    "delete-modify",
                    base[gap],
                    value,
                    MISSING,
                    "theirs deleted this array element while ours modified it",
                )
                if merged_element is not MISSING:
                    result.append(merged_element)
            elif theirs_has:
                value = theirs[theirs_match[gap]]
                if _equal(base[gap], value):
                    continue
                merged_element = self._conflict(
                    path + (gap,),
                    "delete-modify",
                    base[gap],
                    MISSING,
                    value,
                    "ours deleted this array element while theirs modified it",
                )
                if merged_element is not MISSING:
                    result.append(merged_element)
        return result

    @staticmethod
    def _same_target(reference, resolver, other, other_resolver):
        """Compare references by the real object they denote, not spelling."""
        return resolver.key(reference) == other_resolver.key(other)

    def _merge_reference(self, base, ours, theirs, path):
        base_ref = base.get("$ref") if _is_ref(base) else None
        ours_ref = ours.get("$ref") if _is_ref(ours) else None
        theirs_ref = theirs.get("$ref") if _is_ref(theirs) else None

        if base_ref is not None and ours_ref is not None and theirs_ref is not None:
            same_base_ours = self._same_target(
                base_ref, self.base_resolver, ours_ref, self.ours_resolver
            )
            same_base_theirs = self._same_target(
                base_ref, self.base_resolver, theirs_ref, self.theirs_resolver
            )
            same_ours_theirs = self._same_target(
                ours_ref, self.ours_resolver, theirs_ref, self.theirs_resolver
            )
            if same_ours_theirs:
                chosen = ours_ref
            elif same_base_ours:
                chosen = theirs_ref
            elif same_base_theirs:
                chosen = ours_ref
            else:
                chosen = self._conflict(
                    path,
                    "ref-redirect",
                    base,
                    ours,
                    theirs,
                    "both sides redirected the reference differently "
                    "(ours to %r, theirs to %r)" % (ours_ref, theirs_ref),
                ).get("$ref", ours_ref)
            return self._finish_reference(base, ours, theirs, path, chosen)

        if base_ref is not None and (ours_ref is None) != (theirs_ref is None):
            if ours_ref is not None:
                return self._reference_versus_literal(
                    base, ours, theirs, path, ours_ref, theirs, ours_is_reference=True
                )
            return self._reference_versus_literal(
                base, ours, theirs, path, theirs_ref, ours, ours_is_reference=False
            )

        if base_ref is not None and ours_ref is None and theirs_ref is None:
            if isinstance(ours, Mapping) and isinstance(theirs, Mapping):
                return self.merge(
                    {key: value for key, value in base.items() if key != "$ref"},
                    ours,
                    theirs,
                    path,
                )
            return self._conflict(
                path,
                "ref-vs-value",
                base,
                ours,
                theirs,
                "both sides replaced the reference with different literal values",
            )

        if base_ref is None:
            if ours_ref is not None and theirs_ref is not None:
                if self._same_target(
                    ours_ref, self.ours_resolver, theirs_ref, self.theirs_resolver
                ):
                    return self._finish_reference(base, ours, theirs, path, ours_ref)
                chosen = self._conflict(
                    path,
                    "ref-redirect",
                    base,
                    ours,
                    theirs,
                    "both sides added different references "
                    "(ours %r, theirs %r)" % (ours_ref, theirs_ref),
                ).get("$ref", ours_ref)
                return self._finish_reference(base, ours, theirs, path, chosen)
            return self._conflict(
                path,
                "ref-vs-value",
                base,
                ours,
                theirs,
                "one side added a reference while the other added a literal value",
            )

        return self._conflict(
            path,
            "ref-vs-value",
            base,
            ours,
            theirs,
            "one side kept a reference while the other replaced it",
        )

    def _finish_reference(self, base, ours, theirs, path, chosen_ref):
        result = OrderedDict()
        result["$ref"] = chosen_ref
        base_extra = (
            {key: value for key, value in base.items() if key != "$ref"}
            if isinstance(base, Mapping)
            else {}
        )
        ours_extra = (
            {key: value for key, value in ours.items() if key != "$ref"}
            if isinstance(ours, Mapping)
            else {}
        )
        theirs_extra = (
            {key: value for key, value in theirs.items() if key != "$ref"}
            if isinstance(theirs, Mapping)
            else {}
        )
        merged_extra = self.merge(base_extra, ours_extra, theirs_extra, path)
        if isinstance(merged_extra, Mapping):
            for key, value in merged_extra.items():
                result[key] = value
        return dict(result)

    def _reference_versus_literal(
        self,
        base,
        ours,
        theirs,
        path,
        reference,
        literal,
        *,
        ours_is_reference,
    ):
        base_ref = base.get("$ref")
        reference_side = ours if ours_is_reference else theirs
        literal_side = theirs if ours_is_reference else ours
        reference_resolver = (
            self.ours_resolver if ours_is_reference else self.theirs_resolver
        )
        reference_unchanged = self._same_target(
            base_ref, self.base_resolver, reference, reference_resolver
        )
        reference_change = {
            "kind": "keep-reference"
            if reference_unchanged
            else "reference-redirect",
            "from": base_ref,
            "to": reference,
        }
        target = self.base_resolver.dereference({"$ref": base_ref})
        literal_change = {
            "kind": "local-copy",
            "from": base_ref,
            "matches_target": target is not UNRESOLVED
            and _equal(literal, target),
            "value": _copy(literal),
        }

        if reference_unchanged:
            # The reference side did not change identity.  The other side
            # disconnected the reference; the literal is an independent value.
            # Any sibling-property edits still merge, but the target's edits do
            # not flow into the copy.
            return self._apply_reference_extras(
                base, reference_side, literal, path
            )

        ours_change = reference_change if ours_is_reference else literal_change
        theirs_change = literal_change if ours_is_reference else reference_change
        if literal_change["matches_target"]:
            kind = "ref-redirect-vs-local-copy"
            message = (
                "one side redirected the reference while the other disconnected "
                "it in favour of a local copy"
            )
        else:
            kind = "ref-vs-value"
            message = (
                "one side redirected the reference while the other replaced it "
                "with a literal value"
            )
        return self._conflict(
            path,
            kind,
            base,
            ours,
            theirs,
            message,
            ours_change,
            theirs_change,
        )

    def _apply_reference_extras(self, base, reference_side, literal, path):
        """Merge sibling-property edits into a disconnected literal value.

        A literal that is an exact copy of the base target has no opinion on
        reference-only sibling keys; those keys are absent by construction,
        not deleted.  A genuinely different literal is a full replacement and
        participates in ordinary sibling deletion merges.
        """
        if not isinstance(literal, Mapping):
            return _copy(literal)
        base_extra = {
            key: value for key, value in base.items() if key != "$ref"
        }
        ref_extra = {
            key: value for key, value in reference_side.items() if key != "$ref"
        }
        target = self.base_resolver.dereference({"$ref": base.get("$ref")})
        literal_is_copy = target is not UNRESOLVED and _equal(literal, target)
        result = OrderedDict(literal)
        keys = list(ref_extra) + [key for key in base_extra if key not in ref_extra]
        for key in keys:
            before = base_extra.get(key, MISSING)
            after = ref_extra.get(key, MISSING)
            if _equal(before, after):
                continue
            if literal_is_copy and key not in result:
                if after is not MISSING:
                    merged = self.merge(before, after, before, path + (key,))
                else:
                    merged = MISSING
            else:
                existing = result.get(key, MISSING)
                merged = self.merge(before, after, existing, path + (key,))
            if merged is MISSING:
                result.pop(key, None)
            else:
                result[key] = merged
        return dict(result)


def merge3(
    base,
    ours,
    theirs,
    *,
    document="",
    prefer="ours",
    base_documents=None,
    ours_documents=None,
    theirs_documents=None,
):
    """Three-way merge three raw JSON values and return a :class:`MergeResult`.

    ``$ref`` objects are compared as references, not as resolved values.
    Edits inside a shared target are merged at the target location, so
    references that remain connected observe the merged value.  A side that
    replaced a reference with a literal keeps an independent local copy.

    ``prefer`` chooses the value retained at a conflicted location; both
    original values are always recorded on the conflict.
    """
    if base_documents is None:
        base_documents = {document: base}
    if ours_documents is None:
        ours_documents = {document: ours}
    if theirs_documents is None:
        theirs_documents = {document: theirs}
    context = _MergeContext(
        document=document,
        prefer=prefer,
        base_resolver=ReferenceResolver(base_documents, document),
        ours_resolver=ReferenceResolver(ours_documents, document),
        theirs_resolver=ReferenceResolver(theirs_documents, document),
    )
    merged = context.merge(base, ours, theirs, ())
    if merged is MISSING:
        merged = None
    analysis = analyse_references(
        base_documents,
        ours_documents,
        theirs_documents,
        {document: merged},
        documents=[document],
    )
    return MergeResult(merged, context.conflicts, analysis)


three_way_merge = merge3
merge_documents = merge3


def merge_files(base_path, ours_path, theirs_path, *, prefer="ours", document=None):
    """Merge three JSON files, preserving internal ``$ref`` objects."""
    paths = [Path(base_path), Path(ours_path), Path(theirs_path)]
    values = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            values.append(json.load(stream))
    document = str(document if document is not None else paths[0].name)
    return merge3(
        values[0],
        values[1],
        values[2],
        document=document,
        prefer=prefer,
        base_documents={document: values[0]},
        ours_documents={document: values[1]},
        theirs_documents={document: values[2]},
    )


def _walk_refs(document, path=()):
    """Yield ``(path, reference)`` for every reference object."""
    if _is_ref(document):
        yield path, document
        for key, value in document.items():
            if key == "$ref":
                continue
            yield from _walk_refs(value, path + (key,))
    elif isinstance(document, Mapping):
        for key, value in document.items():
            yield from _walk_refs(value, path + (key,))
    elif isinstance(document, Sequence) and not isinstance(document, str):
        for index, value in enumerate(document):
            yield from _walk_refs(value, path + (index,))


def _side_change(base_node, side_node, base_documents, side_documents, document):
    if side_node is MISSING:
        return {"kind": "delete"}
    if _is_ref(base_node) and _is_ref(side_node):
        base_resolver = ReferenceResolver(base_documents, document)
        side_resolver = ReferenceResolver(side_documents, document)
        if side_resolver.key(side_node.get("$ref")) == base_resolver.key(
            base_node.get("$ref")
        ):
            return {
                "kind": "keep-reference",
                "reference": side_node.get("$ref"),
                "sibling_keys": sorted(k for k in side_node if k != "$ref"),
            }
        return {
            "kind": "reference-redirect",
            "from": base_node.get("$ref"),
            "to": side_node.get("$ref"),
        }
    if not _is_ref(base_node) and _is_ref(side_node):
        return {"kind": "add-reference", "reference": side_node.get("$ref")}
    if _is_ref(base_node) and not _is_ref(side_node):
        # Compare the copy with the base target: the question is whether the
        # side disconnected an unchanged target, not how that target changed
        # on either side.
        base_target = ReferenceResolver(base_documents, document).dereference(
            {"$ref": base_node.get("$ref")}
        )
        return {
            "kind": "local-copy",
            "from": base_node.get("$ref"),
            "matches_target": base_target is not UNRESOLVED
            and _equal(side_node, base_target),
            "value": _copy(side_node),
        }
    return {"kind": "value", "value": _copy(side_node)}


def _chain_locations(resolver, reference, document):
    """Return the final target of a reference chain, if it is concrete.

    Intermediate reference objects are not returned: they are reference sites,
    not shared data targets.  Following only to the terminal value also keeps
    cycles bounded.
    """
    entries = resolver.chain(reference, document)
    if not entries:
        return []
    last = entries[-1]
    if last.get("cycle") or last.get("unresolved"):
        return []
    return [(last["document"], last["path"])]


def analyse_references(
    base_documents, ours_documents, theirs_documents, merged_documents, documents=None
):
    """Classify reference-related changes and their real object impact.

    The result separates shared-target corrections, reference redirections and
    local copies, and lists which references still observe a corrected target
    and which were disconnected from it.
    """
    names = list(
        documents
        if documents is not None
        else sorted(
            set(base_documents) | set(ours_documents) | set(theirs_documents)
        )
    )
    base_sites = {}
    for document in names:
        if document not in base_documents:
            continue
        for path, node in _walk_refs(base_documents[document]):
            base_sites[(document, path)] = node

    # Sites added by either side are also relevant: two sides may add
    # different references at the same place, or add references while the
    # other adds a literal.  They are classified against MISSING as the base.
    side_sites = {}
    for documents_map in (ours_documents, theirs_documents):
        for document in names:
            if document not in documents_map:
                continue
            for path, node in _walk_refs(documents_map[document]):
                side_sites.setdefault((document, path), node)

    redirects = []
    local_copies = []
    removed_references = []

    for (document, path) in sorted(set(base_sites) | set(side_sites)):
        base_node = base_sites.get((document, path), MISSING)
        ours_node = _get_path(ours_documents.get(document, MISSING), path)
        theirs_node = _get_path(theirs_documents.get(document, MISSING), path)
        ours_change = _side_change(
            base_node, ours_node, base_documents, ours_documents, document
        )
        theirs_change = _side_change(
            base_node, theirs_node, base_documents, theirs_documents, document
        )
        if (
            ours_change["kind"] == "reference-redirect"
            or theirs_change["kind"] == "reference-redirect"
            or ours_change["kind"] == "add-reference"
            or theirs_change["kind"] == "add-reference"
        ):
            redirects.append(
                {
                    "document": document,
                    "path": list(path),
                    "json_pointer": _json_pointer(path),
                    "from": base_node.get("$ref") if _is_ref(base_node) else None,
                    "ours": ours_change,
                    "theirs": theirs_change,
                }
            )
        if (
            ours_change["kind"] == "local-copy"
            or theirs_change["kind"] == "local-copy"
        ):
            merged_node = _get_path(merged_documents.get(document, MISSING), path)
            local_copies.append(
                {
                    "document": document,
                    "path": list(path),
                    "json_pointer": _json_pointer(path),
                    "from": base_node.get("$ref"),
                    "ours": ours_change
                    if ours_change["kind"] == "local-copy"
                    else None,
                    "theirs": theirs_change
                    if theirs_change["kind"] == "local-copy"
                    else None,
                    "merged_kind": "reference"
                    if _is_ref(merged_node)
                    else ("delete" if merged_node is MISSING else "value"),
                }
            )
        if (
            ours_change["kind"] == "delete"
            and theirs_change["kind"] == "delete"
        ):
            removed_references.append(
                {
                    "document": document,
                    "path": list(path),
                    "json_pointer": _json_pointer(path),
                    "reference": base_node.get("$ref"),
                }
            )

    # Target locations referenced at any point by any side.  Using every side
    # catches references redirected onto an existing target.
    target_locations = set()
    for documents_map in (
        base_documents,
        ours_documents,
        theirs_documents,
        merged_documents,
    ):
        for document in names:
            if document not in documents_map:
                continue
            resolver = ReferenceResolver(documents_map, document)
            for _path, node in _walk_refs(documents_map[document]):
                target_locations.update(
                    _chain_locations(resolver, node.get("$ref"), document)
                )

    merged_resolvers = {
        document: ReferenceResolver(merged_documents, document)
        for document in names
    }
    target_updates = []
    for document, path in sorted(target_locations, key=lambda item: (item[0], item[1])):
        base_value = _get_path(base_documents.get(document, MISSING), path)
        merged_value = _get_path(merged_documents.get(document, MISSING), path)
        if base_value is MISSING and merged_value is MISSING:
            continue
        if _equal(base_value, merged_value):
            continue

        affected = []
        disconnected = []
        redirected = []
        for (site_document, site_path), site_node in base_sites.items():
            base_resolver = ReferenceResolver(base_documents, site_document)
            if (document, path) not in _chain_locations(
                base_resolver, site_node.get("$ref"), site_document
            ):
                continue
            site_id = {
                "document": site_document,
                "path": list(site_path),
                "json_pointer": _json_pointer(site_path),
                "reference": site_node.get("$ref"),
            }
            merged_node = _get_path(
                merged_documents.get(site_document, MISSING), site_path
            )
            if merged_node is MISSING:
                disconnected.append({**site_id, "state": "removed"})
            elif not _is_ref(merged_node):
                disconnected.append({**site_id, "state": "local-copy"})
            else:
                resolver = merged_resolvers.get(site_document) or ReferenceResolver(
                    merged_documents, site_document
                )
                if (document, path) in _chain_locations(
                    resolver, merged_node.get("$ref"), site_document
                ):
                    affected.append(
                        {**site_id, "reference": merged_node.get("$ref")}
                    )
                else:
                    redirected.append(
                        {
                            **site_id,
                            "now": merged_node.get("$ref"),
                            "state": "redirected-away",
                        }
                    )

        # References that did not observe this target in the base but do in
        # the merged tree (for example a reference redirected onto it).
        newly_affected = []
        for site_document in names:
            if site_document not in merged_documents:
                continue
            resolver = merged_resolvers[site_document]
            for site_path, site_node in _walk_refs(merged_documents[site_document]):
                if (document, path) not in _chain_locations(
                    resolver, site_node.get("$ref"), site_document
                ):
                    continue
                base_node = base_sites.get((site_document, site_path))
                if base_node is not None:
                    base_resolver = ReferenceResolver(base_documents, site_document)
                    if (document, path) in _chain_locations(
                        base_resolver, base_node.get("$ref"), site_document
                    ):
                        continue
                newly_affected.append(
                    {
                        "document": site_document,
                        "path": list(site_path),
                        "json_pointer": _json_pointer(site_path),
                        "reference": site_node.get("$ref"),
                    }
                )

        reference_count = (
            len(affected)
            + len(redirected)
            + len(disconnected)
            + len(newly_affected)
        )
        target_updates.append(
            {
                "document": document,
                "path": list(path),
                "json_pointer": _json_pointer(path),
                "reference_count": reference_count,
                "base": _jsonable(base_value),
                "merged": _jsonable(merged_value),
                "affected_references": affected,
                "disconnected_references": disconnected,
                "redirected_references": redirected,
                "newly_affected_references": newly_affected,
            }
        )

    return {
        "shared_target_updates": target_updates,
        "reference_redirects": redirects,
        "local_copies": local_copies,
        "removed_references": removed_references,
    }


def _collect_files(root, pattern, recursive):
    root = Path(root)
    if not root.is_dir():
        raise MergeError("not a directory: %s" % root)
    iterator = root.rglob("*") if recursive else root.glob("*")
    files = set()
    for path in iterator:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
            files.add(relative)
    return files


def _load_documents(root, names):
    documents = {}
    for name in names:
        path = Path(root) / name
        try:
            with path.open(encoding="utf-8") as stream:
                documents[name] = json.load(stream)
        except (OSError, ValueError) as error:
            raise MergeError("cannot read %s: %s" % (path, error)) from error
    return documents


def merge_directories(
    base_dir,
    ours_dir,
    theirs_dir,
    *,
    pattern="*.json",
    recursive=True,
    prefer="ours",
):
    """Merge three configuration directories file by file.

    References between files are resolved per side for classification, but the
    merged output keeps every reference symbolic.  A target correction
    therefore flows to all references that remain connected in the merged
    tree, independent of the order in which files are merged.
    """
    base_dir = Path(base_dir)
    ours_dir = Path(ours_dir)
    theirs_dir = Path(theirs_dir)
    if prefer not in ("ours", "theirs"):
        raise ValueError("prefer must be 'ours' or 'theirs'")
    base_names = _collect_files(base_dir, pattern, recursive)
    ours_names = _collect_files(ours_dir, pattern, recursive)
    theirs_names = _collect_files(theirs_dir, pattern, recursive)

    all_names = sorted(base_names | ours_names | theirs_names)
    base_docs = _load_documents(base_dir, base_names)
    ours_docs = _load_documents(ours_dir, ours_names)
    theirs_docs = _load_documents(theirs_dir, theirs_names)

    merged_files = {}
    sources = {}
    conflicts = []
    added, deleted, modified, unchanged = set(), set(), set(), set()

    # First merge file contents.  The cross-file analysis runs afterwards on
    # the complete merged document set, so file order cannot affect whether a
    # reference is judged connected.
    for name in all_names:
        in_base = name in base_names
        in_ours = name in ours_names
        in_theirs = name in theirs_names

        if not in_ours and not in_theirs:
            deleted.add(name)
            continue

        if in_ours and not in_theirs:
            if not in_base:
                merged_files[name] = _copy(ours_docs[name])
                sources[name] = str(ours_dir / name)
                added.add(name)
            elif _equal(base_docs[name], ours_docs[name]):
                deleted.add(name)
            else:
                conflicts.append(
                    MergeConflict(
                        path=(),
                        kind="file-delete-modify",
                        base=base_docs[name],
                        ours=ours_docs[name],
                        theirs=MISSING,
                        document=name,
                        message="theirs deleted the file while ours modified it",
                        ours_change={"kind": "modify"},
                        theirs_change={"kind": "delete"},
                    )
                )
                if prefer == "ours":
                    merged_files[name] = _copy(ours_docs[name])
                    sources[name] = str(ours_dir / name)
                    modified.add(name)
                else:
                    deleted.add(name)
            continue

        if in_theirs and not in_ours:
            if not in_base:
                merged_files[name] = _copy(theirs_docs[name])
                sources[name] = str(theirs_dir / name)
                added.add(name)
            elif _equal(base_docs[name], theirs_docs[name]):
                deleted.add(name)
            else:
                conflicts.append(
                    MergeConflict(
                        path=(),
                        kind="file-delete-modify",
                        base=base_docs[name],
                        ours=MISSING,
                        theirs=theirs_docs[name],
                        document=name,
                        message="ours deleted the file while theirs modified it",
                        ours_change={"kind": "delete"},
                        theirs_change={"kind": "modify"},
                    )
                )
                if prefer == "ours":
                    deleted.add(name)
                else:
                    merged_files[name] = _copy(theirs_docs[name])
                    sources[name] = str(theirs_dir / name)
                    modified.add(name)
            continue

        if not in_base:
            # Two sides adding the same file are merged structurally when both
            # are objects, so disjoint additions survive.  Other value shapes
            # or identical-key conflicts are reported normally.
            entry = (
                {}
                if isinstance(ours_docs[name], Mapping)
                and isinstance(theirs_docs[name], Mapping)
                else MISSING
            )
            result = merge3(
                entry,
                ours_docs[name],
                theirs_docs[name],
                document=name,
                prefer=prefer,
                base_documents={name: entry} if entry is not MISSING else {},
                ours_documents=ours_docs,
                theirs_documents=theirs_docs,
            )
            merged_files[name] = result.merged
            sources[name] = str(ours_dir / name)
            if result.conflicts:
                conflicts.extend(result.conflicts)
                modified.add(name)
            elif _equal(ours_docs[name], theirs_docs[name]):
                added.add(name)
            else:
                modified.add(name)
            continue

        result = merge3(
            base_docs[name],
            ours_docs[name],
            theirs_docs[name],
            document=name,
            prefer=prefer,
            base_documents=base_docs,
            ours_documents=ours_docs,
            theirs_documents=theirs_docs,
        )
        merged_files[name] = result.merged
        sources[name] = str(ours_dir / name)
        if result.conflicts:
            conflicts.extend(result.conflicts)
            modified.add(name)
        elif _equal(base_docs[name], result.merged):
            unchanged.add(name)
            if _equal(ours_docs[name], base_docs[name]):
                sources[name] = str(theirs_dir / name)
        else:
            modified.add(name)

    analysis = analyse_references(
        base_docs, ours_docs, theirs_docs, merged_files, documents=all_names
    )
    return DirectoryMergeResult(
        merged_files=merged_files,
        conflicts=conflicts,
        analysis=analysis,
        added=added,
        deleted=deleted,
        modified=modified,
        unchanged=unchanged,
        sources=sources,
        base_dir=base_dir,
        ours_dir=ours_dir,
        theirs_dir=theirs_dir,
    )


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="jsonref-merge",
        description="Three-way merge JSON documents or directories while "
        "preserving JSON references.",
    )
    parser.add_argument("base", help="common base file or directory")
    parser.add_argument("ours", help="first modified file or directory")
    parser.add_argument("theirs", help="second modified file or directory")
    parser.add_argument("-o", "--output", help="output file or directory")
    parser.add_argument(
        "--prefer",
        choices=("ours", "theirs"),
        default="ours",
        help="value kept at a conflicted location (default: ours)",
    )
    parser.add_argument(
        "--pattern",
        default="*.json",
        help="directory file pattern (default: *.json)",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="do not descend into subdirectories",
    )
    parser.add_argument("--report", help="write a machine-readable report here")
    parser.add_argument("--quiet", action="store_true", help="only report conflicts")
    arguments = parser.parse_args(argv)

    base = Path(arguments.base)
    ours = Path(arguments.ours)
    theirs = Path(arguments.theirs)
    directory_mode = base.is_dir() or ours.is_dir() or theirs.is_dir()
    if directory_mode:
        if not (base.is_dir() and ours.is_dir() and theirs.is_dir()):
            parser.error("all three inputs must be directories in directory mode")
        result = merge_directories(
            base,
            ours,
            theirs,
            pattern=arguments.pattern,
            recursive=not arguments.no_recursive,
            prefer=arguments.prefer,
        )
        if arguments.output:
            result.write(arguments.output)
        elif not arguments.quiet:
            json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
    else:
        result = merge_files(base, ours, theirs, prefer=arguments.prefer)
        if arguments.output:
            Path(arguments.output).write_text(
                json.dumps(result.merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif not arguments.quiet:
            json.dump(result.merged, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")

    if not arguments.quiet:
        for conflict in result.conflicts:
            print(str(conflict), file=sys.stderr)
    if arguments.report:
        Path(arguments.report).write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 1 if result.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
