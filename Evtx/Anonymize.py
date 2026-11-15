#    This file is part of python-evtx.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""Policy-driven anonymization for rendered Windows event XML.

Only values selected by an explicit path policy are changed. This is not a
global string substitution: if an identifier also occurs in ``Provider/@Name``,
``Computer`` or another unselected field, that occurrence is left untouched.

Supported path syntax (an XPath subset):

* ``/Event/System/Security/@UserID`` selects an attribute.
* ``/Event/EventData/Data[@Name='TargetUserName']/text()`` selects text.
* A path may end at an element; its text content is then the target.
* Segment names may be written as ``prefix:name``, ``{namespace-uri}name`` or
  plain ``name``. A plain name uses ``default_namespace``; if that is not
  configured, the document root namespace is used. Prefixes must be declared
  in ``namespaces``.
* ``//`` selects descendants, and ``*`` selects any element name.
* Predicates are evaluated left to right and may be combined:
  ``[n]`` (1-based position), ``[last()]``, ``[position()=n]``, ``[@name]``,
  ``[@name='value']`` and ``[text()='value']``.
* Namespace URIs are part of matching: an element in a different namespace
  with the same local name does not match. Use an explicit prefix or
  ``{uri}name`` to address such an element.
* Positions are per parent, so repeated siblings are addressed by occurrence,
  for example ``Data[@Name='TargetUserName'][2]``. This is stable even when
  two duplicates have identical values.

A policy path may also be an object with ``path`` plus ``index`` (duplicate
occurrence) or ``namespace`` (prefix applied to unqualified segments), which
makes duplicate and namespace-sensitive selection explicit in JSON.

Replacement is value based and stable: the same identifier always receives
the same pseudonym across events and across selected paths. A policy may map
several originals (for example a SID and its account name) to one pseudonym,
which keeps related fields correlated. Unmapped values receive a
deterministic pseudonym derived from a salted SHA-256 digest, so repeated
runs over the same input produce the same output without a state database.
"""

from __future__ import absolute_import

import hashlib
import json
import re

try:
    from lxml import etree
except ImportError:  # pragma: no cover - reported when anonymization is used
    etree = None

EVENT_NS = "http://schemas.microsoft.com/win/2004/08/events/event"

DEFAULT_PREFIX = "USER-"
DEFAULT_SALT = "python-evtx-anonymize"
DEFAULT_DIGEST_LENGTH = 12
DEFAULT_SKIP_VALUES = ("", "-")

_ANY_NAMESPACE = object()
_WILDCARD_LOCAL = "*"
_TEXT = "#text"
_SEGMENT_PATTERN = re.compile(r"^(.*?)((?:\[[^\]]*\])*)$")
_PREDICATE_PATTERN = re.compile(r"\[([^\]]*)\]")
_INDEX_PATTERN = re.compile(r"^\d+$")
_LAST_PATTERN = re.compile(r"^last\(\)$")
_POSITION_PATTERN = re.compile(r"^position\(\)\s*=\s*(\d+)$")


class AnonymizeError(Exception):
    """Base class for anonymization errors."""


class PolicyError(AnonymizeError, ValueError):
    """Raised when a policy is invalid."""


class PathError(PolicyError):
    """Raised when a path cannot be parsed."""


class StructureError(AnonymizeError):
    """Raised when anonymization would change the event structure."""


def _etree():
    if etree is None:
        raise AnonymizeError("lxml is required for event anonymization")
    return etree


def parse_xml(xml):
    """Parse an XML string or bytes into an lxml element."""
    return _etree().fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)


def to_xml(element):
    """Serialize an element to text, keeping namespace declarations intact."""
    return _etree().tostring(element, encoding="unicode")


def local_name(tag):
    """Return the local name of a possibly namespaced tag or attribute."""
    return etree.QName(tag).localname


def namespace(tag):
    """Return the namespace URI of a tag or attribute, or the empty string."""
    return etree.QName(tag).namespace or ""


_local_name = local_name
_namespace = namespace


def split_path(path):
    """Split a path on slashes that are outside braces, brackets and quotes.

    Returns element/attribute/text segments, with ``"//"`` inserted as a
    marker where the policy asked for an XPath descendant axis. Policies can
    then address fields whose nesting depth varies between providers without
    falling back to substring matching.
    """
    if not isinstance(path, str):
        raise PathError("path must be a string")
    text = path.strip()
    if not text:
        raise PathError("path must not be empty")
    raw = []
    current = []
    braces = 0
    brackets = 0
    quote = None
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == "{":
            braces += 1
            current.append(char)
        elif char == "}":
            braces -= 1
            if braces < 0:
                raise PathError("unbalanced namespace braces in path: %s" % path)
            current.append(char)
        elif char == "[":
            brackets += 1
            current.append(char)
        elif char == "]":
            brackets -= 1
            if brackets < 0:
                raise PathError("unbalanced predicate brackets in path: %s" % path)
            current.append(char)
        elif char == "/" and braces == 0 and brackets == 0:
            raw.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote is not None or braces or brackets:
        raise PathError("unterminated quote, namespace or predicate in path: %s" % path)
    raw.append("".join(current))

    if text.startswith("/") and raw and raw[0] == "":
        raw = raw[1:]
    if not raw or raw == [""]:
        raise PathError("path has no steps: %s" % path)
    if raw[-1] == "":
        raise PathError("trailing slash in path: %s" % path)

    parts = []
    for index, piece in enumerate(raw):
        if piece:
            parts.append(piece)
            continue
        if index == 0:
            # "//Event": descendant from the document root.
            parts.append("//")
        elif parts and parts[-1] != "//":
            parts.append("//")
        else:
            raise PathError("more than two consecutive slashes in path: %s" % path)
    return parts


def _parse_element_name(name, namespaces):
    """Return ``(namespace, local)`` for an element segment.

    A plain name returns ``_ANY_NAMESPACE``; the effective default namespace
    is resolved at selection time from the document root.
    """
    if name == _WILDCARD_LOCAL:
        return _ANY_NAMESPACE, _WILDCARD_LOCAL
    if name.startswith("{"):
        end = name.find("}")
        if end < 0:
            raise PathError("unterminated namespace in name: %s" % name)
        namespace = name[1:end]
        local = name[end + 1 :]
        if not local:
            raise PathError("missing local name in: %s" % name)
        return namespace, local
    if ":" in name:
        prefix, local = name.split(":", 1)
        if not prefix or not local:
            raise PathError("invalid qualified name: %s" % name)
        if prefix not in namespaces:
            raise PathError("unbound namespace prefix: %s" % prefix)
        return namespaces[prefix], local
    return _ANY_NAMESPACE, name


def _parse_attribute_name(name, namespaces):
    if not name:
        raise PathError("attribute path segment has no name")
    if name == _WILDCARD_LOCAL:
        return _ANY_NAMESPACE, _WILDCARD_LOCAL
    if name.startswith("{") or ":" in name:
        return _parse_element_name(name, namespaces)
    # Unprefixed attributes are in no namespace, even under a default xmlns.
    return "", name


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


class _Predicate(object):
    def __init__(self, kind, value=None, attribute=None):
        self.kind = kind
        self.value = value
        self.attribute = attribute

    def apply(self, candidates):
        if self.kind == "index":
            return candidates[self.value - 1 : self.value] if self.value <= len(candidates) else []
        if self.kind == "last":
            return candidates[-1:] if candidates else []
        if self.kind == "attribute_exists":
            return [candidate for candidate in candidates if self._attribute(candidate) is not None]
        if self.kind == "attribute_equals":
            return [candidate for candidate in candidates if self._attribute(candidate) == self.value]
        if self.kind == "text_equals":
            return [candidate for candidate in candidates if (candidate.text or "") == self.value]
        raise PathError("unknown predicate kind: %s" % self.kind)

    def _attribute(self, candidate):
        namespace, local = self.attribute
        for key, value in candidate.attrib.items():
            qname = etree.QName(key)
            if qname.localname == local and (qname.namespace or "") == namespace:
                return value
        return None

    def key(self):
        if self.kind == "attribute_exists":
            return ("@",) + self.attribute
        if self.kind == "attribute_equals":
            return ("@=",) + self.attribute + (self.value,)
        return (self.kind, self.value)


def _parse_predicate(text, namespaces):
    text = text.strip()
    if not text:
        raise PathError("empty predicate")
    if _INDEX_PATTERN.match(text):
        index = int(text)
        if index < 1:
            raise PathError("path indexes are 1-based: [%s]" % text)
        return _Predicate("index", index)
    if _LAST_PATTERN.match(text):
        return _Predicate("last")
    match = _POSITION_PATTERN.match(text)
    if match:
        index = int(match.group(1))
        if index < 1:
            raise PathError("path positions are 1-based: [%s]" % text)
        return _Predicate("index", index)
    if text.startswith("@"):
        expression = text[1:]
        if "=" in expression:
            name, raw_value = expression.split("=", 1)
            namespace, local = _parse_attribute_name(name.strip(), namespaces)
            return _Predicate("attribute_equals", _unquote(raw_value), (namespace, local))
        namespace, local = _parse_attribute_name(expression.strip(), namespaces)
        return _Predicate("attribute_exists", attribute=(namespace, local))
    if text.startswith("text()"):
        expression = text[len("text()") :].strip()
        if not expression.startswith("="):
            raise PathError("text() predicate requires a value: [%s]" % text)
        return _Predicate("text_equals", _unquote(expression[1:]))
    raise PathError("unsupported predicate: [%s]" % text)


class _ElementStep(object):
    def __init__(self, namespace, local, predicates, descendant=False):
        self.namespace = namespace
        self.local = local
        self.predicates = predicates
        self.descendant = descendant

    def effective_namespace(self, default_namespace):
        if self.namespace is _ANY_NAMESPACE:
            return default_namespace
        return self.namespace

    def matches(self, element, default_namespace):
        if self.local != _WILDCARD_LOCAL and _local_name(element.tag) != self.local:
            return False
        if self.local == _WILDCARD_LOCAL and self.namespace is _ANY_NAMESPACE:
            # XPath "*" matches any element regardless of namespace.
            return True
        namespace = self.effective_namespace(default_namespace)
        if namespace is None:
            return True
        return _namespace(element.tag) == namespace

    def candidates(self, parent, default_namespace):
        if self.descendant:
            # XPath's descendant axis selects every matching descendant. Any
            # positional predicate then applies among siblings that share a
            # parent, not across the whole descendant set.
            matched = []
            for child in parent.iterdescendants():
                if isinstance(child.tag, str) and self.matches(child, default_namespace):
                    matched.append(child)
            groups = {}
            for child in matched:
                groups.setdefault(id(child.getparent()), []).append(child)
            selected = []
            for group in groups.values():
                selected.extend(self._apply(group))
            return selected
        matched = [child for child in parent if isinstance(child.tag, str) and self.matches(child, default_namespace)]
        return self._apply(matched)

    def _apply(self, matched):
        for predicate in self.predicates:
            matched = predicate.apply(matched)
            if not matched:
                break
        return matched

    def key(self, default_namespace):
        return (
            self.effective_namespace(default_namespace),
            self.local,
            tuple(predicate.key() for predicate in self.predicates),
            self.descendant,
        )


class _AttributeStep(object):
    def __init__(self, namespace, local):
        self.namespace = namespace
        self.local = local

    def keys(self, element):
        for key in element.attrib:
            qname = etree.QName(key)
            if self.local not in (_WILDCARD_LOCAL, qname.localname):
                continue
            if self.local == _WILDCARD_LOCAL or (qname.namespace or "") == self.namespace:
                yield key

    def key(self):
        return ("@", self.namespace, self.local)


class _TextStep(object):
    def key(self):
        return ("text()",)


class CompiledPath(object):
    def __init__(self, original, steps, default_namespace):
        self.original = original
        self.steps = steps
        self.default_namespace = default_namespace
        self.key = tuple(
            step.key(default_namespace) if isinstance(step, _ElementStep) else step.key() for step in steps
        )

    def __repr__(self):
        return "CompiledPath(%r)" % self.original

    def select(self, root):
        """Return ``[(element, attribute-key-or-None), ...]`` targets."""
        if not self.steps:
            return []
        terminal = self.steps[-1]
        element_steps = self.steps[:-1] if not isinstance(terminal, _ElementStep) else self.steps

        current = [root]
        for index, step in enumerate(element_steps):
            if index == 0 and step.descendant:
                # "//Event/...": match the root as well as any descendant.
                matches = [element for element in current if step.matches(element, self.default_namespace)]
                matches.extend(
                    child
                    for child in root.iterdescendants()
                    if isinstance(child.tag, str) and step.matches(child, self.default_namespace)
                )
                current = step._apply(matches)
            elif index == 0 and step.matches(root, self.default_namespace):
                # An absolute path normally begins at the Event root. If it
                # does not name the root, it is treated as relative to it so
                # both "/Event/..." and "/System/..." are accepted.
                current = [root]
                for predicate in step.predicates:
                    current = predicate.apply(current)
                    if not current:
                        return []
            else:
                current = [child for parent in current for child in step.candidates(parent, self.default_namespace)]
            if not current:
                return []

        if isinstance(terminal, _ElementStep):
            return [(element, None) for element in current]
        if isinstance(terminal, _TextStep):
            return [(element, None) for element in current]
        return [(element, attribute_key) for element in current for attribute_key in terminal.keys(element)]


def compile_path(path, namespaces=None, default_namespace=None):
    """Compile a path string into a :class:`CompiledPath`."""
    namespaces = namespaces or {}
    parts = split_path(path)
    steps = []
    descendant = False
    for index, part in enumerate(parts):
        if part == "//":
            if descendant:
                raise PathError("consecutive descendant axes in path: %s" % path)
            descendant = True
            continue
        if part.startswith("@"):
            if index != len(parts) - 1:
                raise PathError("attribute must be the final path segment: %s" % path)
            namespace, local = _parse_attribute_name(part[1:], namespaces)
            steps.append(_AttributeStep(namespace, local))
            continue
        if part == "text()":
            if index != len(parts) - 1:
                raise PathError("text() must be the final path segment: %s" % path)
            steps.append(_TextStep())
            continue
        match = _SEGMENT_PATTERN.match(part)
        if not match:
            raise PathError("invalid path segment: %s" % part)
        name_text, predicate_text = match.groups()
        if not name_text:
            raise PathError("missing element name in path segment: %s" % part)
        namespace, local = _parse_element_name(name_text, namespaces)
        predicates = [_parse_predicate(value, namespaces) for value in _PREDICATE_PATTERN.findall(predicate_text)]
        steps.append(_ElementStep(namespace, local, predicates, descendant=descendant))
        descendant = False
    if descendant:
        raise PathError("path ends with a descendant axis: %s" % path)
    if not steps:
        raise PathError("path has no steps: %s" % path)
    if isinstance(steps[0], (_AttributeStep, _TextStep)):
        raise PathError("path must begin with an element: %s" % path)
    return CompiledPath(path, steps, default_namespace)


def _parse_mapping(mapping, label):
    """Normalise a mapping that is an object or a from/to list.

    ``to`` may be a string or any JSON scalar, which is converted to text.
    ``from`` may be one string or a list of strings, which is how a group of
    related identifiers (for example a SID and its account name) is sent to
    one pseudonym.
    """
    result = {}
    if mapping is None:
        return result
    if isinstance(mapping, dict):
        for source, target in mapping.items():
            if isinstance(target, (list, tuple)):
                if len(target) != 2 or not _is_scalar(target[1]):
                    raise PolicyError("%s has an invalid grouped replacement for %r" % (label, source))
                target = target[1]
            if not _is_scalar(source) or not _is_scalar(target):
                raise PolicyError("%s must map scalar values to scalar values" % label)
            result[str(source)] = str(target)
        return result
    if isinstance(mapping, list):
        for entry in mapping:
            if not isinstance(entry, dict):
                raise PolicyError("%s entries must be objects" % label)
            source = entry.get("from", entry.get("original", entry.get("sources")))
            target = entry.get("to", entry.get("replacement"))
            if not _is_scalar(target):
                raise PolicyError("%s entries need a scalar to value" % label)
            sources = source if isinstance(source, (list, tuple)) else [source]
            if not sources or not all(_is_scalar(item) for item in sources):
                raise PolicyError("%s entries need scalar from values" % label)
            for item in sources:
                key = str(item)
                if key in result and result[key] != str(target):
                    raise PolicyError("%s has conflicting replacement for %r" % (label, key))
                result[key] = str(target)
        return result
    raise PolicyError("%s must be an object or a list" % label)


def _is_scalar(value):
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _normalise_path_specs(specs, label):
    """Return ``[(path, path_mapping), ...]`` from several accepted shapes.

    Accepted shapes: a path string; a list of strings; a list of objects
    with ``path``/``paths`` and optional ``mapping``; a list of path
    component lists; or an object mapping path strings to optional
    mappings. A path object may also carry ``index``/``position`` to select
    a duplicate occurrence and ``namespace`` to prefix an unprefixed path
    with a declared prefix.
    """
    if specs is None:
        return []
    if isinstance(specs, str):
        return [(specs, {})]
    if isinstance(specs, dict):
        entries = []
        for path, path_mapping in specs.items():
            if isinstance(path_mapping, (dict, list)) or path_mapping is None:
                entries.append((path, _parse_mapping(path_mapping, "%s mapping" % label)))
            else:
                raise PolicyError("%s mapping for %r must be an object" % (label, path))
        return entries
    if isinstance(specs, (list, tuple)):
        entries = []
        for entry in specs:
            if isinstance(entry, str):
                entries.append((entry, {}))
                continue
            if isinstance(entry, dict):
                path = entry.get("path", entry.get("paths", entry.get("steps")))
                path_mapping = _parse_mapping(
                    entry.get("mapping", entry.get("map", entry.get("replace"))),
                    "%s mapping" % label,
                )
                occurrence = entry.get("index", entry.get("position", entry.get("occurrence")))
                namespace_prefix = entry.get("namespace")
                for item in _normalise_path_specs(path, label):
                    path_text = item[0]
                    if occurrence is not None:
                        path_text = _add_occurrence(path_text, occurrence)
                    if namespace_prefix is not None:
                        path_text = _prefix_unqualified_path(path_text, namespace_prefix)
                    entries.append((path_text, {**item[1], **path_mapping}))
                continue
            if isinstance(entry, (list, tuple)) and entry and all(isinstance(part, str) for part in entry):
                entries.append(("/".join(entry), {}))
                continue
            raise PolicyError("%s must contain path strings or path objects" % label)
        return entries
    raise PolicyError("%s must be a path, a list or an object" % label)


def _add_occurrence(path, occurrence):
    """Append ``[n]`` to the final element segment of a path."""
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise PolicyError("path occurrence must be a positive integer")
    if re.search(r"\[\s*(?:\d+|last\(\s*\)|position\(\))\s*\]", path):
        raise PolicyError("path already constrains its position: %s" % path)
    final = path.rsplit("/", 1)[-1]
    prefix = path[: len(path) - len(final)]
    if final.startswith("@") or final.strip() == "text()":
        raise PolicyError("occurrence applies to an element path: %s" % path)
    return "%s%s[%d]" % (prefix, final, occurrence)


def _prefix_unqualified_path(path, prefix):
    """Prefix every unprefixed element segment with the given namespace prefix."""
    if not isinstance(prefix, str) or not prefix:
        raise PolicyError("namespace must be a non-empty prefix")
    parts = split_path(path)
    rebuilt = []
    for part in parts:
        if part == "//":
            rebuilt.append(part)
            continue
        if part.startswith("@"):
            name = part[1:]
            if ":" not in name and not name.startswith("{"):
                name = "%s:%s" % (prefix, name)
            rebuilt.append("@" + name)
            continue
        if part.strip() == "text()":
            rebuilt.append("text()")
            continue
        match = _SEGMENT_PATTERN.match(part)
        name_text, predicate_text = match.groups()
        if ":" not in name_text and not name_text.startswith("{") and name_text != _WILDCARD_LOCAL:
            name_text = "%s:%s" % (prefix, name_text)
        rebuilt.append(name_text + predicate_text)
    return "/".join(rebuilt)


def _add_mapping(target, mapping, label):
    for source, replacement in mapping.items():
        if source in target and target[source] != replacement:
            raise PolicyError("%s has conflicting replacement for %r" % (label, source))
        target[source] = replacement


class AnonymizationPolicy(object):
    """A validated anonymization policy.

    ``data`` may be a decoded JSON object, a path list, or a JSON file path::

        AnonymizationPolicy({"paths": [...], "mapping": {...}})
        AnonymizationPolicy(["/Event/System/Security/@UserID"])
        AnonymizationPolicy.from_file("policy.json")

    Recognised keys:

    ``paths`` (also accepted: ``allowed_paths``, ``anonymous_paths``)
        Paths allowed to be modified. At least one path is required. Each
        entry may be a string, or an object with ``path``/``paths`` plus an
        optional path-specific ``mapping``.

    ``namespaces``
        Prefix to URI mapping. Use ``""`` for a default prefix.

    ``default_namespace``
        Namespace for unprefixed path segments. Defaults to the document
        root namespace.

    ``mapping``
        Explicit original-to-replacement mapping, as an object or a list of
        ``{"from": ..., "to": ...}`` entries. ``from`` may be a list, which
        sends related identifiers to one pseudonym.

    ``relations``
        Optional groups ``{"paths": [...], "mapping": {...}}``. Their paths
        are implicitly allowed, and their mapping is merged into the global
        value mapping, so a SID and an account name can share a pseudonym.

    ``prefix``, ``salt``, ``digest_length``, ``scheme``
        Generation settings for unmapped values. ``scheme`` is
        ``"sha256"`` (default) or ``"counter"``.

    ``skip_values``
        Unmapped values to leave untouched. Defaults to ``""`` and ``"-"``.
    """

    def __init__(self, data=None, **options):
        if data is None:
            data = {}
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, (list, tuple)):
            data = {"paths": list(data)}
        if not isinstance(data, dict):
            raise PolicyError("policy must be a JSON object")
        data = dict(data)
        data.update(options)
        self.data = dict(data)
        self.namespaces = dict(data.get("namespaces") or data.get("namespace_map") or {})
        if not isinstance(self.namespaces, dict):
            raise PolicyError("namespaces must be an object")
        for prefix, uri in self.namespaces.items():
            if not isinstance(prefix, str) or not isinstance(uri, str):
                raise PolicyError("namespaces must map strings to strings")

        default_namespace = data.get("default_namespace")
        if default_namespace is None and "" in self.namespaces:
            default_namespace = self.namespaces[""]
        if default_namespace is not None and not isinstance(default_namespace, str):
            raise PolicyError("default_namespace must be a string")
        self.default_namespace = default_namespace

        paths = data.get("paths", data.get("allowed_paths", data.get("anonymous_paths")))
        self.paths = []
        self.path_mappings = {}
        for path, path_mapping in _normalise_path_specs(paths, "paths"):
            if path not in self.paths:
                self.paths.append(path)
            if path_mapping:
                _add_mapping(self.path_mappings.setdefault(path, {}), path_mapping, "paths")

        self.mapping = _parse_mapping(data.get("mapping", data.get("identity_mapping", {})), "mapping")
        for path_mapping in self.path_mappings.values():
            _add_mapping(self.mapping, path_mapping, "paths and mapping")

        relations = data.get("relations", data.get("groups", [])) or []
        if isinstance(relations, dict):
            relations = [relations]
        if not isinstance(relations, list):
            raise PolicyError("relations must be a list")
        self.relations = []
        for index, relation in enumerate(relations):
            if not isinstance(relation, dict):
                raise PolicyError("relation %d must be an object" % index)
            relation_paths = relation.get("paths", relation.get("allowed_paths"))
            entries = _normalise_path_specs(relation_paths, "relation %d paths" % index)
            if not entries:
                raise PolicyError("relation %d must contain at least one path" % index)
            relation_mapping = _parse_mapping(
                relation.get("mapping", relation.get("map")),
                "relation %d mapping" % index,
            )
            for path, path_mapping in entries:
                _add_mapping(relation_mapping, path_mapping, "relation %d mapping" % index)
            self.relations.append({"paths": [path for path, _ in entries], "mapping": relation_mapping})

        self.prefix = data.get("prefix", DEFAULT_PREFIX)
        self.salt = data.get("salt", DEFAULT_SALT)
        self.digest_length = data.get("digest_length", DEFAULT_DIGEST_LENGTH)
        self.scheme = data.get("scheme", "sha256")
        if not isinstance(self.prefix, str):
            raise PolicyError("prefix must be a string")
        if not isinstance(self.salt, str):
            raise PolicyError("salt must be a string")
        if not isinstance(self.digest_length, int) or isinstance(self.digest_length, bool) or self.digest_length < 1:
            raise PolicyError("digest_length must be a positive integer")
        if self.scheme not in ("sha256", "counter"):
            raise PolicyError("unsupported pseudonym scheme: %s" % self.scheme)

        skip_values = data.get("skip_values", DEFAULT_SKIP_VALUES)
        if isinstance(skip_values, str):
            skip_values = [skip_values]
        if not isinstance(skip_values, (list, tuple)) or not all(isinstance(value, str) for value in skip_values):
            raise PolicyError("skip_values must be a list of strings")
        self.skip_values = tuple(skip_values)

        if not self.paths and not self.relations:
            raise PolicyError("policy must define at least one path")
        self._validate()

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as stream:
            return cls(json.load(stream))

    def _validate(self):
        for path in self.paths:
            compile_path(path, self.namespaces)
        relation_replacements = {}
        for relation in self.relations:
            for path in relation["paths"]:
                compile_path(path, self.namespaces)
            _add_mapping(relation_replacements, relation["mapping"], "relations")
        # Relation mappings are folded into the global value mapping. The
        # same identifier must never receive two different pseudonyms just
        # because it appears in two different related fields or events.
        _add_mapping(relation_replacements, self.mapping, "relations and mapping")
        self.mapping = relation_replacements

    def all_paths(self):
        seen = []
        for path in self.paths:
            if path not in seen:
                seen.append(path)
        for relation in self.relations:
            for path in relation["paths"]:
                if path not in seen:
                    seen.append(path)
        return seen

    def compile(self, root_namespace=None):
        default_namespace = self.default_namespace
        if default_namespace is None:
            default_namespace = root_namespace or ""
        return [compile_path(path, self.namespaces, default_namespace) for path in self.all_paths()]

    def pseudonym(self, value):
        digest = hashlib.sha256((self.salt + "\x00" + value).encode("utf-8")).hexdigest()
        return self.prefix + digest[: self.digest_length]


class Anonymizer(object):
    """Apply an :class:`AnonymizationPolicy` to event XML."""

    def __init__(self, policy):
        if isinstance(policy, Anonymizer):
            self.policy = policy.policy
        elif isinstance(policy, AnonymizationPolicy):
            self.policy = policy
        elif isinstance(policy, (dict, list, tuple)):
            self.policy = AnonymizationPolicy(policy)
        elif isinstance(policy, str):
            self.policy = AnonymizationPolicy.from_file(policy)
        else:
            raise PolicyError("unsupported policy value")
        self._counter_by_value = {}
        self.replacements = dict(self.policy.mapping)

    def replacement(self, value):
        """Return the stable pseudonym for ``value``.

        Surrounding whitespace is preserved. Explicit mappings win;
        configured sentinels are left alone; everything else receives a
        deterministic pseudonym. The same value always maps to the same
        replacement within this anonymizer, across events and paths.
        """
        leading = value[: len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()) :]
        core = value.strip()
        if core in self.policy.mapping:
            replacement = self.policy.mapping[core]
        elif core in self.policy.skip_values:
            replacement = core
        elif self.policy.scheme == "counter":
            counter = self._counter_by_value.get(core)
            if counter is None:
                counter = len(self._counter_by_value) + 1
                self._counter_by_value[core] = counter
            replacement = self.policy.prefix + "%04d" % counter
        else:
            replacement = self.policy.pseudonym(core)
        self.replacements[core] = replacement
        return leading + replacement + trailing

    def anonymize_element(self, root):
        """Mutate ``root`` in place and return it.

        Each XML node is processed at most once even when several allowed
        paths overlap. Overlapping policies therefore cannot replace a
        pseudonym a second time (and cannot alter the structure either).
        """
        seen = set()
        for path in self.policy.compile(_namespace(root.tag)):
            for element, attribute in path.select(root):
                key = (element, attribute)
                if key in seen:
                    continue
                seen.add(key)
                if attribute is not None:
                    old_value = element.get(attribute)
                else:
                    if len(element):
                        raise StructureError(
                            "path %r selects an element with child elements; "
                            "its text cannot be anonymized safely" % path.original
                        )
                    old_value = element.text
                if old_value is None:
                    continue
                new_value = self.replacement(old_value)
                if new_value == old_value:
                    continue
                if attribute is not None:
                    element.set(attribute, new_value)
                else:
                    element.text = new_value
        return root

    def anonymize_xml(self, xml):
        """Return anonymized XML without an XML declaration."""
        root = parse_xml(xml)
        self.anonymize_element(root)
        return to_xml(root)

    def anonymize_record(self, record):
        """Anonymize an :class:`Evtx.Evtx.Record` and return XML."""
        return self.anonymize_xml(record.xml())


def anonymize_xml(xml, policy):
    """Convenience wrapper around :class:`Anonymizer`."""
    return Anonymizer(policy).anonymize_xml(xml)


def anonymize_records(records, policy):
    """Yield anonymized XML strings for an iterable of records."""
    anonymizer = policy if isinstance(policy, Anonymizer) else Anonymizer(policy)
    for record in records:
        yield anonymizer.anonymize_record(record)


def anonymize_file(filename, policy):
    """Yield anonymized XML strings from an EVTX file."""
    import Evtx.Evtx as evtx

    with evtx.Evtx(filename) as log:
        for xml in anonymize_records(log.records(), policy):
            yield xml


def structure_signature(element):
    """Return a value-independent signature of an XML tree."""
    attributes = tuple(sorted(etree.QName(key).localname for key in element.attrib))
    children = tuple(structure_signature(child) for child in element if isinstance(child.tag, str))
    return (
        element.tag,
        attributes,
        element.text is not None and element.text != "",
        len(children),
        children,
    )


def assert_structure_preserved(original, anonymized):
    """Raise :class:`StructureError` if tags, attributes or shape changed."""
    if structure_signature(original) != structure_signature(anonymized):
        raise StructureError("anonymization changed the XML structure")
    return True


def _attribute_json_name(element, key):
    qname = etree.QName(key)
    if not qname.namespace:
        return qname.localname
    for prefix, uri in (element.nsmap or {}).items():
        if uri == qname.namespace:
            return ("%s:%s" % (prefix, qname.localname)) if prefix else qname.localname
    return qname.localname


def _json_key(element):
    qname = etree.QName(element.tag)
    if element.prefix:
        return "%s:%s" % (element.prefix, qname.localname)
    return qname.localname


def _namespace_declarations(element):
    return {
        ("@xmlns" if prefix is None else "@xmlns:%s" % prefix): uri for prefix, uri in (element.nsmap or {}).items()
    }


def xml_to_dict(element, _inherited_nsmap=None):
    """Convert an XML element to a JSON-friendly dict.

    Follows the conventions of the existing ``evtx_dump_json`` script:
    attributes are prefixed with ``@``, repeated sibling names become lists,
    and text lives under ``#text`` when attributes or children are present.
    Unlike a flat projection, the full element hierarchy is retained.
    """
    inherited = _inherited_nsmap or {}
    declarations = {key: uri for key, uri in _namespace_declarations(element).items() if inherited.get(key) != uri}
    attributes = dict(declarations)
    for key, value in element.attrib.items():
        attributes["@%s" % _attribute_json_name(element, key)] = value

    children = {}
    for child in element:
        if not isinstance(child.tag, str):
            continue
        key = _json_key(child)
        value = xml_to_dict(child, _namespace_declarations(element))
        if key in children:
            if not isinstance(children[key], list):
                children[key] = [children[key]]
            children[key].append(value)
        else:
            children[key] = value

    text = element.text
    if children:
        result = dict(children)
        result.update(attributes)
        if text is not None and text.strip():
            result[_TEXT] = text
        return result
    if attributes:
        result = dict(attributes)
        if text is not None:
            result[_TEXT] = text
        return result
    return text


def xml_to_json(xml):
    """Return a JSON string for one anonymized event XML document."""
    root = parse_xml(xml)
    return json.dumps(xml_to_dict(root), ensure_ascii=False)


def events_to_json(records, policy, indent=None):
    """Return a JSON array string for anonymized events."""
    anonymizer = policy if isinstance(policy, Anonymizer) else Anonymizer(policy)
    events = []
    for record in records:
        root = parse_xml(anonymizer.anonymize_record(record))
        events.append(xml_to_dict(root))
    return json.dumps(events, ensure_ascii=False, indent=indent)
