#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
import sys

from pyasn1 import error
from pyasn1.type import tag
from pyasn1.type import univ

__all__ = ['NumericString', 'PrintableString', 'TeletexString', 'T61String', 'VideotexString',
           'IA5String', 'GraphicString', 'VisibleString', 'ISO646String',
           'GeneralString', 'UniversalString', 'BMPString', 'UTF8String',
           'CharacterField', 'CharacterFieldIndex', 'CharacterFieldSet',
           'MergedCharacterField', 'CharacterFieldError',
           'CharacterRepertoireError', 'QuarantinedCharacterFieldError',
           'decodeCharacterField', 'mergeCharacterFields',
           'migrateCharacterField', 'getCharacterRepertoire']

NoValue = univ.NoValue
noValue = univ.noValue


class AbstractCharacterString(univ.OctetString):
    """Creates |ASN.1| schema or value object.

    |ASN.1| class is based on :class:`~pyasn1.type.base.SimpleAsn1Type`,
    its objects are immutable and duck-type :class:`bytes`.
    When used in octet-stream context, |ASN.1| type assumes
    "|encoding|" encoding.

    Keyword Args
    ------------
    value: :class:`str`, :class:`bytes` or |ASN.1| object
        :class:`str`, alternatively :class:`bytes`
        representing octet-stream of serialised unicode string
        (note `encoding` parameter) or |ASN.1| class instance.
        If `value` is not given, schema object will be created.

    tagSet: :py:class:`~pyasn1.type.tag.TagSet`
        Object representing non-default ASN.1 tag(s)

    subtypeSpec: :py:class:`~pyasn1.type.constraint.ConstraintsIntersection`
        Object representing non-default ASN.1 subtype constraint(s). Constraints
        verification for |ASN.1| type occurs automatically on object
        instantiation.

    encoding: :py:class:`str`
        Unicode codec ID to encode/decode
        :class:`str` the payload when |ASN.1| object is used
        in octet-stream context.

    Raises
    ------
    ~pyasn1.error.ValueConstraintError, ~pyasn1.error.PyAsn1Error
        On constraint violation or bad initializer.
    """

    def __str__(self):
        return str(self._value)

    def __bytes__(self):
        try:
            return self._value.encode(self.encoding)
        except UnicodeEncodeError as exc:
            raise error.PyAsn1UnicodeEncodeError(
                "Can't encode string '%s' with codec "
                "%s" % (self._value, self.encoding), exc
            )

    def prettyIn(self, value):
        try:
            if isinstance(value, str):
                return value
            elif isinstance(value, bytes):
                return value.decode(self.encoding)
            elif isinstance(value, (tuple, list)):
                return self.prettyIn(bytes(value))
            elif isinstance(value, univ.OctetString):
                return value.asOctets().decode(self.encoding)
            else:
                return str(value)

        except (UnicodeDecodeError, LookupError) as exc:
            raise error.PyAsn1UnicodeDecodeError(
                "Can't decode string '%s' with codec "
                "%s" % (value, self.encoding), exc
            )

    def asOctets(self, padding=True):
        return bytes(self)

    def asNumbers(self, padding=True):
        return tuple(bytes(self))

    #
    # See OctetString.prettyPrint() for the explanation
    #

    def prettyOut(self, value):
        return value

    def prettyPrint(self, scope=0):
        # first see if subclass has its own .prettyOut()
        value = self.prettyOut(self._value)

        if value is not self._value:
            return value

        return AbstractCharacterString.__str__(self)

    def __reversed__(self):
        return reversed(self._value)


class NumericString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 18)
    )
    encoding = 'us-ascii'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class PrintableString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 19)
    )
    encoding = 'us-ascii'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class TeletexString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 20)
    )
    encoding = 'iso-8859-1'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class T61String(TeletexString):
    __doc__ = TeletexString.__doc__

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class VideotexString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 21)
    )
    encoding = 'iso-8859-1'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class IA5String(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 22)
    )
    encoding = 'us-ascii'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class GraphicString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 25)
    )
    encoding = 'iso-8859-1'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class VisibleString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 26)
    )
    encoding = 'us-ascii'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class ISO646String(VisibleString):
    __doc__ = VisibleString.__doc__

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()

class GeneralString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 27)
    )
    encoding = 'iso-8859-1'

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class UniversalString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 28)
    )
    encoding = "utf-32-be"

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class BMPString(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 30)
    )
    encoding = "utf-16-be"

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


class UTF8String(AbstractCharacterString):
    __doc__ = AbstractCharacterString.__doc__

    #: Set (on class, not on instance) or return a
    #: :py:class:`~pyasn1.type.tag.TagSet` object representing ASN.1 tag(s)
    #: associated with |ASN.1| type.
    tagSet = AbstractCharacterString.tagSet.tagImplicitly(
        tag.Tag(tag.tagClassUniversal, tag.tagFormatSimple, 12)
    )
    encoding = "utf-8"

    # Optimization for faster codec lookup
    typeId = AbstractCharacterString.getTypeId()


#: ASN.1 character types and the repertoire their declaration carries.
#:
#: ``NumericString``, ``PrintableString``, ``IA5String``,
#: ``VisibleString``/``ISO646String``, ``BMPString`` and the
#: ``UTF8String``/``UniversalString`` pair use the sets X.680 defines.
#: The remaining legacy types have no single codec-independent definition,
#: so their repertoire is what their pyasn1 codec (``iso-8859-1``) can
#: round-trip.
_REPERTOIRES = {
    NumericString: 'numeric',
    PrintableString: 'printable',
    IA5String: 'ia5',
    VisibleString: 'visible',
    ISO646String: 'visible',
    GraphicString: 'latin-1',
    GeneralString: 'latin-1',
    TeletexString: 'latin-1',
    T61String: 'latin-1',
    VideotexString: 'latin-1',
    UTF8String: 'unicode',
    BMPString: 'bmp',
    UniversalString: 'unicode',
}

#: The characters of PRINTABLE STRING (X.680 Table 9).
_PRINTABLE_CHARACTERS = frozenset(
    ' ' + ''.join(chr(x) for x in range(0x30, 0x3a)) +
    ''.join(chr(x) for x in range(0x41, 0x5b)) +
    ''.join(chr(x) for x in range(0x61, 0x7b)) +
    "'()+,-./:=?"
)


class CharacterFieldError(error.PyAsn1Error):
    """Base class for character-field provenance errors."""


class CharacterRepertoireError(CharacterFieldError):
    """A text value can not be represented in the target character type."""


class QuarantinedCharacterFieldError(CharacterFieldError):
    """An operation requiring decoded text hit a quarantined value."""


def _describeError(exc):
    """Return a stable, readable description of a quarantine reason."""
    if exc is None:
        return None

    message = str(exc)

    if not message and isinstance(exc, error.PyAsn1UnicodeError) and exc._args:
        message = str(exc._args[0])

    if message:
        return '%s: %s' % (exc.__class__.__name__, message)

    return '%s: %r' % (exc.__class__.__name__, exc.args)


def _characterSpec(asn1Type):
    """Return a schema object for a character type class or instance."""
    if isinstance(asn1Type, AbstractCharacterString):
        return asn1Type

    if isinstance(asn1Type, type) and issubclass(
            asn1Type, AbstractCharacterString):
        return asn1Type()

    raise CharacterFieldError(
        'Expected a restricted character string type, got %r' % (asn1Type,))


def _characterType(asn1Type):
    """Return the concrete character type class for a class or instance."""
    if isinstance(asn1Type, type):
        if issubclass(asn1Type, AbstractCharacterString):
            return asn1Type

    elif isinstance(asn1Type, AbstractCharacterString):
        return asn1Type.__class__

    raise CharacterFieldError(
        'Expected a restricted character string type, got %r' % (asn1Type,))


def getCharacterRepertoire(asn1Type):
    """Return the repertoire name an ASN.1 character type accepts.

    Parameters
    ----------
    asn1Type: type or :py:class:`~pyasn1.type.base.Asn1Type`
        Character string class or instance.

    Returns
    -------
    : :py:class:`str`
        One of ``numeric``, ``printable``, ``ia5``, ``visible``,
        ``latin-1``, ``bmp`` or ``unicode``.

    Raises
    ------
    ~pyasn1.error.PyAsn1Error
        If *asn1Type* is not a known restricted character string type.
    """
    asn1Type = _characterType(asn1Type)

    for candidate in asn1Type.__mro__:
        try:
            return _REPERTOIRES[candidate]

        except KeyError:
            continue

    raise CharacterFieldError(
        '%s has no character repertoire' % (asn1Type.__name__,))


def _checkRepertoire(text, asn1Type):
    """Reject *text* characters outside the declaration's repertoire."""
    repertoire = getCharacterRepertoire(asn1Type)

    if repertoire == 'unicode':
        try:
            text.encode('utf-8')

        except UnicodeEncodeError as exc:
            raise CharacterRepertoireError(
                "Character string %r can not be represented in %s: %s"
                % (text, _characterType(asn1Type).__name__, exc)
            )

        return

    for index, character in enumerate(text):
        code = ord(character)

        if repertoire == 'numeric':
            permitted = character in ' 0123456789'

        elif repertoire == 'printable':
            permitted = character in _PRINTABLE_CHARACTERS

        elif repertoire == 'ia5':
            permitted = code < 0x80

        elif repertoire == 'visible':
            permitted = 0x20 <= code < 0x7f

        elif repertoire == 'latin-1':
            permitted = code < 0x100

        elif repertoire == 'bmp':
            permitted = code < 0x10000 and not 0xd800 <= code <= 0xdfff

        else:
            raise CharacterFieldError(
                'Unknown character repertoire %r' % (repertoire,))

        if not permitted:
            raise CharacterRepertoireError(
                "Character %r at position %d is outside the %s repertoire of %s"
                % (character, index, repertoire,
                   _characterType(asn1Type).__name__)
            )


class CharacterField(object):
    """A character string value with the provenance needed to migrate it.

    The field remembers, side by side:

    * the declared ASN.1 character type (``asn1Type``),
    * the exact wire octets (``octets``),
    * the standard text decoded from those octets (``text``), or ``None``
      while the value is quarantined,
    * an optional ``source`` label distinguishing where the field came from
      (for example ``'old'`` and ``'new'`` for the two sides of a migration).

    ``CharacterField`` never substitutes replacement characters. A value that
    does not decode under its declared type, or whose text falls outside that
    type's repertoire, is retained octet-for-octet and marked quarantined
    through :attr:`quarantine`; :meth:`getText` then raises
    :class:`QuarantinedCharacterFieldError` instead of returning mangled data.

    Parameters
    ----------
    asn1Type: type or :py:class:`~pyasn1.type.base.Asn1Type`
        The declared restricted character string type.
    octets: :class:`bytes`, :class:`bytearray` or :class:`memoryview`
        The exact payload read from the source.
    text: :class:`str` or :py:const:`None`
        The decoded standard text, or ``None`` while quarantined.
    source: :class:`str`
        Optional label identifying the originating store.
    quarantine: :class:`Exception`
        The reason the value was isolated.
    asn1Spec: :py:class:`~pyasn1.type.base.Asn1Type`
        Optional constrained schema object to rebuild the value with.

    Raises
    ------
    ~pyasn1.error.PyAsn1Error
        If *asn1Type* is not a restricted character string type, or if
        neither *text* nor *quarantine* is supplied.
    """

    def __init__(self, asn1Type, octets, text, source=None, quarantine=None,
                 asn1Spec=None):
        self.asn1Type = _characterType(asn1Type)
        self.asn1Spec = asn1Spec

        if not isinstance(octets, (bytes, bytearray, memoryview)):
            raise CharacterFieldError(
                'Expected bytes-like octets, got %r' % (octets,))

        if text is not None and not isinstance(text, str):
            raise CharacterFieldError(
                'Expected str standard text, got %r' % (text,))

        if quarantine is not None and text is not None:
            raise CharacterFieldError(
                'A quarantined value can not carry standard text')

        if quarantine is None and text is None:
            raise CharacterFieldError(
                '%s value carries neither standard text nor a quarantine '
                'reason' % self.asn1Type.__name__)

        if quarantine is not None and not isinstance(quarantine, BaseException):
            raise CharacterFieldError(
                'Expected an exception as quarantine reason, got %r'
                % (quarantine,))

        if asn1Spec is not None:
            if not isinstance(asn1Spec, AbstractCharacterString):
                raise CharacterFieldError(
                    'Expected a character string schema, got %r' % (asn1Spec,))

            if asn1Spec.__class__ is not self.asn1Type:
                raise CharacterFieldError(
                    'Schema %s does not match declared type %s'
                    % (asn1Spec.__class__.__name__, self.asn1Type.__name__))

        self.octets = bytes(octets)
        self.text = text
        self.source = source
        self.quarantine = quarantine

    def __eq__(self, other):
        if isinstance(other, CharacterField):
            return (self.asn1Type is other.asn1Type and
                    self.octets == other.octets and
                    self.text == other.text and
                    self.source == other.source)

        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)

        if result is NotImplemented:
            return result

        return not result

    def __hash__(self):
        return hash((self.asn1Type, self.octets, self.text, self.source))

    def __repr__(self):
        provenance = ' from %s' % (self.source,) if self.source is not None else ''

        if self.isQuarantined:
            return '<%s %s%s quarantined %s>' % (
                self.__class__.__name__, self.asn1Type.__name__, provenance,
                ' '.join('%02x' % x for x in self.octets))

        return '<%s %s%s %r>' % (
            self.__class__.__name__, self.asn1Type.__name__, provenance,
            self.text)

    @property
    def isQuarantined(self):
        """Return :obj:`True` if the wire octets could not be decoded."""
        return self.quarantine is not None

    @property
    def kind(self):
        """Return the declared ASN.1 character type name."""
        return self.asn1Type.__name__

    @property
    def originalType(self):
        """Return the declared ASN.1 character type object."""
        return self.asn1Type

    @property
    def originalOctets(self):
        """Return the exact wire octets, byte-for-byte."""
        return self.octets

    @property
    def standardText(self):
        """Return the standard text, or ``None`` while quarantined."""
        return self.text

    @property
    def repertoire(self):
        """Return the repertoire name of the declared ASN.1 type."""
        return getCharacterRepertoire(self.asn1Type)

    def getText(self):
        """Return the standard text, refusing quarantined values.

        Raises
        ------
        ~pyasn1.type.char.QuarantinedCharacterFieldError
            If the field is quarantined.
        """
        if self.isQuarantined:
            raise QuarantinedCharacterFieldError(
                '%s value is quarantined, no standard text available: %s'
                % (self.asn1Type.__name__, _describeError(self.quarantine)))

        return self.text

    def getOctets(self):
        """Return the exact octets this field was read from."""
        return self.octets

    def asRecord(self):
        """Return the cleansing-exit representation of this field.

        The record carries the declared type, the original octets and the
        standard text side by side, so a downstream store can retain all
        three without inferring any of them. A quarantined field carries
        ``text=None`` and the originating error instead of a substituted
        string. ``octetsHex`` is the same payload in a form suitable for
        JSON or a text column.

        Returns
        -------
        : :py:class:`dict`
            Keys: ``type``, ``asn1Type``, ``octets``, ``octetsHex``,
            ``text``, ``source``, ``quarantined``, ``error`` and
            ``errorMessage``.
        """
        return {
            'type': self.asn1Type.__name__,
            'asn1Type': self.asn1Type,
            'octets': self.octets,
            'octetsHex': self.octets.hex(),
            'text': self.text,
            'source': self.source,
            'quarantined': self.isQuarantined,
            'error': self.quarantine,
            'errorMessage': _describeError(self.quarantine),
        }

    def asJsonRecord(self):
        """Return :meth:`asRecord` with JSON-serialisable values only.

        The original type is the class name, and the octets appear as
        ``octetsHex``; the raw exception object is replaced by its
        ``errorMessage``. The four-state shape (type, octets, text,
        quarantine) is otherwise unchanged.
        """
        return {
            'type': self.asn1Type.__name__,
            'octetsHex': self.octets.hex(),
            'text': self.text,
            'source': self.source,
            'quarantined': self.isQuarantined,
            'errorMessage': _describeError(self.quarantine),
        }

    def asAsn1(self):
        """Rebuild the original ASN.1 value from the exact wire octets.

        Only a clean field can be rebuilt: a quarantined field is isolated
        by design, and its payload is reachable through :meth:`getOctets`
        without pretending it decodes.

        Raises
        ------
        ~pyasn1.type.char.QuarantinedCharacterFieldError
            If the field is quarantined.
        """
        if self.isQuarantined:
            raise QuarantinedCharacterFieldError(
                '%s value is quarantined, no ASN.1 object available: %s'
                % (self.asn1Type.__name__, _describeError(self.quarantine)))

        if self.asn1Spec is not None:
            return self.asn1Spec.clone(self.octets)

        value = self.asn1Type(self.octets)

        if str(value) != self.text:
            raise CharacterFieldError(
                'Octets no longer decode to the recorded standard text')

        return value

    def migrateTo(self, asn1Type):
        """Re-type this field, explicitly rejecting out-of-repertoire text.

        The wire octets are re-encoded from the standard text under the
        target type's codec. A quarantined value, or text the target
        repertoire can not carry, raises :class:`CharacterRepertoireError`;
        the original field is left untouched so the caller can quarantine it.
        Re-typing to the declaring type returns the field unchanged, keeping
        the original octets byte-for-byte.
        """
        targetSpec = _characterSpec(asn1Type)
        targetType = targetSpec.__class__

        if self.isQuarantined:
            raise CharacterRepertoireError(
                "Can not migrate quarantined %s value to %s: %s"
                % (self.asn1Type.__name__, targetType.__name__,
                   _describeError(self.quarantine)))

        text = self.getText()

        if targetType is self.asn1Type and isinstance(asn1Type, type):
            return self

        if targetType is self.asn1Type:
            return CharacterField(targetType, self.octets, text, self.source,
                                  asn1Spec=targetSpec)

        _checkRepertoire(text, targetType)

        try:
            value = targetSpec.clone(text)

        except error.PyAsn1UnicodeEncodeError as exc:
            raise CharacterRepertoireError(
                "Character string %r can not be represented in %s: %s"
                % (text, targetType.__name__, exc)
            )

        return CharacterField(targetType, value.asOctets(), text, self.source,
                              asn1Spec=targetSpec)


class CharacterFieldIndex(object):
    """Look up character fields by their originating ASN.1 type.

    A merged entity can carry the same logical text from more than one
    source. This index keeps every contributed field, grouped by ASN.1 type,
    so a caller can retrieve the field -- and therefore the original octets
    -- from each origin.
    """

    def __init__(self, fields=()):
        self._byType = {}

        for field in fields:
            self.add(field)

    def add(self, field):
        """Add *field*, keeping insertion order within its type."""
        if not isinstance(field, CharacterField):
            raise CharacterFieldError(
                'Expected CharacterField, got %r' % (field,))

        self._byType.setdefault(field.asn1Type, []).append(field)

    def get(self, asn1Type):
        """Return the first field of *asn1Type*, or ``None``.

        *asn1Type* may be a class or an instance.
        """
        fields = self._byType.get(_characterType(asn1Type))

        if fields:
            return fields[0]

        return None

    def getAll(self, asn1Type):
        """Return every field of *asn1Type*, in insertion order."""
        return tuple(self._byType.get(_characterType(asn1Type), ()))

    def getByKind(self, name):
        """Return every field whose ASN.1 type name is *name*."""
        result = []

        for asn1Type, fields in self._byType.items():
            if asn1Type.__name__ == name:
                result.extend(fields)

        return tuple(result)

    def getBySource(self, source):
        """Return every field carrying the *source* label.

        This is how a caller reaches both sides of a cross-source merge:
        ``getBySource('old')`` and ``getBySource('new')`` return the fields
        read from each origin, with their original types and octets.
        """
        return tuple(field for field in self if field.source == source)

    @property
    def sourceLabels(self):
        """Return the distinct source labels in insertion order."""
        labels = []

        for field in self:
            if field.source not in labels:
                labels.append(field.source)

        return tuple(labels)

    def __iter__(self):
        for fields in self._byType.values():
            for field in fields:
                yield field

    def __len__(self):
        return sum(len(fields) for fields in self._byType.values())

    def __repr__(self):
        return '<%s %s>' % (
            self.__class__.__name__,
            ', '.join('%s=%d' % (asn1Type.__name__, len(fields))
                      for asn1Type, fields in self._byType.items()))


class CharacterFieldSet(object):
    """The cleansing exit: clean and quarantined character fields together.

    A set keeps every field exactly as it was read, and partitions the
    result into :attr:`clean` and :attr:`quarantined`. This is the
    collection form of :meth:`CharacterField.asRecord`: nothing is dropped
    and no undecodable value is replaced, so an operator can export the
    clean rows and route the quarantined ones for review by source label.
    """

    def __init__(self, fields=()):
        self._fields = []

        for field in fields:
            self.add(field)

    def add(self, field):
        """Add *field* to the set."""
        if not isinstance(field, CharacterField):
            raise CharacterFieldError(
                'Expected CharacterField, got %r' % (field,))

        self._fields.append(field)

    def getBySource(self, source):
        """Return every field carrying the *source* label."""
        return tuple(field for field in self._fields
                     if field.source == source)

    @property
    def clean(self):
        """Return the fields that decoded to standard text."""
        return tuple(field for field in self._fields
                     if not field.isQuarantined)

    @property
    def quarantined(self):
        """Return the fields isolated without substitution."""
        return tuple(field for field in self._fields
                     if field.isQuarantined)

    @property
    def sourceLabels(self):
        """Return the distinct source labels in insertion order."""
        labels = []

        for field in self._fields:
            if field.source not in labels:
                labels.append(field.source)

        return tuple(labels)

    def asRecords(self):
        """Return one cleansing record per field, in insertion order."""
        return tuple(field.asRecord() for field in self._fields)

    def asJsonRecords(self):
        """Return one JSON-serialisable record per field."""
        return tuple(field.asJsonRecord() for field in self._fields)

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)

    def __contains__(self, field):
        return field in self._fields

    def __repr__(self):
        return '<%s %d clean, %d quarantined>' % (
            self.__class__.__name__, len(self.clean), len(self.quarantined))


class MergedCharacterField(object):
    """Text identity merged across one or more source character fields.

    ``text`` is the standard text used for merging. ``sources`` is a
    :class:`CharacterFieldIndex` retaining the original type, octets and
    source label of each contributing field, so the pre-merge representation
    stays recoverable from either side.
    """

    def __init__(self, text, sources):
        self.text = text
        self.sources = sources

    def __eq__(self, other):
        if isinstance(other, MergedCharacterField):
            return self.text == other.text

        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)

        if result is NotImplemented:
            return result

        return not result

    def __hash__(self):
        return hash(self.text)

    def __repr__(self):
        return '<%s %r from %s>' % (
            self.__class__.__name__, self.text,
            ', '.join('%s=%d' % (asn1Type.__name__, len(fields))
                      for asn1Type, fields in self.sources._byType.items()))

    def getField(self, asn1Type):
        """Return the merged text as a field of *asn1Type*, or ``None``.

        The returned field is rebuilt from the merged text, not from any
        single source; use :meth:`getSource` to reach the original octets.
        Returns ``None`` when no source of that type contributed, so a caller
        can never conjure a provenance that did not exist.
        """
        if self.sources.get(asn1Type) is None:
            return None

        _checkRepertoire(self.text, asn1Type)

        spec = _characterSpec(asn1Type)

        return CharacterField(spec.__class__, spec.clone(self.text).asOctets(),
                              self.text, asn1Spec=spec)

    def getSource(self, asn1Type):
        """Return the first original field of *asn1Type*, or ``None``."""
        return self.sources.get(asn1Type)

    def getSources(self, asn1Type):
        """Return every original field of *asn1Type*, in insertion order."""
        return self.sources.getAll(asn1Type)

    def getBySource(self, source):
        """Return every original field read from *source*."""
        return self.sources.getBySource(source)

    @property
    def sourceLabels(self):
        """Return the distinct source labels in insertion order."""
        return self.sources.sourceLabels

    def migrateTo(self, asn1Type):
        """Migrate the merged text to *asn1Type*, rejecting overflow.

        The target repertoire is checked before anything is encoded, so
        migrating a value merged from ``UTF8String`` back into a restricted
        source type either yields a field of that type or raises
        :class:`CharacterRepertoireError`; it is never truncated.
        """
        spec = _characterSpec(asn1Type)
        targetType = spec.__class__

        _checkRepertoire(self.text, targetType)

        try:
            value = spec.clone(self.text)

        except error.PyAsn1UnicodeEncodeError as exc:
            raise CharacterRepertoireError(
                "Character string %r can not be represented in %s: %s"
                % (self.text, targetType.__name__, exc)
            )

        return CharacterField(targetType, value.asOctets(), self.text,
                              asn1Spec=spec)

    def asRecord(self):
        """Return the merged text together with every source record."""
        return {
            'text': self.text,
            'sourceLabels': self.sourceLabels,
            'sources': tuple(field.asRecord() for field in self.sources),
        }


def decodeCharacterField(asn1Type, octets, source=None):
    """Read *octets* as *asn1Type*, quarantining instead of substituting.

    Parameters
    ----------
    asn1Type: type or :py:class:`~pyasn1.type.base.Asn1Type`
        The declared restricted character string type. A schema instance is
        honoured too, so its constraints and tags survive a rebuild.
    octets: :class:`bytes`, :class:`bytearray`, :class:`memoryview` or
        :class:`str`
        Payload read from the source. A :class:`str` is treated as
        already-decoded text and encoded with the declared type's codec.
    source: :class:`str`
        Optional label identifying where the field came from, typically
        ``'old'`` or ``'new'`` for the two sides of a migration.

    Returns
    -------
    : :class:`CharacterField`
        Either a decodable field carrying the declared type, the exact
        octets and the standard text, or a quarantined field carrying the
        type and octets plus the reason it was isolated. Values whose text
        falls outside the declared repertoire are quarantined as well: they
        decoded, but the declaration can not legally represent them.

    Raises
    ------
    ~pyasn1.error.PyAsn1Error
        If *asn1Type* is not a restricted character string type, if *octets*
        is not bytes-like or text, or if the payload violates the type's
        size constraint.
    """
    spec = _characterSpec(asn1Type)
    asn1Type = spec.__class__

    if isinstance(octets, str):
        try:
            octets = octets.encode(spec.encoding)

        except UnicodeEncodeError as exc:
            raise CharacterRepertoireError(
                "Character string %r can not be represented in %s: %s"
                % (octets, asn1Type.__name__, exc)
            )

    elif not isinstance(octets, (bytes, bytearray, memoryview)):
        raise CharacterFieldError(
            'Expected bytes-like or str payload, got %r' % (octets,))

    octets = bytes(octets)

    try:
        value = spec.clone(octets)

    except error.PyAsn1UnicodeDecodeError as exc:
        return CharacterField(asn1Type, octets, None, source, exc, spec)

    text = str(value)

    try:
        _checkRepertoire(text, asn1Type)

    except CharacterRepertoireError as exc:
        return CharacterField(asn1Type, octets, None, source, exc, spec)

    return CharacterField(asn1Type, octets, text, source, asn1Spec=spec)


def mergeCharacterFields(*fields):
    """Merge character fields by text while retaining every source.

    Fields are merged on their standard text, so a value read from a
    restricted type and the same logical value read from
    :class:`UTF8String` collapse into one entity. The returned object keeps
    a :class:`CharacterFieldIndex` of the original fields, letting callers
    look the value up by either type or source label.

    A single iterable argument is accepted for convenience, so both
    ``mergeCharacterFields([old, new])`` and
    ``mergeCharacterFields(old, new)`` work; the iterable may be a generator.

    Raises
    ------
    ~pyasn1.type.char.QuarantinedCharacterFieldError
        If any field is quarantined: undecodable values have no text
        identity to merge on and must be handled explicitly.
    ~pyasn1.type.char.CharacterFieldError
        If the fields disagree on text, or no field is supplied.
    """
    if len(fields) == 1 and not isinstance(fields[0], CharacterField):
        try:
            fields = tuple(fields[0])

        except TypeError:
            raise CharacterFieldError(
                'Expected a CharacterField or an iterable of them, got %r'
                % (fields[0],))

    if not fields:
        raise CharacterFieldError('No character fields to merge')

    for field in fields:
        if not isinstance(field, CharacterField):
            raise CharacterFieldError(
                'Expected CharacterField, got %r' % (field,))

        if field.isQuarantined:
            raise QuarantinedCharacterFieldError(
                'Can not merge quarantined %s value: %s'
                % (field.asn1Type.__name__, _describeError(field.quarantine)))

    text = fields[0].getText()

    for field in fields[1:]:
        if field.getText() != text:
            raise CharacterFieldError(
                'Can not merge fields with different text: %r != %r'
                % (text, field.getText()))

    return MergedCharacterField(text, CharacterFieldIndex(fields))


def migrateCharacterField(field, asn1Type):
    """Migrate a field back to a target ASN.1 character type.

    This is :meth:`CharacterField.migrateTo` as a free function, for callers
    that keep their fields in plain data structures. Text the target type
    can not carry -- or any quarantined value -- is rejected with
    :class:`CharacterRepertoireError`; it is never truncated or replaced.
    """
    if not isinstance(field, CharacterField):
        raise CharacterFieldError(
            'Expected CharacterField, got %r' % (field,))

    return field.migrateTo(asn1Type)
