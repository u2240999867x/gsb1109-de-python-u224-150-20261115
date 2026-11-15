"""
Export a minimal, self-contained material package of a selected JSON subtree
and everything it depends on through JSON references.

The output preserves reference objects (``{"$ref": ...}``) instead of inlining
them, so recursive and shared references keep their original shape. Internal
references are rewritten to the paths of the new package. References that cannot
be satisfied are rewritten to their fully resolved URIs and reported, together
with the full dependency chain leading to them, rather than silently producing
a package that only looks self-contained.
"""

import json
import os
import posixpath
from collections.abc import Mapping, Sequence
from urllib import parse as urlparse
from urllib.parse import unquote


class ExtractionError(ValueError):
    """Raised when the requested subtree cannot be located in its document."""


def _unavailable_loader(uri):
    raise ExtractionError("No loader configured for external document: %r" % uri)


def _normalize_uri(uri):
    return urlparse.urlsplit(uri).geturl()


def _split_ref(base_uri, ref_string):
    full_uri = urlparse.urljoin(base_uri, ref_string)
    doc_uri, fragment = urlparse.urldefrag(full_uri)
    return _normalize_uri(doc_uri), fragment, full_uri


def _is_ref(node):
    return isinstance(node, Mapping) and isinstance(node.get("$ref"), str)


def _normalize_pointer(pointer):
    if pointer is None or pointer == "":
        return ""
    if pointer.startswith("#"):
        pointer = pointer[1:]
    return pointer


def _pointer_parts(pointer):
    pointer = _normalize_pointer(pointer)
    if not pointer:
        return []
    return unquote(pointer.lstrip("/")).split("/")


def _resolve_pointer(document, pointer):
    """Resolve a JSON pointer against a raw document, mirroring JsonRef."""
    node = document
    for part in _pointer_parts(pointer):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Sequence) and not isinstance(node, str):
            try:
                part = int(part)
            except ValueError:
                pass
        try:
            node = node[part]
        except (TypeError, LookupError):
            raise ExtractionError("Unresolvable JSON pointer: %r" % pointer)
    return node


def _effective_base(node, base_uri, jsonschema):
    if jsonschema and isinstance(node, Mapping):
        id_ = node.get("$id") or node.get("id")
        if isinstance(id_, str):
            return urlparse.urljoin(base_uri, id_)
    return base_uri


class ExtractionResult:
    """
    The result of :func:`extract_subtree`.

    :attr documents: Mapping of package-relative file paths to the raw JSON
        data they contain.
    :attr root_document: Package-relative path of the document holding the
        selected subtree.
    :attr root_pointer: JSON pointer (including the leading ``#``) of the
        selected subtree inside the root document.
    :attr missing: List of records describing references that could not be
        satisfied, each including the full chain of references leading to it.
    """

    def __init__(self, documents, root_document, root_pointer, source_uris, missing):
        self.documents = documents
        self.root_document = root_document
        self.root_pointer = root_pointer
        self.source_uris = source_uris
        self.missing = missing

    @property
    def self_contained(self):
        return not self.missing

    def to_manifest(self):
        return {
            "self_contained": self.self_contained,
            "root": {
                "document": self.root_document,
                "pointer": self.root_pointer or "#",
            },
            "documents": dict(self.source_uris),
            "unresolved": self.missing,
        }

    def write_to(self, directory):
        """
        Write the package, including a ``manifest.json``, into `directory`
        (created if necessary) and return the manifest as a dictionary.
        """
        os.makedirs(directory, exist_ok=True)
        for relative_path, data in self.documents.items():
            destination = os.path.join(directory, *relative_path.split("/"))
            os.makedirs(os.path.dirname(destination) or directory, exist_ok=True)
            with open(destination, "w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=2)
                fp.write("\n")
        manifest = self.to_manifest()
        manifest_path = os.path.join(directory, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2)
            fp.write("\n")
        return manifest


def extract_subtree(document, pointer, *, loader=None, base_uri="", jsonschema=False):
    """
    Build a minimal local material package for the JSON subtree at `pointer`
    within `document`, including every document and node the subtree depends
    on through JSON references.

    Reference objects are kept intact (recursive and shared relationships are
    preserved, never inlined), and rewritten to point at the new package paths.
    Nodes that nothing depends on are pruned, including sibling data.

    :param document: Parsed JSON document containing the selected subtree.
    :param pointer: JSON pointer selecting the subtree (``""`` or ``"#"`` for
        the whole document). Both ``"/a/b"`` and ``"#/a/b"`` are accepted.
    :param loader: Callable taking a document URI and returning its parsed
        JSON. Documents that fail to load are recorded as missing instead of
        aborting the export.
    :param base_uri: URI of `document`, used to resolve relative references.
    :param jsonschema: When ``True``, ``$id``/``id`` keywords update the base
        URI for contained references, matching JSON Schema mode.
    :returns: An :class:`ExtractionResult`.
    """
    if loader is None:
        loader = _unavailable_loader

    root_uri = _normalize_uri(urlparse.urldefrag(base_uri)[0])
    pointer = _normalize_pointer(pointer)

    # Fail fast if the selected subtree does not even exist in its own document.
    _resolve_pointer(document, pointer)

    docs = {root_uri: document}
    failed = {}
    marked = {root_uri: set()}

    # Work items are entry points: (document uri, fragment, reference chain).
    # Only nodes reachable from a processed entry point are marked, so unrelated
    # sibling data never triggers loads or ends up in the package.
    pending_entries = [(root_uri, pointer, [])]
    seen_entries = set()
    missing = []

    def mark_reachable(uri, fragment, entry_node):
        marker = marked[uri]

        # Mark the ancestor chain so the output keeps the original document
        # skeleton and internal JSON pointers stay valid without remapping.
        # Arrays on the chain are kept whole: dropping an earlier element would
        # shift indices and invalidate the pointer.
        node = docs[uri]
        marker.add(id(node))
        ancestor_arrays = []
        for part in _pointer_parts(fragment):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, Sequence) and not isinstance(node, str):
                try:
                    part = int(part)
                except ValueError:
                    pass
            node = node[part]
            marker.add(id(node))
            if isinstance(node, Sequence) and not isinstance(node, str):
                ancestor_arrays.append(node)

        # Then mark every descendant of the entry point (and of whole ancestor
        # arrays). A separate traversal set is needed because ancestor-chain
        # nodes were marked above but still have to be descended into.
        stack = [entry_node] + ancestor_arrays
        traversed = set()
        while stack:
            current = stack.pop()
            identity = id(current)
            if identity in traversed:
                continue
            traversed.add(identity)
            marker.add(identity)
            if isinstance(current, Mapping):
                stack.extend(current.values())
            elif isinstance(current, Sequence) and not isinstance(current, str):
                stack.extend(current)

    while pending_entries:
        uri, fragment, chain = pending_entries.pop()
        entry_key = (uri, fragment)
        if entry_key in seen_entries:
            continue
        seen_entries.add(entry_key)

        try:
            entry_node = _resolve_pointer(docs[uri], fragment)
        except ExtractionError as error:
            missing.append(
                {
                    "uri": urlparse.urljoin(uri, "#" + fragment),
                    "reason": str(error),
                    "chain": list(chain),
                }
            )
            continue

        mark_reachable(uri, fragment, entry_node)

        for ref_path, ref_string, ref_base in _iter_refs(
            docs[uri], uri, jsonschema, marker=marked[uri]
        ):
            target_uri, target_fragment, _full = _split_ref(ref_base, ref_string)
            hop = {
                "document": uri,
                "pointer": _path_to_pointer(ref_path),
                "ref": ref_string,
            }
            new_chain = chain + [hop]
            if target_uri not in docs and target_uri not in failed:
                try:
                    docs[target_uri] = loader(target_uri)
                    marked[target_uri] = set()
                except Exception as error:
                    failed[target_uri] = {
                        "uri": target_uri,
                        "reason": "%s: %s" % (error.__class__.__name__, error),
                        "chain": new_chain,
                    }
            if target_uri in failed:
                continue
            pending_entries.append((target_uri, target_fragment, new_chain))

    missing = list(failed.values()) + missing

    output_names = _plan_output_names(docs, root_uri)

    rewritten = {}
    for uri, doc in docs.items():
        if not marked[uri]:
            continue
        rewritten[output_names[uri]] = _rewrite_document(
            doc=doc,
            doc_uri=uri,
            marker=marked[uri],
            docs=docs,
            failed=failed,
            output_names=output_names,
            jsonschema=jsonschema,
            memo={},
        )

    source_uris = {
        uri: name for uri, name in output_names.items() if name in rewritten
    }

    root_pointer = "#" + pointer if pointer else "#"
    return ExtractionResult(
        documents=rewritten,
        root_document=output_names[root_uri],
        root_pointer=root_pointer,
        source_uris=source_uris,
        missing=missing,
    )


def _path_to_pointer(path):
    if not path:
        return "#"
    escaped = [
        str(part).replace("~", "~0").replace("/", "~1") for part in path
    ]
    return "#/" + "/".join(escaped)


def _iter_refs(node, base_uri, jsonschema, marker, path=()):
    """Yield (path, ref string, effective base uri) for refs under `node`."""
    if id(node) not in marker:
        return
    base_uri = _effective_base(node, base_uri, jsonschema)
    if _is_ref(node):
        yield path, node["$ref"], base_uri
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _iter_refs(
                value, base_uri, jsonschema, marker, path + (key,)
            )
    elif isinstance(node, Sequence) and not isinstance(node, str):
        for index, value in enumerate(node):
            yield from _iter_refs(
                value, base_uri, jsonschema, marker, path + (index,)
            )


def _plan_output_names(docs, root_uri):
    """
    Assign flat, package-local output names. Flattening keeps every file inside
    the package directory (no ``..`` escapes) while still reflecting the source
    document's basename; collisions get a numeric suffix.
    """
    used = set()
    names = {}

    def claim(uri, preferred):
        candidate = preferred
        stem, ext = os.path.splitext(preferred)
        counter = 1
        while candidate in used:
            candidate = "%s-%d%s" % (stem, counter, ext)
            counter += 1
        used.add(candidate)
        names[uri] = candidate

    claim(root_uri, "root.json")
    for uri in docs:
        if uri == root_uri:
            continue
        basename = posixpath.basename(urlparse.urlsplit(uri).path)
        if not basename:
            basename = "document-%d.json" % len(used)
        if not basename.endswith(".json"):
            basename += ".json"
        claim(uri, basename)
    return names


def _rewrite_document(
    *, doc, doc_uri, marker, docs, failed, output_names, jsonschema, memo
):
    def copy_node(node, base_uri):
        if id(node) not in marker:
            return None
        if id(node) in memo:
            return memo[id(node)]
        base_uri = _effective_base(node, base_uri, jsonschema)
        if _is_ref(node):
            new_node = dict(node)
            new_node["$ref"] = _rewrite_ref(
                ref_string=node["$ref"],
                source_uri=doc_uri,
                source_base=base_uri,
                docs=docs,
                failed=failed,
                output_names=output_names,
            )
            memo[id(node)] = new_node
            children = {key: value for key, value in node.items() if key != "$ref"}
        elif isinstance(node, Mapping):
            new_node = {}
            memo[id(node)] = new_node
            children = node
        elif isinstance(node, Sequence) and not isinstance(node, str):
            new_node = []
            memo[id(node)] = new_node
            children = {index: value for index, value in enumerate(node)}
        else:
            return node

        if isinstance(new_node, list):
            for value in children.values():
                copied = copy_node(value, base_uri)
                if copied is not None:
                    new_node.append(copied)
        else:
            for key, value in children.items():
                copied = copy_node(value, base_uri)
                if copied is not None:
                    new_node[key] = copied
        return new_node

    return copy_node(doc, doc_uri)


def _rewrite_ref(*, ref_string, source_uri, source_base, docs, failed, output_names):
    target_uri, fragment, full_uri = _split_ref(source_base, ref_string)
    if target_uri in failed or target_uri not in docs:
        return full_uri
    try:
        _resolve_pointer(docs[target_uri], fragment)
    except ExtractionError:
        return full_uri

    target_name = output_names[target_uri]
    if target_uri == source_uri:
        return "#" + fragment if fragment else "#"

    source_name = output_names[source_uri]
    relative = posixpath.relpath(
        target_name, start=posixpath.dirname(source_name)
    )
    return relative + ("#" + fragment if fragment else "")
