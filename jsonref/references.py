"""
Data structures describing how JSON references were resolved.

A :class:`ReferenceGraph` records the document resources that were visited
while replacing references and the reference occurrences that connect them.
Resources are keyed by both the URI they were retrieved from and the base URI
that is in effect for them (which may be changed by ``$id``/``id``).
"""

from collections import namedtuple
from collections.abc import MutableMapping
from urllib import parse as urlparse


def normalize_uri(uri):
    return urlparse.urlsplit(uri).geturl()


class DocumentKey(namedtuple("DocumentKey", "retrieval_uri base_uri")):
    """
    Identity of a document resource.

    ``retrieval_uri`` is where the document was loaded from, while ``base_uri``
    is the URI that references inside it are resolved against.  Keeping both
    values prevents two resources with different bases from sharing a cache
    entry, and prevents a document from being loaded again when its declared
    ``$id`` differs from its retrieval URI.
    """

    __slots__ = ()

    def __new__(cls, retrieval_uri, base_uri):
        if isinstance(retrieval_uri, DocumentKey):
            return retrieval_uri
        retrieval_uri = urlparse.urldefrag(retrieval_uri)[0]
        base_uri = urlparse.urldefrag(base_uri)[0]
        return super(DocumentKey, cls).__new__(
            cls, normalize_uri(retrieval_uri), normalize_uri(base_uri)
        )

    def to_dict(self):
        return [self.retrieval_uri, self.base_uri]


class DocumentStore(MutableMapping):
    """
    Cache of parsed documents, keyed by :class:`DocumentKey`.

    Values are stored under the full ``(retrieval_uri, base_uri)`` key.  String
    keys are aliases by resource URI and resolve to the canonical entry, so a
    document can be looked up by either the URI it was retrieved from or the
    ``$id`` it declares.  The base URI is never removed from the canonical key.
    """

    def __init__(self):
        self.store = {}
        self.aliases = {}

    def _as_key(self, key):
        if isinstance(key, DocumentKey):
            return key
        if isinstance(key, str):
            return self.aliases.get(normalize_uri(key))
        return DocumentKey(*key)

    def canonical_key(self, key):
        key = self._as_key(key)
        if key is None:
            raise KeyError(key)
        return self.aliases.get(key, key)

    def __getitem__(self, key):
        return self.store[self.canonical_key(key)]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, value):
        key = key if isinstance(key, DocumentKey) else DocumentKey(*key)
        self.store[key] = value
        self.aliases[key] = key

    def register(self, retrieval_uri, base_uri, value, aliases=(), primary=True):
        key = DocumentKey(retrieval_uri, base_uri)
        self.store[key] = value
        self.aliases[key] = key
        for alias in aliases:
            # The declared base is only an alias.  If several separately
            # retrieved documents declare the same base, keep the first one
            # rather than silently repointing existing references.
            self.aliases.setdefault(normalize_uri(alias), key)
        if primary:
            # Only the resource actually retrieved from a URI owns that URI as
            # an alias.  Nested resources which merely declare an id inside it
            # must not repoint the document's retrieval URI.
            self.aliases.setdefault(key.retrieval_uri, key)
        return key

    def register_alias(self, alias, key):
        self.aliases[normalize_uri(alias)] = self.canonical_key(key)

    def keys_for(self, key):
        """Return every alias which resolves to the canonical ``key``."""
        canonical = self.canonical_key(key)
        return {
            alias
            for alias, target in self.aliases.items()
            if target == canonical
        }

    def __delitem__(self, key):
        key = self.canonical_key(key)
        del self.store[key]
        self.aliases = {
            alias: canonical
            for alias, canonical in self.aliases.items()
            if canonical != key
        }

    def __iter__(self):
        return iter(self.store)

    def __len__(self):
        return len(self.store)

    def __contains__(self, key):
        try:
            key = self._as_key(key)
        except (TypeError, ValueError):
            return False
        return key is not None and self.canonical_key(key) in self.store

    def keys(self):
        return self.store.keys()

    def values(self):
        return self.store.values()

    def items(self):
        return self.store.items()

    def __repr__(self):
        return repr(self.store)


class Document(object):
    """A document resource that was visited while resolving references."""

    def __init__(
        self,
        key,
        declared_id=None,
        container=None,
        retrieved_from=None,
        via=None,
    ):
        self.key = key
        self.retrieval_uri = key.retrieval_uri
        self.base_uri = key.base_uri
        self.declared_id = declared_id
        self.container = container
        self.object = None
        self.references = []
        self.incoming = []

        # ``parents`` records every reference path which reached this document.
        # ``parent``/``parent_reference`` are the first of those, kept for
        # convenient single-path inspection.
        self.parents = []
        self.parent = None
        self.parent_reference = None
        if retrieved_from is not None or via is not None:
            self.add_parent(retrieved_from, via)

    @property
    def id(self):
        """The effective id of this resource, or ``None`` if it has no id."""
        return self.base_uri if self.declared_id is not None else None

    @property
    def uri(self):
        return self.base_uri

    def add_parent(self, document, reference):
        pair = (document, reference)
        if pair not in self.parents:
            self.parents.append(pair)
        if self.parent is None:
            self.parent = document
        if self.parent_reference is None:
            self.parent_reference = reference

    def __repr__(self):
        return "<Document %r>" % (self.key,)


class Reference(object):
    """
    One ``$ref`` occurrence.

    ``source`` is the document resource containing the reference and ``target``
    is the document resource its resolved URI points at.  The same target can be
    shared by many :class:`Reference` objects, one for each call path.
    """

    def __init__(
        self,
        reference,
        source,
        path,
        full_uri,
        parent=None,
        target=None,
        resolved=False,
        subject=None,
    ):
        self.reference = reference
        self.source = source
        self.path = tuple(path)
        self.full_uri = full_uri
        uri, fragment = urlparse.urldefrag(full_uri)
        self.uri = uri
        self.pointer = fragment
        self.parent = parent
        self.target = target
        self.resolved = resolved
        self.subject = subject
        self.graph = None

    @property
    def ref(self):
        return self.reference.get("$ref")

    @property
    def source_uri(self):
        return self.source.base_uri

    @property
    def target_uri(self):
        return self.full_uri

    @property
    def chain(self):
        """The first root-to-reference path followed to reach this reference."""
        chains = self.all_chains()
        return chains[0] if chains else (self,)

    def all_chains(self):
        """Every root-to-reference path for this occurrence."""
        if self.graph is None:
            return (self.chain,)
        return self.graph.chains_for(self)

    def __repr__(self):
        return "<Reference %r from %r>" % (self.ref, self.source.key)

    def to_dict(self, document_index=None, reference_index=None):
        def key_of(document):
            if document is None:
                return None
            if document_index is not None:
                return document_index[document.key]
            return document.key.to_dict()

        def index_of(item):
            if reference_index is not None:
                return reference_index[item]
            return None

        return {
            "source": key_of(self.source),
            "source_uri": self.source.base_uri,
            "target": key_of(self.target),
            "path": list(self.path),
            "reference": self.reference,
            "ref": self.ref,
            "uri": self.uri,
            "pointer": self.pointer,
            "full_uri": self.full_uri,
            "resolved": self.resolved,
            "chain": [index_of(item) for item in self.chain],
            "chains": [
                [index_of(item) for item in chain] for chain in self.all_chains()
            ],
        }


class ReferenceGraph(object):
    """
    A graph of documents and the reference occurrences which connect them.

    Iterating over the graph yields the :class:`Reference` occurrences in the
    order they were discovered.  ``documents`` is an ordered mapping of
    :class:`DocumentKey` to :class:`Document`.

    The graph is updated as lazy references are resolved, so a graph returned
    before every reference has been accessed may contain unresolved edges.
    """

    def __init__(self):
        self.documents = {}
        self.references = []
        self.result = None
        self.root_uri = ""

    @property
    def nodes(self):
        return self.documents

    @property
    def edges(self):
        return self.references

    @property
    def root(self):
        """The root document resource, if any document has been visited."""
        for document in self.documents.values():
            return document
        return None

    def __iter__(self):
        return iter(self.references)

    def __len__(self):
        return len(self.references)

    def document(
        self,
        retrieval_uri,
        base_uri,
        declared_id=None,
        container=None,
        retrieved_from=None,
        via=None,
    ):
        key = DocumentKey(retrieval_uri, base_uri)
        try:
            document = self.documents[key]
        except KeyError:
            document = Document(
                key,
                declared_id=declared_id,
                container=container,
                retrieved_from=retrieved_from,
                via=via,
            )
            self.documents[key] = document
        else:
            if declared_id is not None:
                document.declared_id = declared_id
            if document.container is None and container is not None:
                document.container = container
            if retrieved_from is not None or via is not None:
                document.add_parent(retrieved_from, via)
        return document

    def add_reference(self, reference, source, path, full_uri, parent=None):
        result = Reference(
            reference=reference,
            source=source,
            path=path,
            full_uri=full_uri,
            parent=parent,
        )
        result.graph = self
        source.references.append(result)
        self.references.append(result)
        return result

    def resolve_reference(self, reference, target, subject=None):
        reference.target = target
        reference.resolved = True
        reference.subject = subject
        if reference not in target.incoming:
            target.incoming.append(reference)
        if target is not reference.source:
            target.add_parent(reference.source, reference)
        return reference

    def find_document(self, key):
        if isinstance(key, DocumentKey):
            return self.documents.get(key)
        if isinstance(key, str):
            uri = normalize_uri(key)
            matches = [
                document
                for document in self.documents.values()
                if uri in (document.retrieval_uri, document.base_uri)
            ]
            return matches[0] if matches else None
        return self.documents.get(DocumentKey(*key))

    def find_documents(self, uri):
        """Return every document whose retrieval URI or base URI is ``uri``."""
        uri = normalize_uri(uri)
        return [
            document
            for document in self.documents.values()
            if uri in (document.retrieval_uri, document.base_uri)
        ]

    def reference_at(self, path, source_uri=None):
        """Return references declared at ``path`` (within their resource)."""
        path = tuple(path)
        return [
            reference
            for reference in self.references
            if reference.path == path
            and (source_uri is None or reference.source.base_uri == source_uri)
        ]

    def references_to(self, uri):
        """Return every reference occurrence whose resolved URI is ``uri``."""
        uri = normalize_uri(uri)
        return [
            reference
            for reference in self.references
            if normalize_uri(reference.uri) == uri
        ]

    def paths_to(self, document):
        """
        Return all reference chains that reach ``document``.

        Each chain is a tuple of :class:`Reference` occurrences starting at the
        root document.  The root document itself is reached by an empty chain.
        Multiple chains are returned when a shared resource is reached through
        more than one call path.
        """
        if isinstance(document, (str, DocumentKey)):
            document = self.find_document(document)
        if document is None:
            return ()
        return self._paths_to(document, set())

    def _paths_to(self, document, seen):
        if document.key in seen:
            return ()
        seen = seen | {document.key}

        if not document.parents:
            if document.container is not None and document.container is not document:
                return self._paths_to(document.container, seen)
            return ((),)

        chains = []
        for parent, reference in document.parents:
            if parent is None or parent is document:
                chain = ()
                if chain not in chains:
                    chains.append(chain)
                continue
            for prefix in self._paths_to(parent, seen):
                chain = prefix + (reference,) if reference is not None else prefix
                if chain not in chains:
                    chains.append(chain)
        return tuple(chains)

    def chains_for(self, reference):
        """Return every root-to-reference chain for a reference occurrence."""
        return tuple(
            prefix + (reference,) for prefix in self.paths_to(reference.source)
        )

    def to_dict(self):
        documents = list(self.documents.values())
        document_index = {
            document.key: index for index, document in enumerate(documents)
        }
        reference_index = {
            reference: index for index, reference in enumerate(self.references)
        }
        nodes = []
        for index, document in enumerate(documents):
            nodes.append(
                {
                    "index": index,
                    "key": document.key.to_dict(),
                    "retrieval_uri": document.retrieval_uri,
                    "base_uri": document.base_uri,
                    "id": document.id,
                    "declared_id": document.declared_id,
                    "container": (
                        document_index[document.container.key]
                        if document.container is not None
                        else None
                    ),
                    "parents": [
                        {
                            "document": (
                                document_index[parent.key]
                                if parent is not None
                                else None
                            ),
                            "reference": (
                                reference_index[reference]
                                if reference is not None
                                else None
                            ),
                        }
                        for parent, reference in document.parents
                    ],
                    "incoming": [
                        reference_index[reference]
                        for reference in document.incoming
                    ],
                    "paths": [
                        [reference_index[reference] for reference in chain]
                        for chain in self.paths_to(document)
                    ],
                    "references": [
                        reference_index[reference]
                        for reference in document.references
                    ],
                }
            )
        edges = [
            reference.to_dict(document_index, reference_index)
            for reference in self.references
        ]
        return {"nodes": nodes, "edges": edges}

    def to_json(self, **kwargs):
        import json

        return json.dumps(self.to_dict(), **kwargs)

    def to_mermaid(self):
        """Render the graph as a Mermaid flowchart."""
        documents = list(self.documents.values())
        document_index = {
            document.key: index for index, document in enumerate(documents)
        }
        lines = ["flowchart LR"]
        for index, document in enumerate(documents):
            label = document.base_uri or document.retrieval_uri or "root"
            label = label.replace('"', '\\"')
            if document.retrieval_uri != document.base_uri:
                label += "<br/>retrieved from " + document.retrieval_uri.replace(
                    '"', '\\"'
                )
            lines.append('    document_{}["{}"]'.format(index, label))
        for reference in self.references:
            if reference.target is None:
                continue
            source = document_index[reference.source.key]
            target = document_index[reference.target.key]
            label = reference.ref or ""
            label = label.replace('"', '\\"')
            lines.append(
                '    document_{} -->|"{}"| document_{}'.format(source, label, target)
            )
        return "\n".join(lines)

    def __repr__(self):
        return "<ReferenceGraph: %d documents, %d references>" % (
            len(self.documents),
            len(self.references),
        )
