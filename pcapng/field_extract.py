"""
Minimal byte-fragment extraction for whitelisted protocol fields.

The analysis station is only allowed to collect fields belonging to
protocol layers that have already been verified for a packet.  Given the
raw packet bytes, the verified byte range of each layer and the allowed
field descriptions (including private options), this module builds the
smallest possible set of byte fragments from which every allowed field
can be reconstructed, together with a mapping back to the original
packet offsets.

Rules enforced here:

- only bytes covered by the field's own verified layer range are
  collected; a field reaching into an unverified region is rejected;
- variable-length headers are handled through their *actual* verified
  range, never through assumed fixed sizes;
- bytes in unparsed/unverified regions are never guessed or emitted;
- unrelated payload sitting between allowed fields is excluded;
- a byte referenced by more than one field is delivered exactly once,
  while every referencing field keeps its own mapping entry.
"""

from pcapng._compat import namedtuple


class FieldExtractionError(ValueError):
    """Raised when an allowed field cannot be extracted safely."""

    pass


# Half-open byte range [start, end) of a verified protocol layer,
# expressed as absolute offsets inside the packet.
LayerRange = namedtuple("LayerRange", ("start", "end"))

# Description of an allowed field.  ``start``/``end`` are absolute
# half-open offsets inside the packet and must fall inside the verified
# range of ``layer``.
FieldSpec = namedtuple("FieldSpec", ("name", "layer", "start", "end"))

# A delivered byte fragment together with the original packet offsets
# it came from (half-open).
Fragment = namedtuple("Fragment", ("data", "original_start", "original_end"))

# Mapping entry for a single allowed field, kept even when its bytes
# overlap with another field.
FieldMapping = namedtuple("FieldMapping", ("name", "layer", "start", "end"))

# Result of an extraction.
ExtractionResult = namedtuple(
    "ExtractionResult",
    ("fragments", "field_mappings", "layer_ranges"),
)


def _coerce_range(value, what):
    """Accept LayerRange/tuple/list/dict and return (start, end)."""
    if isinstance(value, LayerRange):
        start, end = value
    elif isinstance(value, dict):
        try:
            start = value["start"]
            end = value["end"]
        except KeyError as exc:
            raise FieldExtractionError(
                "{0} range is missing key: {1}".format(what, exc.args[0])
            )
    else:
        try:
            start, end = value
        except (TypeError, ValueError):
            raise FieldExtractionError(
                "{0} range must be a (start, end) pair".format(what)
            )
    if not isinstance(start, int) or not isinstance(end, int) or isinstance(
        start, bool
    ) or isinstance(end, bool):
        raise FieldExtractionError(
            "{0} range bounds must be integers".format(what)
        )
    if start < 0 or end < start:
        raise FieldExtractionError(
            "invalid {0} range: [{1}, {2})".format(what, start, end)
        )
    return start, end


def _coerce_field(spec):
    """Accept FieldSpec/tuple/dict and return a validated FieldSpec."""
    if isinstance(spec, FieldSpec):
        name, layer, start, end = spec
    elif isinstance(spec, dict):
        try:
            name = spec["name"]
            layer = spec["layer"]
            start = spec["start"]
            end = spec["end"]
        except KeyError as exc:
            raise FieldExtractionError(
                "field spec is missing key: {0}".format(exc.args[0])
            )
    else:
        try:
            name, layer, start, end = spec
        except (TypeError, ValueError):
            raise FieldExtractionError(
                "field spec must be a (name, layer, start, end) tuple"
            )
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or isinstance(start, bool)
        or isinstance(end, bool)
    ):
        raise FieldExtractionError(
            "field {0!r} bounds must be integers".format(name)
        )
    if start < 0 or end <= start:
        raise FieldExtractionError(
            "field {0!r} has invalid range: [{1}, {2})".format(
                name, start, end
            )
        )
    return FieldSpec(name, layer, start, end)


def normalize_layer_ranges(layer_ranges):
    """
    Validate the per-packet verified layer ranges.

    Returns a dict mapping layer identifier to a LayerRange.  Ranges are
    absolute half-open offsets into the packet and must fit inside
    ``packet_length``.
    """
    if layer_ranges is None:
        raise FieldExtractionError("layer_ranges is required")
    normalized = {}
    for layer, value in layer_ranges.items():
        start, end = _coerce_range(value, "layer {0!r}".format(layer))
        normalized[layer] = LayerRange(start, end)
    return normalized


def field_at_offset(name, layer, layer_ranges, offset, length):
    """
    Build a FieldSpec for a field located at a layer-relative offset.

    This is the helper to use for variable-length headers: the caller
    computes the offset from the layer's *actual* verified range rather
    than from an assumed fixed header size.
    """
    if layer not in layer_ranges:
        raise FieldExtractionError(
            "unknown layer {0!r} for field {1!r}".format(layer, name)
        )
    layer_start = layer_ranges[layer].start
    if not isinstance(offset, int) or not isinstance(length, int):
        raise FieldExtractionError(
            "field {0!r} offset and length must be integers".format(name)
        )
    if offset < 0 or length <= 0:
        raise FieldExtractionError(
            "field {0!r} has invalid offset/length".format(name)
        )
    start = layer_start + offset
    return FieldSpec(name, layer, start, start + length)


def extract_field_fragments(packet, layer_ranges, fields):
    """
    Extract the minimal byte fragments covering the allowed fields.

    :param packet: raw packet bytes (any bytes-like object).
    :param layer_ranges: mapping of layer identifier to its verified
        half-open byte range (LayerRange, tuple or dict form).
    :param fields: iterable of allowed field descriptions (FieldSpec,
        ``(name, layer, start, end)`` tuple or dict form).
    :returns: ExtractionResult with merged non-overlapping fragments,
        one FieldMapping per requested field and the normalized layer
        ranges.
    :raises FieldExtractionError: if a field references an unknown layer
        or reaches outside the layer's verified range, or any range is
        malformed or outside the packet.
    """
    packet = bytes(packet)
    packet_length = len(packet)

    normalized_layers = normalize_layer_ranges(layer_ranges)
    for layer, layer_range in normalized_layers.items():
        if layer_range.end > packet_length:
            raise FieldExtractionError(
                "layer {0!r} range [{1}, {2}) exceeds packet length {3}".format(
                    layer, layer_range.start, layer_range.end, packet_length
                )
            )

    field_specs = [_coerce_field(spec) for spec in fields]

    mappings = []
    intervals = []
    for spec in field_specs:
        if spec.layer not in normalized_layers:
            raise FieldExtractionError(
                "field {0!r} references unknown layer {1!r}".format(
                    spec.name, spec.layer
                )
            )
        layer_range = normalized_layers[spec.layer]
        if (
            spec.start < layer_range.start
            or spec.end > layer_range.end
            or spec.end > packet_length
        ):
            raise FieldExtractionError(
                "field {0!r} range [{1}, {2}) is not fully inside "
                "verified layer {3!r} range [{4}, {5})".format(
                    spec.name,
                    spec.start,
                    spec.end,
                    spec.layer,
                    layer_range.start,
                    layer_range.end,
                )
            )
        mappings.append(
            FieldMapping(spec.name, spec.layer, spec.start, spec.end)
        )
        intervals.append((spec.start, spec.end))

    fragments = []
    if intervals:
        intervals.sort()
        current_start, current_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= current_end:
                # Overlapping or adjacent intervals share no extra byte;
                # merge so every covered byte is delivered exactly once.
                if end > current_end:
                    current_end = end
            else:
                fragments.append(
                    Fragment(
                        packet[current_start:current_end],
                        current_start,
                        current_end,
                    )
                )
                current_start, current_end = start, end
        fragments.append(
            Fragment(
                packet[current_start:current_end],
                current_start,
                current_end,
            )
        )

    return ExtractionResult(fragments, mappings, normalized_layers)


def reassemble(result, start, end):
    """
    Rebuild ``packet[start:end]`` from the delivered fragments.

    Raises FieldExtractionError if any byte in the requested range was
    not delivered (i.e. the range is not fully covered by allowed
    fields).
    """
    if not isinstance(start, int) or not isinstance(end, int):
        raise FieldExtractionError("range bounds must be integers")
    if start < 0 or end < start:
        raise FieldExtractionError(
            "invalid reassembly range: [{0}, {1})".format(start, end)
        )
    rebuilt = bytearray()
    position = start
    for fragment in result.fragments:
        fragment_end = fragment.original_end
        if fragment_end <= position:
            continue
        if fragment.original_start >= end:
            break
        if fragment.original_start > position:
            raise FieldExtractionError(
                "range [{0}, {1}) is not fully covered by fragments "
                "(gap at {2})".format(start, end, position)
            )
        slice_start = position - fragment.original_start
        slice_end = min(end, fragment_end) - fragment.original_start
        rebuilt.extend(fragment.data[slice_start:slice_end])
        position = min(end, fragment_end)
        if position == end:
            break
    if position != end:
        raise FieldExtractionError(
            "range [{0}, {1}) is not fully covered by fragments".format(
                start, end
            )
        )
    return bytes(rebuilt)


def verify_extraction(packet, result):
    """
    Re-check an extraction against the original packet.

    Confirms that fragments are disjoint, match the original bytes at
    their recorded offsets and that every mapped field can be
    reassembled exactly from the delivered fragments.  Returns True on
    success, raises FieldExtractionError otherwise.
    """
    packet = bytes(packet)
    covered = set()
    previous_end = None
    for fragment in result.fragments:
        if previous_end is not None and fragment.original_start < previous_end:
            raise FieldExtractionError(
                "fragments overlap at offset {0}".format(
                    fragment.original_start
                )
            )
        if (
            fragment.original_start < 0
            or fragment.original_end > len(packet)
            or fragment.original_end <= fragment.original_start
        ):
            raise FieldExtractionError(
                "fragment [{0}, {1}) is outside the packet".format(
                    fragment.original_start, fragment.original_end
                )
            )
        if fragment.data != packet[
            fragment.original_start:fragment.original_end
        ]:
            raise FieldExtractionError(
                "fragment data does not match packet bytes at [{0}, {1})".format(
                    fragment.original_start, fragment.original_end
                )
            )
        for offset in range(fragment.original_start, fragment.original_end):
            if offset in covered:
                raise FieldExtractionError(
                    "byte {0} delivered more than once".format(offset)
                )
            covered.add(offset)
        previous_end = fragment.original_end

    for mapping in result.field_mappings:
        if mapping.end > len(packet):
            raise FieldExtractionError(
                "field {0!r} mapping exceeds packet length".format(mapping.name)
            )
        rebuilt = reassemble(result, mapping.start, mapping.end)
        if rebuilt != packet[mapping.start:mapping.end]:
            raise FieldExtractionError(
                "field {0!r} cannot be reconstructed from fragments".format(
                    mapping.name
                )
            )
    return True
