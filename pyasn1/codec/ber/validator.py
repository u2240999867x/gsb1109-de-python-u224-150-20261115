#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""Validate a batch of concatenated BER-encoded objects against a schema.

The regular decoder is all-or-nothing: the first constraint violation or
malformed item aborts iteration, so the complete objects that follow it are
never seen by the caller.  This module decodes each top-level TLV on its own,
validates the decoded value against the original (constrained) schema, and
keeps every diagnostic attached to the byte range it came from.  A failing
item therefore does not consume its neighbours.

Values are decoded through a constraint-free copy of the schema.  Constraints
are checked afterwards, while walking the original schema, which is what makes
it possible to report the failing field path rather than only the first
exception the decoder happened to raise.
"""

import sys

from pyasn1 import error
from pyasn1.codec.ber import decoder
from pyasn1.type import base
from pyasn1.type import constraint
from pyasn1.type import namedtype
from pyasn1.type import tag
from pyasn1.type import univ

__all__ = ['validate', 'validateBatch', 'validate_batch',
           'ValidationItem', 'Diagnostic', 'ValidationReport']

_EMPTY_CONSTRAINTS = constraint.ConstraintsIntersection()

#: Diagnostic kind for a collection size constraint (SEQUENCE OF/SET OF SIZE).
SIZE = 'size'

#: Diagnostic kind for an element value constraint.
VALUE = 'value'

#: Diagnostic kind for malformed encoding or a structurally wrong item.
DECODE = 'decode'


class _ResyncExhausted(Exception):
    """The bounded recovery scan ran out of parsing budget."""


class _FramingError(Exception):
    """The substrate at a given offset does not form a complete TLV.

    ``detectedEnd`` is the offset at which the failure became visible, or
    ``None`` when the whole remaining substrate was examined.  It bounds the
    reported original range so a bad item never claims octets that belong to
    the object that follows it.
    """

    def __init__(self, message, detectedEnd=None, structural=False):
        Exception.__init__(self, message)
        self.detectedEnd = detectedEnd
        self.structural = structural


class _TlvNode(object):
    """One BER tag-length-value span, with nested children when constructed.

    Only the outer extent (``start``/``end``) and the children matter to the
    report; the tag is needed to line a decoded component up with the wire.
    """

    __slots__ = ('start', 'end', 'tag', 'children')

    def __init__(self, start, end, tag, children):
        self.start = start
        self.end = end
        self.tag = tag
        self.children = children


class ValidationItem(object):
    """A complete object that passed both decoding and constraint checks.

    Attributes
    ----------
    index: :class:`int`
        Zero-based position of the item in the batch.

    value: :py:class:`~pyasn1.type.base.PyAsn1Item`
        Decoded value, safe to store.

    start, end: :class:`int`
        Byte range of the complete top-level TLV in the original substrate.

    raw: :class:`bytes`
        Original bytes of the complete top-level TLV.
    """

    __slots__ = ('index', 'value', 'start', 'end', 'raw')

    def __init__(self, index, value, start, end, raw):
        self.index = index
        self.value = value
        self.start = start
        self.end = end
        self.raw = raw

    @property
    def range(self):
        """Return the ``(start, end)`` byte range of this item."""
        return self.start, self.end

    #: Alias of :attr:`range`.
    originalRange = range

    @property
    def original(self):
        """Alias of :attr:`raw`."""
        return self.raw

    def __bool__(self):
        return True

    def __repr__(self):
        return '<%s object, index %s, range %s:%s>' % (
            self.__class__.__name__, self.index, self.start, self.end)


class Diagnostic(object):
    """A rejected value, with its field path and original byte range.

    Attributes
    ----------
    itemIndex: :class:`int`
        Zero-based position of the top-level item this diagnostic belongs to.

    path: :class:`str`
        Complete field path, rooted at ``$``.  Sequence/Set fields are joined
        with ``.`` and SEQUENCE OF/SET OF elements use ``[index]``.  A CHOICE
        contributes the name of the alternative that was actually decoded.

    kind: :class:`str`
        ``'size'`` for a collection size constraint, ``'value'`` for an
        element value constraint, ``'decode'`` for a malformed or structurally
        incompatible item.

    constraint: :py:class:`~pyasn1.type.constraint.AbstractConstraint`
        The innermost constraint object that rejected the value, or
        :obj:`None` for decoding failures.

    constraintName: :class:`str`
        Class name of ``constraint`` (e.g. ``'ValueSizeConstraint'``).

    constraintText: :class:`str`
        ``repr()``-style rendering of ``constraint``.

    value: :py:class:`~pyasn1.type.base.PyAsn1Item`
        The rejected ASN.1 value.

    start, end: :class:`int`
        Byte range of the rejected value, or of the whole item for decoding
        failures.

    raw: :class:`bytes`
        Original bytes in ``[start:end]``.

    reason: :class:`str`
        Exception text for the failure.  For decoding failures this is the
        decoder/framing message; constraint failures carry the aggregated
        constraint message as well.
    """

    __slots__ = ('itemIndex', 'path', 'kind', 'constraint', 'constraintName',
                 'constraintText', 'value', 'start', 'end', 'raw', 'reason',
                 'itemStart', 'itemEnd')

    def __init__(self, itemIndex, path, kind, constraint, value,
                 start, end, raw, reason='', itemStart=None, itemEnd=None):
        self.itemIndex = itemIndex
        self.path = path
        self.kind = kind
        self.constraint = constraint
        self.constraintName = (
            constraint is not None and constraint.__class__.__name__ or '')
        self.constraintText = (
            constraint is not None and repr(constraint) or '')
        self.value = value
        self.start = start
        self.end = end
        self.raw = raw
        self.reason = reason
        self.itemStart = start if itemStart is None else itemStart
        self.itemEnd = end if itemEnd is None else itemEnd

    @property
    def range(self):
        """Return the ``(start, end)`` byte range of the rejected value."""
        return self.start, self.end

    #: Alias of :attr:`range`.
    originalRange = range

    @property
    def fieldPath(self):
        """Alias of :attr:`path`."""
        return self.path

    @property
    def original(self):
        """Alias of :attr:`raw`."""
        return self.raw

    @property
    def message(self):
        """Return a single-line human readable description."""
        if self.kind == DECODE:
            return '%s: %s' % (self.path, self.reason)

        return '%s: %s failed for %r' % (
            self.path, self.constraintText, self.value)

    def asDict(self):
        """Return the diagnostic as a plain :class:`dict`."""
        return {
            'itemIndex': self.itemIndex,
            'path': self.path,
            'kind': self.kind,
            'constraint': self.constraintName,
            'constraintText': self.constraintText,
            'start': self.start,
            'end': self.end,
            'range': self.range,
            'value': self.value,
            'raw': self.raw,
            'reason': self.reason,
        }

    def __repr__(self):
        return '<%s object, item %s, path %r, kind %r, range %s:%s>' % (
            self.__class__.__name__, self.itemIndex, self.path, self.kind,
            self.start, self.end)


class ValidationReport(object):
    """Outcome of :func:`validate`.

    Attributes
    ----------
    objects: :class:`tuple` of :class:`ValidationItem`
        Items that decoded and passed every constraint.  These are the
        objects safe to store.

    failures: :class:`tuple` of :class:`Diagnostic`
        Every rejected item, in substrate order.
    """

    __slots__ = ('objects', 'failures')

    def __init__(self, objects, failures):
        self.objects = tuple(objects)
        self.failures = tuple(failures)

    @property
    def accepted(self):
        """Alias for :attr:`objects`."""
        return self.objects

    @property
    def values(self):
        """Return the decoded values of all accepted items."""
        return tuple(item.value for item in self.objects)

    @property
    def diagnostics(self):
        """Alias for :attr:`failures`."""
        return self.failures

    @property
    def sizeFailures(self):
        """Failures caused by a collection size constraint."""
        return tuple(item for item in self.failures if item.kind == SIZE)

    @property
    def valueFailures(self):
        """Failures caused by an element value constraint."""
        return tuple(item for item in self.failures if item.kind == VALUE)

    @property
    def decodeFailures(self):
        """Failures caused by malformed or structurally wrong encoding."""
        return tuple(item for item in self.failures if item.kind == DECODE)

    @property
    def failuresByItem(self):
        """Map each top-level item index to its diagnostics.

        Size and value failures stay distinguishable inside each entry, so a
        caller can list them separately per object.
        """
        grouped = {}

        for failure in self.failures:
            grouped.setdefault(failure.itemIndex, []).append(failure)

        return dict((index, tuple(items)) for index, items in grouped.items())

    def itemFailures(self, index):
        """Return ``(sizeFailures, valueFailures)`` for one top-level item."""
        failures = self.failuresByItem.get(index, ())

        return (tuple(item for item in failures if item.kind == SIZE),
                tuple(item for item in failures if item.kind == VALUE))

    @property
    def ok(self):
        """Indicate whether every item in the batch was accepted."""
        return not self.failures

    def __bool__(self):
        return self.ok

    def __len__(self):
        return len(self.objects)

    def __iter__(self):
        return iter(self.objects)

    def __repr__(self):
        return '<%s object, accepted %d, failures %d>' % (
            self.__class__.__name__, len(self.objects), len(self.failures))


def _asBytes(substrate):
    if isinstance(substrate, bytes):
        return substrate

    if isinstance(substrate, (bytearray, memoryview)):
        return bytes(substrate)

    if isinstance(substrate, univ.OctetString):
        return substrate.asOctets()

    read = getattr(substrate, 'read', None)
    if read is not None:
        data = read()
        if isinstance(data, str):
            raise error.PyAsn1Error('Text-mode substrate is not supported')
        return bytes(data)

    raise error.PyAsn1Error(
        'Unsupported substrate type %s' % substrate.__class__.__name__)


def _decodeTag(data, offset):
    if offset >= len(data):
        raise _FramingError(
            'Missing identifier octet at offset %d' % offset,
            detectedEnd=len(data))

    firstOctet = data[offset]
    offset += 1

    tagClass = firstOctet & 0xC0
    tagFormat = firstOctet & 0x20
    tagId = firstOctet & 0x1F

    if tagId == 0x1F:
        tagId = 0
        octetCount = 0
        while True:
            if offset >= len(data):
                raise _FramingError(
                    'Truncated long-form tag', detectedEnd=len(data))

            octet = data[offset]
            offset += 1
            octetCount += 1

            if octetCount > decoder.MAX_TAG_OCTETS:
                raise _FramingError(
                    'Tag ID octet count exceeds limit (%d)' %
                    decoder.MAX_TAG_OCTETS, detectedEnd=len(data))

            tagId <<= 7
            tagId |= octet & 0x7F

            if not octet & 0x80:
                break

    return tag.Tag(tagClass, tagFormat, tagId), offset


def _decodeLength(data, offset):
    if offset >= len(data):
        raise _FramingError('Missing length octet', detectedEnd=len(data))

    firstOctet = data[offset]
    offset += 1

    if firstOctet < 128:
        return firstOctet, offset, False

    if firstOctet == 128:
        return -1, offset, True

    size = firstOctet & 0x7F
    if size > decoder.MAX_LENGTH_OCTETS:
        raise _FramingError(
            'BER length field size %d exceeds limit (%d)' %
            (size, decoder.MAX_LENGTH_OCTETS), detectedEnd=len(data))

    if offset + size > len(data):
        raise _FramingError('Truncated length field', detectedEnd=len(data))

    length = int.from_bytes(data[offset:offset + size], 'big')
    offset += size

    if length > sys.maxsize:
        raise _FramingError(
            'Length %d exceeds maximum readable size' % length,
            detectedEnd=len(data))

    return length, offset, False


def _parseTlv(data, offset, depth, budget=None):
    if budget is not None:
        budget[0] -= 1
        if budget[0] < 0:
            raise _ResyncExhausted()

    if depth > decoder.MAX_NESTING_DEPTH:
        raise _FramingError(
            'ASN.1 structure nesting depth exceeds limit (%d)' %
            decoder.MAX_NESTING_DEPTH, structural=True)

    start = offset
    tagObject, offset = _decodeTag(data, offset)

    length, contentStart, indefinite = _decodeLength(data, offset)

    if indefinite:
        if tagObject.tagFormat != tag.tagFormatConstructed:
            raise _FramingError(
                'Indefinite length on primitive tag at offset %d' % start,
                structural=True)

        children = []
        cursor = contentStart

        while True:
            if cursor + 2 > len(data):
                raise _FramingError(
                    'Missing end-of-octets at offset %d' % cursor,
                    detectedEnd=len(data))

            if data[cursor] == 0 and data[cursor + 1] == 0:
                contentEnd = cursor
                end = cursor + 2
                break

            child = _parseTlv(data, cursor, depth + 1, budget)
            children.append(child)
            cursor = child.end

        return _TlvNode(start, end, tagObject, children)

    contentEnd = contentStart + length
    if contentEnd > len(data):
        raise _FramingError(
            'Truncated value at offset %d: need %d octets, have %d' %
            (start, length, len(data) - contentStart),
            detectedEnd=len(data))

    children = []
    if tagObject.tagFormat == tag.tagFormatConstructed:
        cursor = contentStart
        while cursor < contentEnd:
            child = _parseTlv(data, cursor, depth + 1, budget)
            if child.end > contentEnd:
                raise _FramingError(
                    'Child value exceeds parent extent at offset %d' % cursor,
                    detectedEnd=cursor, structural=True)
            children.append(child)
            cursor = child.end

    return _TlvNode(start, contentEnd, tagObject, children)


def _installComponentType(schema, componentType):
    """Attach a component schema to an ephemeral relaxed-schema prototype.

    ``componentType`` is a read-only attribute by design, but the relaxed
    prototype is never exposed.  Building it in two steps is what makes
    recursive schemas terminate: the clone is memoized before its children
    are visited.
    """
    object.__setattr__(schema, 'componentType', componentType)
    schema.readOnly['componentType'] = componentType

    if isinstance(componentType, namedtype.NamedTypes):
        object.__setattr__(schema, '_componentTypeLen', len(componentType))
        object.__setattr__(schema, '_componentValues', [])
        object.__setattr__(
            schema, '_dynamicNames',
            len(componentType) or schema.DynamicNames())


_SAFE_SCHEMA_CLASSES = {}


def _safeSchemaClass(cls):
    """Return a subclass of *cls* whose representation never recurses.

    A relaxed recursive schema contains a genuine reference cycle (the same
    prototype is reused for its own nested component).  That is what the
    decoder needs, but ``repr()`` of such a schema would recurse forever, and
    the decoder formats its spec into the debug log.  The temporary subclass
    keeps the cycle while making every diagnostic rendering shallow.
    """
    try:
        return _SAFE_SCHEMA_CLASSES[cls]

    except KeyError:
        pass

    def safeRepr(self):
        return '<%s object, relaxed schema>' % cls.__name__

    def safePrettyPrint(self, scope=0):
        return safeRepr(self)

    def safePrettyPrintType(self, scope=0):
        return '%s -> %s' % (self.tagSet, cls.__name__)

    safeClass = type('Relaxed' + cls.__name__, (cls,), {
        '__repr__': safeRepr,
        '__str__': safeRepr,
        'prettyPrint': safePrettyPrint,
        'prettyPrintType': safePrettyPrintType,
    })

    _SAFE_SCHEMA_CLASSES[cls] = safeClass
    return safeClass


def _rebuild(spec, **kwargs):
    """Instantiate a safe relaxed copy of *spec*, keeping its read-only kit."""
    if isinstance(spec, base.ConstructedAsn1Type):
        initializers = spec._cloneInitializers()

    else:
        initializers = spec.readOnly.copy()

    initializers.update(kwargs)

    return _safeSchemaClass(spec.__class__)(**initializers)


def _clearConstraints(spec):
    """Clone *spec* with its own constraints removed."""
    kwargs = {'subtypeSpec': _EMPTY_CONSTRAINTS}

    if isinstance(spec, base.ConstructedAsn1Type):
        kwargs['sizeSpec'] = _EMPTY_CONSTRAINTS

    return _rebuild(spec, **kwargs)


def _relaxedSchema(spec, memo, depth):
    """Return a constraint-free copy of *spec*, preserving tags and shape."""
    key = id(spec)
    if key in memo:
        return memo[key]

    if depth > decoder.MAX_NESTING_DEPTH:
        return _clearConstraints(spec)

    if isinstance(spec, (univ.Choice, univ.Sequence, univ.Set)):
        componentType = spec.componentType
        if not componentType:
            # A recursively defined schema may have been captured before its
            # class-level componentType was completed; clone() re-resolves it.
            componentType = spec.clone().componentType

        schema = _rebuild(spec, componentType=namedtype.NamedTypes(),
                          subtypeSpec=_EMPTY_CONSTRAINTS,
                          sizeSpec=_EMPTY_CONSTRAINTS)
        memo[key] = schema

        relaxed = []
        for namedType in componentType.namedTypes:
            relaxed.append(namedType.__class__(
                namedType.name,
                _relaxedSchema(namedType.asn1Object, memo, depth + 1),
                openType=namedType.openType))

        _installComponentType(schema, namedtype.NamedTypes(*relaxed))
        return schema

    if isinstance(spec, (univ.SequenceOf, univ.SetOf)):
        schema = _rebuild(spec, componentType=None,
                          subtypeSpec=_EMPTY_CONSTRAINTS,
                          sizeSpec=_EMPTY_CONSTRAINTS)
        memo[key] = schema

        if spec.componentType is not None:
            _installComponentType(
                schema,
                _relaxedSchema(spec.componentType, memo, depth + 1))

        return schema

    schema = _clearConstraints(spec)
    memo[key] = schema
    return schema


def _unwrapNode(node, spec):
    """Descend EXPLICIT wrappers, returning the TLV carrying the value."""
    superTags = spec.tagSet.superTags
    if not superTags:
        return node

    # An untagged base type (e.g. CHOICE) contributes no base tag of its own,
    # so every superTag is a wrapper.  A tagged type's first superTag is its
    # base tag and must not be unwrapped.
    lowest = 1 if spec.tagSet.baseTag else 0
    index = len(superTags) - 1

    while (index >= lowest and node.tag == superTags[index] and
           len(node.children) == 1):
        node = node.children[0]
        index -= 1

    return node


def _constraintTarget(value, spec):
    """Represent *value* the way constructed-type constraints expect it."""
    if isinstance(spec, (univ.Sequence, univ.Set, univ.Choice)):
        componentType = spec.componentType
        if not componentType:
            return value

        mapping = {}

        for idx, component in enumerate(value._componentValues):
            if component is base.noValue or not component.isValue:
                continue
            mapping[componentType.getNameByPosition(idx)] = component

        return mapping

    if isinstance(spec, (univ.SequenceOf, univ.SetOf)):
        mapping = {}
        for idx, component in value._componentValues.items():
            if component is base.noValue or not component.isValue:
                continue
            mapping[idx] = component

        return mapping

    return value


def _passesConstraint(constraintObject, target):
    if not constraintObject:
        return True

    try:
        constraintObject(target)

    except (error.PyAsn1Error, TypeError, ValueError):
        return False

    return True


def _firstFailure(constraintObject, target):
    """Find the innermost constraint responsible for a failed check."""
    if isinstance(constraintObject, constraint.AbstractConstraintSet):
        for child in constraintObject:
            if not _passesConstraint(child, target):
                return _firstFailure(child, target) or child
        return None

    if isinstance(constraintObject, constraint.ContainedSubtypeConstraint):
        if _passesConstraint(constraintObject, target):
            return None

        for child in constraintObject:
            if isinstance(child, constraint.AbstractConstraint):
                if not _passesConstraint(child, target):
                    return _firstFailure(child, target) or child

        return constraintObject

    if _passesConstraint(constraintObject, target):
        return None

    return constraintObject


def _isCollection(spec):
    return isinstance(spec, (univ.SequenceOf, univ.SetOf,
                             univ.Sequence, univ.Set))


def _declaredBy(constraintObject, ownSpec):
    """Tell whether *constraintObject* is part of *ownSpec*'s chain."""
    if ownSpec is None:
        return False

    if ownSpec is constraintObject:
        return True

    if isinstance(ownSpec, constraint.AbstractConstraintSet):
        return any(_declaredBy(constraintObject, child) for child in ownSpec)

    return False


def _diagnosticKind(spec, constraintObject):
    # Only a collection's own size constraint is a "size" failure.  A
    # ValueSizeConstraint nested under WithComponentsConstraint still checks
    # an element's value, so the type of the constraint alone is not enough.
    if (_isCollection(spec) and
            isinstance(constraintObject, constraint.ValueSizeConstraint) and
            _declaredBy(constraintObject, getattr(spec, 'subtypeSpec', None))):
        return SIZE

    return VALUE


class _ItemContext(object):
    __slots__ = ('index', 'start', 'end', 'raw')

    def __init__(self, index, node, data):
        self.index = index
        self.start = node.start
        self.end = node.end
        self.raw = data[node.start:node.end]


def _checkConstraints(value, spec, path, node, context, diagnostics):
    subtypeSpec = spec.subtypeSpec
    if not subtypeSpec:
        return

    target = _constraintTarget(value, spec)

    try:
        subtypeSpec(target)

    except (error.PyAsn1Error, TypeError, ValueError) as exc:
        failing = _firstFailure(subtypeSpec, target) or subtypeSpec
        diagnostics.append(Diagnostic(
            context.index, path, _diagnosticKind(spec, failing), failing,
            value, node.start, node.end,
            context.raw[node.start - context.start:node.end - context.start],
            reason=str(exc), itemStart=context.start, itemEnd=context.end))


def _matchChild(node, component, used, preferred=0):
    """Find the wire child carrying a decoded component."""
    effectiveTagSet = component.effectiveTagSet
    outerTag = None

    if effectiveTagSet.superTags:
        outerTag = effectiveTagSet.superTags[-1]

    children = node.children

    if outerTag is not None:
        for idx in range(preferred, len(children)):
            if idx not in used and children[idx].tag == outerTag:
                return idx
        for idx in range(len(children)):
            if idx not in used and children[idx].tag == outerTag:
                return idx

        return None

    for idx in range(len(children)):
        if idx not in used:
            return idx

    return None


def _validateNamed(value, spec, path, node, context, diagnostics):
    if isinstance(spec, univ.Choice):
        try:
            name = value.getName()
        except error.PyAsn1Error:
            return

        try:
            namedType = spec.componentType[name]
        except error.PyAsn1Error:
            return

        component = value.getComponent()
        _validateValue(component, namedType.asn1Object,
                       path + '.' + name, node, context, diagnostics)
        return

    if not spec.componentType:
        return

    used = set()

    for idx, namedType in enumerate(spec.componentType.namedTypes):
        component = value.getComponentByPosition(idx, instantiate=False)
        if component is base.noValue or not component.isValue:
            continue

        childIndex = _matchChild(node, component, used, preferred=idx)
        if childIndex is None:
            childNode = node
        else:
            used.add(childIndex)
            childNode = node.children[childIndex]

        _validateValue(component, namedType.asn1Object,
                       '%s.%s' % (path, namedType.name), childNode,
                       context, diagnostics)


def _resolvedSpec(spec):
    """Return *spec* with a completed ``componentType`` when it has none.

    A recursively defined schema instantiates its own placeholder before the
    class-level ``componentType`` is assigned; ``clone()`` re-resolves that
    placeholder from the class (see
    ``ConstructedAsn1Type._cloneInitializers``).
    Validation must follow the same rule, or a nested value decoded through a
    resolved schema would be compared against an empty one.
    """
    if (isinstance(spec, (univ.Choice, univ.Sequence, univ.Set)) and
            not spec.componentType):
        return spec.clone()

    return spec


def _validateValue(value, spec, path, node, context, diagnostics):
    if spec is None or not isinstance(value, base.Asn1Item):
        return

    spec = _resolvedSpec(spec)

    node = _unwrapNode(node, spec)

    _checkConstraints(value, spec, path, node, context, diagnostics)

    if isinstance(spec, (univ.Choice, univ.Sequence, univ.Set)):
        _validateNamed(value, spec, path, node, context, diagnostics)

    elif isinstance(spec, (univ.SequenceOf, univ.SetOf)):
        componentType = spec.componentType
        if componentType is None:
            return

        for idx, component in enumerate(value):
            childNode = (node.children[idx]
                         if idx < len(node.children) else node)
            _validateValue(component, componentType, '%s[%d]' % (path, idx),
                           childNode, context, diagnostics)


def _retype(value, spec):
    """Rebuild *value* using the classes and schema of *spec*.

    The decoded tree may be made of generated relaxed-schema subclasses.  This
    walks the value once and produces an equivalent tree of the caller's own
    classes, carrying the original constraints in ``readOnly``.  Values were
    already validated, so components are set without re-checking constraints.
    """
    spec = _resolvedSpec(spec)

    if isinstance(spec, univ.Choice):
        rebuilt = type(spec)(**spec.readOnly)

        if value.isValue:
            try:
                name = value.getName()
            except error.PyAsn1Error:
                pass

            else:
                idx = spec.componentType.getPositionByName(name)
                component = _retype(value.getComponent(),
                                    spec.componentType[idx].asn1Object)
                rebuilt.setComponentByPosition(
                    idx, component, verifyConstraints=False,
                    matchTags=False, matchConstraints=False)

        return rebuilt

    if isinstance(spec, (univ.Sequence, univ.Set)):
        rebuilt = type(spec)(**spec.readOnly)

        namedTypes = (spec.componentType.namedTypes
                      if spec.componentType else ())

        for idx, component in enumerate(value._componentValues):
            if component is base.noValue or not component.isValue:
                continue

            if idx < len(namedTypes):
                componentSpec = namedTypes[idx].asn1Object

            else:
                componentSpec = component

            rebuilt.setComponentByPosition(
                idx, _retype(component, componentSpec),
                verifyConstraints=False, matchTags=False,
                matchConstraints=False)

        return rebuilt

    if isinstance(spec, (univ.SequenceOf, univ.SetOf)):
        rebuilt = type(spec)(**spec.readOnly)

        for component in value:
            if component is base.noValue or not component.isValue:
                continue

            if spec.componentType is not None:
                component = _retype(component, spec.componentType)
            else:
                component = component.clone()

            rebuilt.append(component)

        return rebuilt

    return spec.clone(value=value)


def _decodeItem(data, node, schema):
    substrate = data[node.start:node.end]

    if schema is None:
        value, tail = decoder.decode(substrate)

    else:
        value, tail = decoder.decode(substrate, asn1Spec=schema)

    if tail:
        raise error.PyAsn1Error(
            'Trailing %d octets inside a single item' % len(tail))

    return value


def _declaredExtent(data, offset):
    """Return the end offset a definite-length TLV claims, if determinable.

    Used only to bound a structural failure: when the top-level header is
    sound but a nested value is not, the whole declared item is one bad
    object and scanning inside it would report fragments of the same damage.
    """
    try:
        _tag, offset = _decodeTag(data, offset)
        length, contentStart, indefinite = _decodeLength(data, offset)

    except _FramingError:
        return None

    if indefinite or length < 0:
        return None

    end = contentStart + length

    if end > len(data):
        return None

    return end


def _expectedTags(spec):
    """Return the outer tags a top-level object may legitimately carry.

    :obj:`None` means the schema cannot narrow the tag (e.g. ``Any`` or a
    schema-less decode), so every tag is a possible start.
    """
    if spec is None:
        return None

    spec = _resolvedSpec(spec)

    if isinstance(spec, univ.Choice) and not spec.tagSet:
        tags = set()

        for namedType in spec.componentType.namedTypes:
            componentTags = _expectedTags(namedType.asn1Object)

            if componentTags is None:
                return None

            tags.update(componentTags)

        return tags or None

    superTags = spec.tagSet.superTags
    if not superTags:
        return None

    return {superTags[-1]}


#: Upper bound on full decodes attempted while recovering from a bad prefix.
MAX_RESYNC_DECODES = 256


def _resync(data, offset, schema, limit=None):
    """Find the next offset that starts a complete top-level object.

    Recovery is bounded in both CPU and candidate count.  When the schema
    fixes the outer tag, only offsets carrying that identifier octet are
    tried, located with ``bytes.find`` rather than a Python-level loop.  A
    shared node budget caps the total TLV parsing work, so a long malformed
    prefix cannot turn recovery into quadratic time.

    ``limit`` bounds the scan when the broken item declared an extent.
    """
    expectedTags = _expectedTags(schema)
    firstOctets = None

    if expectedTags is not None:
        firstOctets = set()

        for expected in expectedTags:
            if expected.tagId < 0x1F:
                firstOctets.add(
                    expected.tagClass | expected.tagFormat | expected.tagId)
            else:
                firstOctets.add(
                    expected.tagClass | expected.tagFormat | 0x1F)

    stop = len(data) if limit is None else min(limit, len(data))
    budget = [4 * (len(data) - offset) + 1024]
    decodes = 0
    cursor = offset

    while cursor < stop:
        if firstOctets is not None:
            positions = [data.find(bytes([octet]), cursor, stop)
                         for octet in firstOctets]
            positions = [position for position in positions if position >= 0]

            if not positions:
                return None

            cursor = min(positions)

        try:
            node = _parseTlv(data, cursor, 0, budget)

        except _ResyncExhausted:
            return None

        except _FramingError:
            cursor += 1
            continue

        if expectedTags is not None and node.tag not in expectedTags:
            cursor += 1
            continue

        if decodes >= MAX_RESYNC_DECODES:
            # The verification budget is spent.  Accept the first candidate
            # that frames cleanly rather than dropping the rest of the batch;
            # normal validation will still accept or reject it.
            return cursor

        decodes += 1

        try:
            _decodeItem(data, node, schema)

        except Exception:
            cursor += 1
            continue

        return cursor

    return None


def _decodeDiagnostic(index, start, end, raw, reason, itemStart, itemEnd):
    return Diagnostic(index, '$', DECODE, None, None, start, end, raw,
                      reason=reason, itemStart=itemStart, itemEnd=itemEnd)


def validate(substrate, asn1Spec=None):
    """Validate every top-level BER object in *substrate* against *asn1Spec*.

    The substrate is split on TLV boundaries, so a malformed or constraint
    violating item is reported and skipped rather than aborting the whole
    batch.  Objects are decoded through a constraint-free copy of *asn1Spec*
    and then checked against the original schema, which yields the complete
    field path, the responsible constraint and the original byte range.

    Parameters
    ----------
    substrate: :class:`bytes`, :class:`bytearray`, :class:`memoryview`,
        :py:class:`~pyasn1.type.univ.OctetString` or a binary file-like object
        Concatenated BER encodings, one complete object after another.

    asn1Spec: :py:class:`~pyasn1.type.base.PyAsn1Item`
        Schema guiding the decode and carrying the constraints to enforce.

    Returns
    -------
    : :class:`ValidationReport`
        Accepted objects and classified diagnostics.  Collection size
        failures are available separately from element value failures.

    Raises
    ------
    : :class:`~pyasn1.error.PyAsn1Error`
        If the substrate type is unsupported.
    """
    data = _asBytes(substrate)

    schema = None
    if asn1Spec is not None:
        schema = _relaxedSchema(asn1Spec, {}, 0)

    objects = []
    diagnostics = []
    offset = 0
    index = 0

    while offset < len(data):
        try:
            node = _parseTlv(data, offset, 0)

        except _FramingError as exc:
            declared = _declaredExtent(data, offset)

            if exc.structural and declared is not None and schema is None:
                # Without a schema every nested TLV looks like a complete
                # object, so scanning inside a damaged item would report its
                # own fragments.  The declared extent is the whole bad item.
                diagnostics.append(_decodeDiagnostic(
                    index, offset, declared, data[offset:declared], str(exc),
                    offset, declared))

                offset = declared
                index += 1
                continue

            # Scan the damaged prefix for the next complete object.  A bad
            # length can claim octets it does not own, so the whole rest of
            # the substrate is considered for ordinary framing failures; a
            # nested structural failure is bounded by the declared extent so
            # fragments of the same damage are not reported as items.
            nextOffset = _resync(data, offset + 1, schema, limit=declared)
            if nextOffset is None:
                nextOffset = declared

            if nextOffset is None:
                end = (exc.detectedEnd
                       if exc.detectedEnd is not None else offset + 1)
                end = min(end, len(data))
                end = max(end, offset + 1)

            else:
                end = min(nextOffset, len(data))

            diagnostics.append(_decodeDiagnostic(
                index, offset, end, data[offset:end], str(exc),
                offset, end))

            if nextOffset is None:
                break

            offset = nextOffset
            index += 1
            continue

        context = _ItemContext(index, node, data)

        try:
            value = _decodeItem(data, node, schema)

        except Exception as exc:
            diagnostics.append(_decodeDiagnostic(
                index, node.start, node.end, context.raw, str(exc),
                node.start, node.end))

        else:
            before = len(diagnostics)
            _validateValue(value, asn1Spec, '$', node, context, diagnostics)

            if len(diagnostics) == before:
                if asn1Spec is not None:
                    # The relaxed copy exists only for diagnostics; hand back
                    # objects of the exact classes the caller supplied.
                    value = _retype(value, asn1Spec)

                objects.append(ValidationItem(
                    index, value, node.start, node.end, context.raw))

        offset = node.end
        index += 1

    return ValidationReport(objects, diagnostics)


#: Backwards/ergonomically friendly aliases.
validateBatch = validate
validate_batch = validate
