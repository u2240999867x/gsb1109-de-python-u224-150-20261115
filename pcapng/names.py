"""
Address dimension tables built from Name Resolution Blocks (NRBs).

The pcap-ng format gives Name Resolution Blocks no timestamp.  They may appear
at the beginning of a section, but they may also be appended later by tools
that post-process a capture.  A block written after the packets is still
useful as a display alias, but it cannot prove which name was in use when a
packet was captured.

This module keeps that distinction explicit:

* every declaration records the NRB it came from (``source``), the section it
  belongs to, and the block position at which it starts to apply
  (``applies_from``);
* a declaration's applicability ends at the end of the section or immediately
  before a later declaration of the same address in the same section
  (``applies_until``, inclusive);
* packet timestamps bounding the applicability window are exposed separately
  as ``applies_from_time`` / ``applies_until_time``; they are ``None`` when no
  packet exists in that part of the section;
* declarations are never overwritten by later blocks or by other sections;
  an address may have any number of names and all of them are retained;
* unknown record types keep their raw payload and can be written back out.

The main entry points are :py:func:`build_address_table`,
:py:attr:`pcapng.blocks.SectionHeader.address_table` and
:py:class:`AddressTable`.
"""

from collections import OrderedDict
from collections.abc import Mapping

# Packet block types: Packet (obsolete), Simple Packet, Enhanced Packet.
_PACKET_BLOCK_TYPES = (0x00000002, 0x00000003, 0x00000006)
_NRB_BLOCK_TYPE = 0x00000004


def _ordered_unique(values):
    """Return ``values`` without duplicates, preserving their order."""
    result = []
    seen_hashable = set()
    seen_identity = set()
    for value in values:
        try:
            hash(value)
        except TypeError:
            if id(value) in seen_identity:
                continue
            seen_identity.add(id(value))
        else:
            if value in seen_hashable:
                continue
            seen_hashable.add(value)
        result.append(value)
    return result


def _get(record, key, default=None):
    if hasattr(record, "get"):
        return record.get(key, default)
    try:
        return record[key]
    except (KeyError, TypeError):
        return default


def _record_type(record):
    return _get(record, "type")


def _record_address(record):
    return _get(record, "address")


def _record_names(record):
    names = _get(record, "names")
    if names is None:
        return []
    return list(names)


def _is_packet(block):
    return getattr(block, "magic_number", None) in _PACKET_BLOCK_TYPES


def _block_timestamp(block):
    try:
        value = block.timestamp
    except (AttributeError, KeyError, TypeError, ZeroDivisionError):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _section_blocks(section):
    if section is None:
        return ()
    return tuple(getattr(section, "blocks", ()) or ())


def _section_end_index(section):
    blocks = _section_blocks(section)
    return len(blocks) - 1 if blocks else None


def _ensure_section_index(section, fallback=0):
    """
    Give an in-memory section a stable index.

    Sections read from a file already have their file position.  Sections
    built by hand do not, so they are numbered by first appearance; this keeps
    declarations from different sections distinguishable.
    """
    if section is None:
        return None
    if getattr(section, "section_index", None) is None:
        try:
            object.__setattr__(section, "section_index", fallback)
        except AttributeError:
            return getattr(section, "section_index", None)
    return getattr(section, "section_index", None)


def _packet_indexes(section, start=0, end=None):
    """Return packet block indexes within ``[start, end]`` (inclusive)."""
    indexes = []
    for index, block in enumerate(_section_blocks(section)):
        if end is not None and index > end:
            break
        if index < start:
            continue
        if _is_packet(block):
            indexes.append(index)
    return indexes


def _packet_time_range(section, start=0, end=None):
    """Return ``(first, last)`` packet timestamps within the index window."""
    timestamps = []
    for index in _packet_indexes(section, start=start, end=end):
        timestamp = _block_timestamp(_section_blocks(section)[index])
        if timestamp is not None:
            timestamps.append(timestamp)
    if not timestamps:
        return None, None
    return timestamps[0], timestamps[-1]


class NameDeclaration(Mapping):
    """
    One name-resolution record together with its provenance.

    :param address:
        The numeric address the record applies to, or ``None`` for a record
        whose type is not IPv4 or IPv6.
    :param names:
        All names carried by the record.
    :param record_type:
        The pcap-ng record type (``1`` for IPv4, ``2`` for IPv6, anything
        else for an unknown record).
    :param source:
        The :py:class:`pcapng.blocks.NameResolution` block that declared this
        record.
    :param record_index:
        Position of the record inside its source block.
    :param applies_from:
        Block index at which the declaration starts to apply in its section.
        NRBs carry no timestamp, so this ordering position is the explicit
        applicability time; :py:attr:`applies_from_time` translates it to a
        capture timestamp when a packet is available.
    :param applies_until:
        Last block index covered by the declaration, or ``None`` when it
        extends to the end of the section.
    :param raw:
        Original payload for records whose type is not IPv4/IPv6.
    """

    __slots__ = [
        "address",
        "names",
        "record_type",
        "source",
        "section",
        "record_index",
        "declaration_index",
        "applies_from",
        "_applies_until",
        "raw",
    ]

    def __init__(
        self,
        address,
        names,
        record_type,
        source,
        section=None,
        record_index=0,
        declaration_index=0,
        applies_from=0,
        applies_until=None,
        raw=None,
    ):
        self.address = address
        self.names = tuple(names or ())
        self.record_type = record_type
        self.source = source
        if section is None:
            section = getattr(source, "section", None)
        self.section = section
        self.record_index = record_index
        self.declaration_index = declaration_index
        self.applies_from = applies_from
        self._applies_until = applies_until
        self.raw = raw

    # ------------------------- provenance -------------------------

    @property
    def provenance(self):
        return self.source

    @property
    def source_block(self):
        return self.source

    @property
    def block(self):
        return self.source

    @property
    def block_index(self):
        return self.applies_from

    @property
    def section_index(self):
        return getattr(self.section, "section_index", None)

    @property
    def type(self):
        return self.record_type

    @property
    def is_unknown(self):
        return self.record_type not in (1, 2)

    @property
    def name(self):
        return self.names[0] if self.names else None

    # ------------------------- applicability -------------------------

    @property
    def applicable_from(self):
        return self.applies_from

    @property
    def applies_until(self):
        """
        Last block index covered by the declaration.

        This is recomputed from the current section contents, so a
        declaration made before more blocks were appended automatically
        extends over them.  ``None`` means "to the end of the section".
        """
        if self.section is None:
            return self._applies_until
        if self._applies_until is not None:
            return self._applies_until
        return _section_end_index(self.section)

    @applies_until.setter
    def applies_until(self, value):
        self._applies_until = value

    @property
    def applicable_until(self):
        return self.applies_until

    @property
    def valid_from(self):
        return self.applies_from

    @property
    def valid_until(self):
        return self.applies_until

    @property
    def declared_at(self):
        return self.applies_from

    @property
    def applicable_index(self):
        """Applicability as ``(first_block, last_block)`` indexes."""
        return (self.applies_from, self.applies_until)

    @property
    def applies_from_time(self):
        """
        Timestamp of the first packet in the applicability window, or
        ``None`` when no packet carries a usable timestamp there.
        """
        if self.section is None:
            return None
        first, _ = _packet_time_range(
            self.section, start=self.applies_from, end=self.applies_until
        )
        return first

    @property
    def applies_until_time(self):
        """
        Timestamp of the last packet in the applicability window, or
        ``None`` when no packet carries a usable timestamp there.
        """
        if self.section is None:
            return None
        _, last = _packet_time_range(
            self.section, start=self.applies_from, end=self.applies_until
        )
        return last

    @property
    def applicable_time(self):
        """
        Explicit applicability as packet timestamps.

        Returns ``(applies_from_time, applies_until_time)``.  Either item is
        ``None`` when no packet exists in that part of the window, so callers
        can distinguish "applies to all remaining packets" from "no packet
        evidence at all".
        """
        return (self.applies_from_time, self.applies_until_time)

    @property
    def applicable_time_range(self):
        return self.applicable_time

    @property
    def time_range(self):
        return self.applicable_time

    @property
    def proven(self):
        """
        Whether a captured packet exists while this declaration applies.

        A declaration appended after every packet in its section is a display
        alias only.  A declaration is never considered proven merely because
        it exists in the file.
        """
        return bool(self._proven_packets())

    def _proven_packets(self):
        if self.section is None or self.address is None:
            return []
        return _packet_indexes(
            self.section, start=self.applies_from, end=self.applies_until
        )

    @property
    def proven_packets(self):
        """Packet blocks captured while this declaration applies."""
        blocks = _section_blocks(self.section)
        return tuple(blocks[i] for i in self._proven_packets())

    @property
    def is_proven(self):
        return self.proven

    @property
    def last_packet_index(self):
        if self.section is None:
            return -1
        return getattr(self.section, "last_packet_index", -1)

    @property
    def is_display_alias(self):
        """
        Whether this declaration's names are only display aliases.

        Such a declaration can still be shown, but no packet supports a claim
        that the name was in use at capture time.  This is true for NRBs
        appended after every packet and for declarations that were superseded
        before any packet existed.
        """
        return not self.proven

    @property
    def is_superseded(self):
        """
        Whether a later declaration of the same address ended this one before
        any packet could use it.
        """
        if self.proven or self._applies_until is None:
            return False
        return True

    @property
    def kind(self):
        if self.proven:
            return "resolution"
        if self.is_superseded:
            return "superseded"
        return "display_alias"

    def applies_at(self, at=None):
        """
        Whether the declaration's positional window covers ``at``.

        Applicability alone does not prove a name for a packet: see
        :py:meth:`is_proven_at`.
        """
        if at is None:
            return True
        index, at_section = _at_position(at)
        if at_section is not None and self.section is not None:
            if at_section is not self.section:
                return False
        if index is None:
            return True
        if isinstance(index, float):
            if self.section is None:
                return False
            mapper = getattr(self.section, "block_index_for_time", None)
            index = mapper(index) if mapper is not None else None
            if index is None:
                return False
        if index < self.applies_from:
            return False
        if self.applies_until is not None and index > self.applies_until:
            return False
        return True

    def is_proven_at(self, at=None):
        """
        Whether the declaration proves the name at ``at``.

        This requires both that the declaration applies at that point and
        that a captured packet backs it up.  ``at=None`` uses the static
        :py:attr:`proven` flag.
        """
        if not self.proven:
            return False
        return self.applies_at(at) if at is not None else True

    def is_alias_at(self, at=None):
        return not self.is_proven_at(at)

    def proves_packet(self, block):
        """
        Whether this declaration proves the address's name for ``block``.

        The block must belong to the same section and must fall inside the
        declaration's applicability window.  An NRB appended after the packet
        does not prove anything about it.
        """
        if self.section is not None and getattr(block, "section", None) is not None:
            if block.section is not self.section:
                return False
        index = getattr(block, "block_index", None)
        if index is None:
            return False
        return self.is_proven_at(index)

    # ------------------------- mapping interface -------------------------

    _KEYS = (
        "address",
        "name",
        "names",
        "type",
        "record_type",
        "source",
        "source_block",
        "block",
        "section",
        "section_index",
        "block_index",
        "record_index",
        "declaration_index",
        "applies_from",
        "applicable_from",
        "valid_from",
        "declared_at",
        "applies_until",
        "applicable_until",
        "valid_until",
        "applicable_index",
        "applies_from_time",
        "applies_until_time",
        "applicable_time",
        "raw",
        "proven",
        "is_proven",
        "kind",
        "provenance",
    )

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self):
        return len(self._KEYS)

    def __repr__(self):
        return (
            "<NameDeclaration type={0!r} address={1!r} names={2!r} "
            "section={3!r} applies_from={4!r} applies_until={5!r} "
            "proven={6!r}>"
        ).format(
            self.record_type,
            self.address,
            list(self.names),
            self.section_index,
            self.applies_from,
            self.applies_until,
            self.proven,
        )


class ResolutionResult(Mapping):
    """A resolved name together with its declaration and provenance."""

    __slots__ = ["address", "name", "declaration", "at"]

    def __init__(self, address, name, declaration, at=None):
        self.address = address
        self.name = name
        self.declaration = declaration
        self.at = at

    @property
    def source(self):
        return self.declaration.source

    @property
    def section(self):
        return self.declaration.section

    @property
    def section_index(self):
        return self.declaration.section_index

    @property
    def applies_from(self):
        return self.declaration.applies_from

    @property
    def applies_until(self):
        return self.declaration.applies_until

    @property
    def applicable_time(self):
        return self.declaration.applicable_time

    @property
    def proven(self):
        return self.declaration.is_proven_at(self.at)

    @property
    def is_alias(self):
        return self.declaration.is_display_alias

    @property
    def kind(self):
        if self.proven:
            return "resolution"
        if self.declaration.is_superseded:
            return "superseded"
        return "display_alias"

    _KEYS = (
        "address",
        "name",
        "declaration",
        "source",
        "section",
        "section_index",
        "applies_from",
        "applies_until",
        "applicable_time",
        "proven",
        "is_alias",
        "kind",
    )

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self):
        return len(self._KEYS)

    def __repr__(self):
        return (
            "<ResolutionResult address={0!r} name={1!r} kind={2!r} "
            "source={3!r}>"
        ).format(self.address, self.name, self.kind, self.source)


class AddressRow(Mapping):
    """A single ``(address, name)`` row of the address dimension table."""

    __slots__ = ["declaration", "name", "reference_at"]

    def __init__(self, declaration, name, reference_at=None):
        self.declaration = declaration
        self.name = name
        self.reference_at = reference_at

    @property
    def address(self):
        return self.declaration.address

    @property
    def record_type(self):
        return self.declaration.record_type

    @property
    def source(self):
        return self.declaration.source

    @property
    def section(self):
        return self.declaration.section

    @property
    def section_index(self):
        return self.declaration.section_index

    @property
    def block_index(self):
        return self.declaration.applies_from

    @property
    def applies_from(self):
        return self.declaration.applies_from

    @property
    def applies_until(self):
        return self.declaration.applies_until

    @property
    def applies_from_time(self):
        return self.declaration.applies_from_time

    @property
    def applies_until_time(self):
        return self.declaration.applies_until_time

    @property
    def applicable_time(self):
        return self.declaration.applicable_time

    @property
    def proven(self):
        return self.declaration.is_proven_at(self.reference_at)

    @property
    def kind(self):
        if self.proven:
            return "resolution"
        if self.declaration.is_superseded:
            return "superseded"
        return "display_alias"

    def is_proven_at(self, at=None):
        return self.declaration.is_proven_at(
            self.reference_at if at is None else at
        )

    def proves_packet(self, block):
        return self.declaration.proves_packet(block)

    _KEYS = (
        "address",
        "name",
        "names",
        "type",
        "record_type",
        "source",
        "source_block",
        "section",
        "section_index",
        "block_index",
        "record_index",
        "declaration_index",
        "applies_from",
        "applicable_from",
        "valid_from",
        "declared_at",
        "applies_until",
        "applicable_until",
        "valid_until",
        "applies_from_time",
        "applies_until_time",
        "applicable_time",
        "proven",
        "is_proven",
        "kind",
    )

    def __getitem__(self, key):
        declaration = self.declaration
        if key == "names":
            return list(declaration.names)
        if key in ("type", "record_type"):
            return declaration.record_type
        if key == "source_block":
            return declaration.source
        if key == "record_index":
            return declaration.record_index
        if key == "declaration_index":
            return declaration.declaration_index
        if key in ("applicable_from", "valid_from", "declared_at"):
            return declaration.applies_from
        if key in ("applicable_until", "valid_until"):
            return declaration.applies_until
        if key == "is_proven":
            return self.proven
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self):
        return len(self._KEYS)

    def __repr__(self):
        return "<AddressRow address={0!r} name={1!r} kind={2!r}>".format(
            self.address, self.name, self.kind
        )


class AddressEntry(Mapping):
    """
    All declarations and names known for one address.

    ``names`` contains every name from every declaration, in declaration
    order.  ``proven_names`` and ``display_aliases`` split the same data by
    whether a captured packet exists while the declaration applies.
    """

    __slots__ = ["address", "declarations", "table", "reference_at"]

    def __init__(self, address, declarations, table=None, reference_at=None):
        self.address = address
        self.declarations = tuple(declarations)
        self.table = table
        if reference_at is None and table is not None:
            reference_at = getattr(table, "reference_at", None)
        self.reference_at = reference_at

    # ------------------------- declarations -------------------------

    @property
    def records(self):
        return self.declarations

    @property
    def sources(self):
        return _ordered_unique(
            declaration.source for declaration in self.declarations
        )

    @property
    def source(self):
        return self.sources[0] if self.sources else None

    @property
    def source_blocks(self):
        return self.sources

    @property
    def provenance(self):
        return self.sources

    @property
    def sections(self):
        return _ordered_unique(
            declaration.section
            for declaration in self.declarations
            if declaration.section is not None
        )

    @property
    def section(self):
        sections = self.sections
        return sections[0] if len(sections) == 1 else None

    @property
    def unknown_records(self):
        return tuple(d for d in self.declarations if d.is_unknown)

    # ------------------------- names -------------------------

    @property
    def names(self):
        """Every name from every declaration, in declaration order."""
        return _ordered_unique(
            name for declaration in self.declarations for name in declaration.names
        )

    @property
    def all_names(self):
        return self.names

    def _declarations_at(self, at=None):
        """Declarations whose applicability window covers the reference."""
        reference = self.reference_at if at is None else at
        declarations = []
        for declaration in self.declarations:
            if declaration.is_proven_at(reference):
                declarations.append(declaration)
        return declarations

    def _alias_declarations_at(self, at=None):
        """
        Declarations that are display aliases at the reference point.

        A name is an alias when it applies but has no captured packet behind
        it (for example an NRB appended to the end of a capture), or when it
        only starts at a later block.  A declaration that already ended before
        the reference point is history, not an alias.
        """
        reference = self.reference_at if at is None else at
        at_index, at_section = _at_position(reference)
        if isinstance(at_index, float):
            section = self.section
            if section is None and self.declarations:
                section = self.declarations[0].section
            mapper = getattr(section, "block_index_for_time", None)
            at_index = mapper(at_index) if mapper is not None else None

        declarations = []
        for declaration in self.declarations:
            if at_section is not None and declaration.section is not None:
                if declaration.section is not at_section:
                    continue
            if declaration.is_proven_at(reference):
                continue
            if at_index is None:
                declarations.append(declaration)
            elif (
                declaration.applies_at(reference)
                or at_index < declaration.applies_from
            ):
                declarations.append(declaration)
        return declarations

    @property
    def proven_declarations(self):
        return tuple(self._declarations_at())

    @property
    def alias_declarations(self):
        return tuple(self._alias_declarations_at())

    @property
    def proven_names(self):
        """Every name that can be proven for a captured packet."""
        return _ordered_unique(
            name
            for declaration in self.proven_declarations
            for name in declaration.names
        )

    @property
    def resolved_names(self):
        return self.proven_names

    @property
    def proven_packets(self):
        """All packets that make any declaration of this address proven."""
        packets = []
        for declaration in self.proven_declarations:
            packets.extend(declaration.proven_packets)
        return tuple(packets)

    @property
    def display_aliases(self):
        return _ordered_unique(
            name
            for declaration in self.alias_declarations
            for name in declaration.names
        )

    @property
    def display_names(self):
        return self.display_aliases

    @property
    def aliases(self):
        return self.display_aliases

    def proven_names_at(self, at=None):
        reference = self.reference_at if at is None else at
        return _ordered_unique(
            name
            for declaration in self._declarations_at(reference)
            for name in declaration.names
        )

    def resolved_names_at(self, at=None):
        return self.proven_names_at(at)

    def declarations_at(self, at=None):
        reference = self.reference_at if at is None else at
        return tuple(self._declarations_at(reference))

    def aliases_at(self, at=None):
        reference = self.reference_at if at is None else at
        return _ordered_unique(
            name
            for declaration in self._alias_declarations_at(reference)
            for name in declaration.names
        )

    def display_aliases_at(self, at=None):
        return self.aliases_at(at)

    def names_at(self, at=None, include_aliases=True):
        reference = self.reference_at if at is None else at
        names = list(self.proven_names_at(reference))
        if include_aliases:
            names.extend(self.aliases_at(reference))
        return _ordered_unique(names)

    def applicable_names_at(self, at=None):
        """
        Names whose declaration applies at ``at``, regardless of whether a
        packet exists in its window.
        """
        reference = self.reference_at if at is None else at
        return _ordered_unique(
            name
            for declaration in self.declarations
            if declaration.is_proven_at(reference)
            for name in declaration.names
        )

    # ------------------------- applicability -------------------------

    @property
    def applicable_from(self):
        values = [d.applies_from for d in self.declarations]
        return min(values) if values else None

    @property
    def applicable_until(self):
        values = [
            d.applies_until
            for d in self.declarations
            if d.applies_until is not None
        ]
        return max(values) if values else None

    @property
    def applicable_time(self):
        """
        Applicability window as ``(first_packet_time, last_packet_time)``.

        ``(None, None)`` means no packet with a usable timestamp falls inside
        the window, so the name cannot be tied to a captured packet.
        """
        values = [
            d.applicable_time
            for d in self.declarations
            if d.applicable_time != (None, None)
        ]
        if not values:
            return (None, None)
        firsts = [value[0] for value in values if value[0] is not None]
        lasts = [value[1] for value in values if value[1] is not None]
        return (min(firsts) if firsts else None, max(lasts) if lasts else None)

    @property
    def valid_from(self):
        return self.applicable_from

    @property
    def valid_until(self):
        return self.applicable_until

    @property
    def first_seen(self):
        return self.applicable_from

    @property
    def rows(self):
        return tuple(
            AddressRow(declaration, name, reference_at=self.reference_at)
            for declaration in self.declarations
            for name in declaration.names
        )

    # ------------------------- resolution -------------------------

    def resolve(self, at=None, include_aliases=False):
        """
        Return one name for this address.

        Proven names are preferred.  With ``include_aliases=True`` a display
        alias is returned when no proven name exists.  Use
        :py:meth:`resolution` to know which kind was returned.
        """
        reference = self.reference_at if at is None else at
        proven = self.proven_names_at(reference)
        if proven:
            return proven[0]
        if include_aliases:
            aliases = self.aliases_at(reference)
            if aliases:
                return aliases[0]
        return None

    def display_name(self, at=None):
        return self.resolve(at=at, include_aliases=True)

    @property
    def name(self):
        return self.resolve()

    @property
    def display_names_first(self):
        return self.display_name()

    def resolution(self, at=None, include_aliases=True):
        reference = self.reference_at if at is None else at
        name = self.resolve(at=reference, include_aliases=include_aliases)
        if name is None:
            return None
        want_proven = name in self.proven_names_at(reference)
        for declaration in self.declarations:
            if name not in declaration.names:
                continue
            if declaration.is_proven_at(reference) != want_proven:
                continue
            return ResolutionResult(
                self.address, name, declaration, at=reference
            )
        return None

    def proves(self, block):
        """All declarations proving this address's name for ``block``."""
        return tuple(d for d in self.declarations if d.proves_packet(block))

    def names_proven_for(self, block):
        """
        Names that provably applied when ``block`` was captured.

        Later NRBs are not consulted; use :py:attr:`display_aliases` for
        names that only exist in the file for display.
        """
        return _ordered_unique(
            name
            for declaration in self.proves(block)
            for name in declaration.names
        )

    def proven_at(self, at=None):
        return self.proven_names_at(at)

    def alias_at(self, at=None):
        return self.aliases_at(at)

    def name_at(self, at=None):
        """First proven name at ``at``, or ``None``."""
        return self.resolve(at=at)

    def display_name_at(self, at=None):
        return self.resolve(at=at, include_aliases=True)

    @property
    def canonical_name(self):
        """First proven name, falling back to the first display alias."""
        return self.resolve(include_aliases=True)

    # ------------------------- mapping interface -------------------------

    _KEYS = (
        "address",
        "names",
        "all_names",
        "proven_names",
        "resolved_names",
        "display_aliases",
        "display_names",
        "aliases",
        "declarations",
        "records",
        "sources",
        "source",
        "source_blocks",
        "provenance",
        "sections",
        "section",
        "unknown_records",
        "applicable_from",
        "applicable_until",
        "applicable_time",
        "valid_from",
        "valid_until",
        "first_seen",
        "rows",
    )

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __iter__(self):
        return iter(self._KEYS)

    def __len__(self):
        return len(self._KEYS)

    def __contains__(self, value):
        return value in self.names or value in self._KEYS

    def __eq__(self, other):
        if isinstance(other, (list, tuple)):
            return self.names == list(other)
        if isinstance(other, AddressEntry):
            return (
                self.address == other.address
                and self.declarations == other.declarations
            )
        return NotImplemented

    def __repr__(self):
        return (
            "<AddressEntry address={0!r} names={1!r} proven={2!r} "
            "aliases={3!r}>"
        ).format(
            self.address,
            self.names,
            self.proven_names,
            self.display_aliases,
        )


def _at_position(at):
    """
    Normalise an ``at`` argument into ``(index_or_timestamp, section)``.

    A block gives its section and block index.  An integer is a block index.
    A float is a packet timestamp.
    """
    if at is None:
        return None, None
    section = getattr(at, "section", None)
    block_index = getattr(at, "block_index", None)
    if block_index is not None:
        return block_index, section
    if isinstance(at, float):
        return at, section
    return int(at), section


def declarations_for_block(block, start_index=0):
    """
    Build :py:class:`NameDeclaration` objects for one NRB.

    Declarations are returned in record order.  Unknown record types are
    included with their raw payload so callers can round-trip them.
    """
    declarations = []
    section = getattr(block, "section", None)
    applies_from = getattr(block, "block_index", None)
    if applies_from is None:
        applies_from = 0
    records = getattr(block, "records", None) or ()
    for record_index, record in enumerate(records):
        declarations.append(
            NameDeclaration(
                address=_record_address(record),
                names=_record_names(record),
                record_type=_record_type(record),
                source=block,
                section=section,
                record_index=record_index,
                declaration_index=start_index + record_index,
                applies_from=applies_from,
                applies_until=None,
                raw=_get(record, "raw"),
            )
        )
    return declarations


def _iter_sections(sections):
    seen = set()
    for section in sections:
        if section is None:
            continue
        key = id(section)
        if key in seen:
            continue
        seen.add(key)
        yield section


def _declarations_from_sections(sections):
    declarations = []
    for section in _iter_sections(sections):
        section_declarations = []
        for block in _section_blocks(section):
            if getattr(block, "magic_number", None) != _NRB_BLOCK_TYPE:
                continue
            section_declarations.extend(
                declarations_for_block(
                    block, start_index=len(declarations) + len(section_declarations)
                )
            )
        _apply_section_supersession(section_declarations)
        declarations.extend(section_declarations)

    for index, declaration in enumerate(declarations):
        declaration.declaration_index = index
    return declarations


def _apply_section_supersession(declarations):
    """
    End an older declaration just before a later one for the same address.

    Supersession is strictly section-local: a declaration in another section
    never shortens or replaces this one.  Several records for the same address
    inside one NRB all apply from that block; only a later *block* supersedes.
    """
    by_address = OrderedDict()
    for declaration in declarations:
        if declaration.address is None:
            continue
        by_address.setdefault(declaration.address, []).append(declaration)

    for address_declarations in by_address.values():
        for index, declaration in enumerate(address_declarations):
            for later in address_declarations[index + 1 :]:
                if later.applies_from <= declaration.applies_from:
                    continue
                declaration.applies_until = later.applies_from - 1
                break


class AddressTable(Mapping):
    """
    Address dimension table for one section or for a whole capture.

    Mapping access is by address::

        table["192.0.2.1"].names
        table["192.0.2.1"].proven_names
        table["192.0.2.1"].display_aliases

    All names and all declarations are retained.  Looking up an address never
    replaces an older section's declaration with a newer one; each declaration
    keeps its own section, source block and applicability range.  Use
    :py:meth:`for_section` (or ``section=`` on the lookup helpers) to work
    within one section.
    """

    __slots__ = [
        "section",
        "sections",
        "reference_at",
        "_declarations",
        "_entries",
        "_raw_records",
    ]

    def __init__(
        self,
        section=None,
        sections=None,
        declarations=None,
        reference_at=None,
    ):
        if declarations is not None:
            declarations = list(declarations)
            if sections is None:
                inferred = []
                for declaration in declarations:
                    candidate = declaration.section
                    if candidate is not None and candidate not in inferred:
                        inferred.append(candidate)
                sections = inferred
        else:
            if sections is None:
                sections = [section] if section is not None else []
            sections = list(_iter_sections(sections))
            declarations = _declarations_from_sections(sections)
        if sections is None:
            sections = []
        self.sections = list(_iter_sections(sections))
        for index, candidate in enumerate(self.sections):
            _ensure_section_index(candidate, index)
        if section is not None:
            _ensure_section_index(section, 0)
            self.section = section
        elif len(self.sections) == 1:
            self.section = self.sections[0]
        else:
            self.section = None
        self.reference_at = reference_at
        self._declarations = tuple(declarations)
        self._raw_records = tuple(
            {"type": declaration.record_type, "raw": declaration.raw}
            for declaration in self._declarations
            if declaration.is_unknown
        )

        grouped = OrderedDict()
        for declaration in self._declarations:
            if declaration.address is None:
                continue
            grouped.setdefault(declaration.address, []).append(declaration)
        self._entries = OrderedDict(
            (
                address,
                AddressEntry(
                    address, values, table=self, reference_at=reference_at
                ),
            )
            for address, values in grouped.items()
        )

    # ------------------------- construction helpers -------------------------

    @classmethod
    def from_section(cls, section, reference_at=None):
        return cls(section=section, reference_at=reference_at)

    @classmethod
    def from_sections(cls, sections, reference_at=None):
        return cls(sections=sections, reference_at=reference_at)

    @classmethod
    def from_blocks(cls, blocks, per_section=False):
        return build_address_table(blocks, per_section=per_section)

    # ------------------------- construction from records -------------------------

    @classmethod
    def from_records(cls, records, section=None):
        """
        Build a table directly from NRB-style record dictionaries.

        This is useful when a caller has already decoded records or wants to
        pre-populate a table without writing a block first.  Unknown record
        types are retained verbatim.
        """
        declarations = []
        for index, record in enumerate(records):
            declarations.append(
                NameDeclaration(
                    address=_record_address(record),
                    names=_record_names(record),
                    record_type=_record_type(record),
                    source=getattr(record, "source", None),
                    section=section,
                    record_index=index,
                    declaration_index=index,
                    applies_from=0,
                    raw=_get(record, "raw"),
                )
            )
        return cls(section=section, declarations=declarations)

    # ------------------------- mapping interface -------------------------

    def __getitem__(self, address):
        return self._entries[address]

    def __iter__(self):
        return iter(self._entries)

    def __len__(self):
        return len(self._entries)

    def __contains__(self, address):
        return address in self._entries

    def __repr__(self):
        return "<AddressTable sections={0!r} addresses={1!r}>".format(
            len(self.sections), list(self._entries)
        )

    # ------------------------- public data -------------------------

    @property
    def declarations(self):
        return self._declarations

    @property
    def records(self):
        return self._declarations

    @property
    def entries(self):
        return tuple(self._entries.values())

    @property
    def addresses(self):
        return tuple(self._entries.keys())

    @property
    def rows(self):
        rows = []
        for entry in self._entries.values():
            rows.extend(entry.rows)
        return tuple(rows)

    @property
    def proven_names(self):
        return {
            address: entry.proven_names
            for address, entry in self._entries.items()
        }

    @property
    def display_aliases(self):
        return {
            address: entry.display_aliases
            for address, entry in self._entries.items()
        }

    @property
    def aliases(self):
        return self.display_aliases

    @property
    def raw_records(self):
        """Unknown-type records, retained verbatim."""
        return self._raw_records

    @property
    def unknown_records(self):
        return self._raw_records

    @property
    def tables(self):
        return tuple(self.for_section(section) for section in self.sections)

    @property
    def by_section(self):
        """Per-section tables keyed by section index."""
        return self.section_tables

    @property
    def section_tables(self):
        return OrderedDict(
            (getattr(section, "section_index", None), self.for_section(section))
            for section in self.sections
        )

    # ------------------------- per-section views -------------------------

    def _match_section(self, declaration, section):
        if section is None:
            return True
        if not isinstance(section, int):
            return declaration.section is section
        declaration_index = declaration.section_index
        if declaration_index is not None:
            return declaration_index == section
        return declaration.section is section

    def for_section(self, section, reference_at=None):
        """
        Return a table containing only the declarations from ``section``.

        ``section`` may be a ``SectionHeader`` object or a section index.
        """
        declarations = [
            d
            for d in self._declarations
            if self._match_section(d, section)
        ]
        if reference_at is None:
            reference_at = self.reference_at
        section_object = section if not isinstance(section, int) else None
        if isinstance(section, int):
            for candidate in self.sections:
                if getattr(candidate, "section_index", None) == section:
                    section_object = candidate
                    break
        return AddressTable(
            section=section_object,
            sections=None,
            declarations=declarations,
            reference_at=reference_at,
        )

    # ------------------------- lookup helpers -------------------------

    def _entry_for(self, address, section=None):
        entry = self._entries.get(address)
        if entry is None or section is None:
            return entry
        declarations = [
            d for d in entry.declarations if self._match_section(d, section)
        ]
        if not declarations:
            return None
        return AddressEntry(
            address,
            declarations,
            table=self,
            reference_at=self.reference_at,
        )

    def _lookup_entry(self, address, section=None, at=None):
        """
        Return the entry used for a lookup without mutating any declaration.

        If no section is named, the section of ``at`` wins when there is one;
        otherwise an address declared in several sections resolves against
        the earliest section, so a later section can never override it.
        Declarations from every section remain available through
        :py:meth:`get_entry` and :py:attr:`AddressEntry.declarations`.
        """
        entry = self._entries.get(address)
        if entry is None:
            return None
        if section is not None:
            return self._entry_for(address, section)
        at_section = getattr(at, "section", None)
        if at_section is not None:
            return self._entry_for(address, at_section)
        sections = entry.sections
        if len(sections) > 1:
            return self._entry_for(address, sections[0])
        return entry

    def get_entry(self, address, default=None):
        return self._entries.get(address, default)

    def lookup(self, address, default=None):
        return self._entries.get(address, default)

    def entry(self, address):
        return self._entries.get(address)

    def resolve(
        self,
        address,
        section=None,
        at=None,
        include_aliases=False,
    ):
        """
        Resolve ``address`` to one name.

        :param section: restrict the lookup to one section (object or index).
        :param at: block, block index, or packet timestamp at which the name
            must have applied.  ``None`` uses static provenance.
        :param include_aliases: fall back to a display alias when no proven
            name is available.

        Without ``section`` the lookup uses the section of ``at`` when there
        is one, otherwise the earliest section declaring the address.  A
        later section never overrides an earlier one.
        """
        entry = self._lookup_entry(address, section=section, at=at)
        if entry is None:
            return None
        return entry.resolve(at=at, include_aliases=include_aliases)

    def display_name(self, address, section=None, at=None):
        entry = self._lookup_entry(address, section=section, at=at)
        if entry is None:
            return None
        return entry.resolve(at=at, include_aliases=True)

    def resolution(self, address, section=None, at=None, include_aliases=True):
        entry = self._lookup_entry(address, section=section, at=at)
        if entry is None:
            return None
        return entry.resolution(at=at, include_aliases=include_aliases)

    def lookup_name(self, address, **kwargs):
        return self.resolve(address, **kwargs)

    def name_for(self, address, **kwargs):
        return self.resolve(address, **kwargs)

    def names(self, address, section=None, at=None, include_aliases=True):
        entry = self._lookup_entry(address, section=section, at=at)
        if entry is None:
            return []
        return entry.names_at(at=at, include_aliases=include_aliases)

    def proven_names_at(self, address, section=None, at=None):
        entry = self._lookup_entry(address, section=section, at=at)
        if entry is None:
            return []
        return entry.proven_names_at(at=at)

    def resolved_names(self, address, section=None, at=None):
        return self.proven_names_at(address, section=section, at=at)

    def aliases_for(self, address, section=None, at=None):
        entry = self._lookup_entry(address, section=section, at=at)
        if entry is None:
            return []
        return entry.aliases_at(at=at)

    def display_aliases_for(self, address, section=None, at=None):
        return self.aliases_for(address, section=section, at=at)

    def at(self, at, include_aliases=True):
        """
        Return a view whose proven/alias split is evaluated at ``at``.

        With ``include_aliases=False`` only declarations applicable at that
        point remain in the table.
        """
        if include_aliases:
            declarations = [
                d for d in self._declarations if d.address is not None
            ]
        else:
            declarations = [
                d
                for d in self._declarations
                if d.address is not None and d.is_proven_at(at)
            ]
        return AddressTable(
            section=self.section,
            sections=self.sections,
            declarations=declarations,
            reference_at=at,
        )

    def to_records(self, section=None):
        """
        Return NRB-style record dictionaries in declaration order.

        Unknown record types are emitted with their original ``raw`` payload,
        which makes a table -> block round-trip lossless.
        """
        records = []
        for declaration in self._declarations:
            if declaration.address is None and not declaration.is_unknown:
                continue
            if not self._match_section(declaration, section):
                continue
            if declaration.is_unknown:
                records.append(
                    {"type": declaration.record_type, "raw": declaration.raw}
                )
            else:
                records.append(
                    {
                        "type": declaration.record_type,
                        "address": declaration.address,
                        "names": list(declaration.names),
                    }
                )
        return records

    def as_dict(self):
        return {
            address: list(entry.names) for address, entry in self._entries.items()
        }


def _collect_sections(blocks):
    sections = []
    seen = set()
    for block in blocks:
        section = getattr(block, "section", None)
        if section is None:
            if getattr(block, "magic_number", None) == 0x0A0D0D0A:
                section = block
            else:
                continue
        if id(section) in seen:
            continue
        seen.add(id(section))
        sections.append(section)

    # Sections built in memory have no file position yet; number them in
    # first-appearance order so their declarations stay distinguishable.
    for index, section in enumerate(sections):
        _ensure_section_index(section, index)
    return sections


def build_address_table(blocks, per_section=False):
    """
    Build an address dimension table from an iterable of pcap-ng blocks.

    ``blocks`` may include section headers (as returned by
    :py:class:`pcapng.scanner.FileScanner`) or any other blocks carrying a
    ``section`` attribute.  Detached Name Resolution blocks (blocks not
    registered in a section) are also accepted.  The resulting table spans
    every section while keeping declarations from different sections
    separate.

    :param per_section: when true, return one table per section.
    """
    if getattr(blocks, "magic_number", None) is not None:
        blocks = [blocks]
    blocks = list(blocks)
    sections = _collect_sections(blocks)

    registered = set()
    for section in sections:
        registered.update(id(block) for block in _section_blocks(section))
    detached = [
        block
        for block in blocks
        if getattr(block, "magic_number", None) == _NRB_BLOCK_TYPE
        and id(block) not in registered
    ]

    if not sections:
        declarations = []
        for block in detached:
            declarations.extend(
                declarations_for_block(block, start_index=len(declarations))
            )
        if per_section:
            return ()
        return AddressTable(declarations=declarations)

    if per_section:
        return tuple(AddressTable(section=section) for section in sections)

    if not detached:
        return AddressTable(sections=sections)

    declarations = _declarations_from_sections(sections)
    for block in detached:
        declarations.extend(
            declarations_for_block(block, start_index=len(declarations))
        )
    return AddressTable(sections=sections, declarations=declarations)


build_address_dimension = build_address_table
build_name_resolution_table = build_address_table

# The table is also commonly called an address book or a name-resolution
# table; keep those spellings available.
AddressBook = AddressTable
AddressDimensionTable = AddressTable
AddressDimension = AddressTable
NameResolutionTable = AddressTable
