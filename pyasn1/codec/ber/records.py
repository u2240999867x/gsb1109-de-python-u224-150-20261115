#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""Incremental framing of a stream of BER records.

The BER decoder consumes exactly one ASN.1 object when handed a complete
encoding.  This module adds the layer a collection channel needs: bytes may
arrive in arbitrary chunks, records are framed without decoding partial
values, and a consumer can confirm how far it has processed so that a
reconnect resumes from that position.

Framing is separate from decoding.  A record is reported only after its
outermost tag, length and complete payload have arrived.  An indefinite
length container is not complete until its own end-of-contents octets have
arrived, so its outer record cannot be acknowledged before every nested
indefinite container is closed.

Example
-------
::

    decoder = RecordDecoder(asn1Spec=MyRecord())

    decoder.feed(chunk)                 # may split tag, length or value
    for record in decoder:
        process(record.asn1Object)
        decoder.acknowledge(record.end)

    # After a reconnect, resend the unacknowledged portion.  Overlapping
    # bytes are checked and skipped, so the same record is not delivered
    # twice.
    decoder.feed(retransmittedChunk, offset=decoder.acknowledged)
"""
import collections
import sys

from pyasn1 import error
from pyasn1.codec.ber import decoder
from pyasn1.type import univ

__all__ = ['Record', 'RecordDecoder', 'StreamingRecordDecoder',
           'BerRecordDecoder', 'decodeRecords']


class Record(collections.namedtuple('Record', 'asn1Object start end')):
    """One complete BER record and the byte range it consumed.

    The range is absolute: ``start`` is the offset of the first identifier
    octet in the input stream and ``end`` is the offset just past the last
    payload octet.  The two offsets are stable across acknowledgements and
    retransmissions.
    """

    __slots__ = ()

    @property
    def object(self):
        return self.asn1Object

    @property
    def value(self):
        return self.asn1Object

    @property
    def offset(self):
        return self.start

    @property
    def consumed(self):
        return self.end - self.start

    @property
    def length(self):
        return self.end - self.start

    @property
    def nextOffset(self):
        return self.end

    @property
    def byteRange(self):
        return self.start, self.end

    @property
    def range(self):
        return self.start, self.end


class RecordDecoder(object):
    """Frame a continuous stream of BER records.

    Parameters
    ----------
    substrate: :py:class:`bytes` or :py:class:`bytearray` or
               :py:class:`memoryview` or :py:class:`~pyasn1.type.univ.OctetString`
        Optional bytes to feed immediately.

    asn1Spec: :py:class:`~pyasn1.type.base.PyAsn1Item`
        Optional schema used to decode each framed record.

    offset: :py:class:`int`
        Absolute offset of the first octet that will be fed.  Use the
        acknowledged position when constructing a decoder for a reconnected
        channel so that record ranges stay absolute across connections.

    Keyword Args
    ------------
    Any other keyword argument is forwarded to
    :py:func:`pyasn1.codec.ber.decoder.decode`.

    Notes
    -----
    Feeding and delivery are independent.  :meth:`feed` only appends bytes;
    :meth:`acknowledge` only discards bytes that a consumer has confirmed.
    Records that have been framed but not yet delivered stay in an internal
    queue, so feeding a retransmitted block never creates a second record.
    """

    def __init__(self, substrate=None, asn1Spec=None, offset=0, **options):
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError('BER record offset must be an integer')

        if offset < 0:
            raise ValueError('BER record offset must not be negative')

        self._asn1Spec = asn1Spec
        self._options = options

        # Received but not acknowledged input, addressed by absolute offset.
        self._buffer = bytearray()
        self._base = offset         # absolute offset of _buffer[0]
        self._end = offset          # absolute offset just past received data

        # Acknowledgements and delivery progress.
        self._ack = offset          # last offset confirmed by the consumer
        self._next = offset         # start offset of the next delivered record
        self._boundaries = set((offset,))  # delivered record ends

        # Framing state for the record currently being scanned.
        self._recordStart = offset   # start offset of the current record
        self._scanCursor = offset    # absolute offset of next octet to scan
        self._scanStack = []         # (indefinite, contentEnd or None) frames
        self._framedRecords = collections.deque()

        if substrate is not None:
            self.feed(substrate)

    @property
    def acknowledged(self):
        """Absolute position last confirmed by the consumer."""
        return self._ack

    @property
    def position(self):
        """Start offset of the next record to deliver."""
        return self._next

    @property
    def received(self):
        """Absolute position just past the last received octet."""
        return self._end

    @property
    def pending(self):
        """Number of received but not yet delivered octets."""
        return self._end - self._next

    @property
    def unacknowledged(self):
        """Number of received but not yet acknowledged octets."""
        return self._end - self._ack

    @property
    def pendingRecords(self):
        """Number of framed records not yet delivered."""
        return len(self._framedRecords)

    @property
    def hasPendingRecords(self):
        """Whether at least one framed record is waiting for delivery."""
        return bool(self._framedRecords)

    def __len__(self):
        return len(self._framedRecords)

    def feed(self, data, offset=None):
        """Add received bytes to the decoder.

        Parameters
        ----------
        data: :py:class:`bytes` or :py:class:`bytearray` or
              :py:class:`memoryview` or :py:class:`~pyasn1.type.univ.OctetString`
            Newly received bytes.

        offset: :py:class:`int`
            Absolute offset of the first octet in `data`.  Omit it for
            ordinary in-order delivery.  Supplying it makes retransmission
            safe: bytes at or before the acknowledged position are ignored,
            and bytes overlapping already received unacknowledged input are
            compared and skipped rather than appended again.

        Returns
        -------
        : :py:class:`RecordDecoder`
            This decoder, to allow chaining.
        """
        if isinstance(data, univ.OctetString):
            data = data.asOctets()

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError('BER record data must be bytes-like')

        data = bytes(data)

        if offset is None:
            offset = self._end

        else:
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise TypeError('BER record offset must be an integer')

            if offset < 0:
                raise ValueError('BER record offset must not be negative')

            if offset < self._ack:
                # Data from an already confirmed prefix is redundant.
                skip = self._ack - offset
                if len(data) <= skip:
                    self._verifyOverlap(offset, data)
                    return self
                data = data[skip:]
                offset = self._ack

            if offset < self._end:
                overlap = self._end - offset
                if len(data) < overlap:
                    self._verifyOverlap(offset, data)
                    return self
                self._verifyOverlap(offset, data[:overlap])
                data = data[overlap:]
                offset = self._end

            elif offset > self._end:
                raise error.PyAsn1Error(
                    'Cannot feed BER record data at offset %d, expected %d'
                    % (offset, self._end)
                )

        if data:
            self._buffer.extend(data)
            self._end += len(data)
            self._frameRecords()

        return self

    def records(self):
        """Iterate over complete records not delivered yet."""
        while self._framedRecords:
            yield self._deliver()

    def replay(self, data, offset=None):
        """Feed a block that may begin before the received position.

        This is :meth:`feed` with `offset` defaulting to :attr:`acknowledged`,
        which is the natural entry point after a reconnect: the sender
        replays from the last confirmed record boundary, and the overlapping
        prefix (including a repeated last unacknowledged block) is verified
        and skipped.
        """
        if offset is None:
            offset = self._ack

        return self.feed(data, offset=offset)

    resend = replay

    def read(self):
        """Return the next complete record, or `None` if none is ready."""
        if not self._framedRecords:
            return None
        return self._deliver()

    def readAll(self):
        """Return all currently complete records as a list."""
        return list(self.records())

    def __iter__(self):
        return self.records()

    def acknowledge(self, offset=None):
        """Confirm that everything before `offset` has been consumed.

        Only a record boundary that has actually been delivered can be
        confirmed.  In particular, an indefinite length container is not a
        valid boundary until its end-of-contents octets have arrived, since
        no record end exists before that point.

        Parameters
        ----------
        offset: :py:class:`int`
            Absolute offset to confirm.  Defaults to the end of the last
            delivered record.

        Returns
        -------
        : :py:class:`int`
            The confirmed offset.
        """
        if offset is None:
            offset = self._next

        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError('BER record offset must be an integer')

        if offset < self._ack:
            raise error.PyAsn1Error(
                'Cannot acknowledge offset %d before current position %d'
                % (offset, self._ack)
            )

        if offset > self._next:
            raise error.PyAsn1Error(
                'Cannot acknowledge offset %d beyond last delivered record %d'
                % (offset, self._next)
            )

        if offset != self._ack and offset not in self._boundaries:
            raise error.PyAsn1Error(
                'Offset %d is not a complete BER record boundary' % (offset,)
            )

        if offset == self._ack:
            return offset

        dropped = offset - self._base
        del self._buffer[:dropped]
        self._base = offset
        self._ack = offset
        self._boundaries = set(
            boundary for boundary in self._boundaries if boundary > offset
        )

        return offset

    # `ack` is a convenient spelling for the common case.
    ack = acknowledge

    def confirm(self, offset=None):
        """Confirm consumption up to `offset`; alias of :meth:`acknowledge`."""
        return self.acknowledge(offset)

    def acknowledgeRecord(self, record):
        """Confirm that `record` and everything before it were consumed."""
        return self.acknowledge(record.end)

    @property
    def consumed(self):
        """Absolute position last confirmed by the consumer."""
        return self._ack

    @property
    def consumedRange(self):
        """Acknowledged byte range as ``(0, acknowledged)``."""
        return 0, self._ack

    def _verifyOverlap(self, offset, data):
        """Check retransmitted bytes against input we already hold."""
        if not data:
            return

        first = max(offset, self._base)
        last = min(offset + len(data), self._end)

        if first >= last:
            return

        existing = bytes(self._buffer[first - self._base:last - self._base])
        proposed = bytes(data[first - offset:last - offset])

        if existing != proposed:
            raise error.PyAsn1Error(
                'Conflicting retransmission at offset %d' % (first,)
            )

    def _deliver(self):
        start, end = self._framedRecords[0]

        payload = bytes(self._buffer[start - self._base:end - self._base])

        asn1Object, tail = decoder.decode(
            payload, asn1Spec=self._asn1Spec, **self._options)

        if tail:
            raise error.PyAsn1Error(
                'BER record decoder left %d octets unprocessed at offset %d'
                % (len(tail), end - len(tail))
            )

        self._framedRecords.popleft()
        self._next = end
        self._boundaries.add(end)

        return Record(asn1Object, start, end)

    def _byte(self, position):
        if position < self._base:
            return None

        index = position - self._base
        if index >= len(self._buffer):
            return None

        return self._buffer[index]

    def _parseHeader(self, position):
        firstOctet = self._byte(position)

        if firstOctet is None:
            return None

        cursor = position + 1
        tagClass = firstOctet & 0xC0
        tagFormat = firstOctet & 0x20
        tagId = firstOctet & 0x1F

        if tagId == 0x1F:
            tagId = 0
            tagOctetCount = 0

            while True:
                integerByte = self._byte(cursor)

                if integerByte is None:
                    return None

                cursor += 1
                tagOctetCount += 1

                if tagOctetCount > decoder.MAX_TAG_OCTETS:
                    raise error.PyAsn1Error(
                        'Tag ID octet count exceeds limit (%d)' % (
                            decoder.MAX_TAG_OCTETS,)
                    )

                tagId = (tagId << 7) | (integerByte & 0x7F)

                if not integerByte & 0x80:
                    break

        firstLengthOctet = self._byte(cursor)

        if firstLengthOctet is None:
            return None

        cursor += 1

        if firstLengthOctet < 128:
            indefinite = False
            length = firstLengthOctet

        elif firstLengthOctet == 128:
            indefinite = True
            length = -1

        else:
            size = firstLengthOctet & 0x7F

            if size > decoder.MAX_LENGTH_OCTETS:
                raise error.PyAsn1Error(
                    'BER length field size %d exceeds limit (%d)' % (
                        size, decoder.MAX_LENGTH_OCTETS)
                )

            if cursor + size > self._end:
                return None

            length = int.from_bytes(
                bytes(self._buffer[cursor - self._base:
                                   cursor + size - self._base]),
                'big'
            )

            if length > sys.maxsize:
                raise error.PyAsn1Error(
                    'Length %d exceeds maximum readable size %d' % (
                        length, sys.maxsize)
                )

            cursor += size
            indefinite = False

        return tagClass, tagId, tagFormat, indefinite, length, cursor

    def _pushFrame(self, indefinite, contentEnd):
        if len(self._scanStack) >= decoder.MAX_NESTING_DEPTH:
            raise error.PyAsn1Error(
                'ASN.1 structure nesting depth exceeds limit (%d)' %
                decoder.MAX_NESTING_DEPTH
            )

        self._scanStack.append((indefinite, contentEnd))

    def _enclosingEnd(self):
        """End of the innermost definite-length enclosure, if any."""
        enclosingEnd = None

        for indefinite, contentEnd in self._scanStack:
            if contentEnd is not None:
                if enclosingEnd is None or contentEnd < enclosingEnd:
                    enclosingEnd = contentEnd

        return enclosingEnd

    def _checkWithinEnclosure(self, end):
        enclosingEnd = self._enclosingEnd()

        if enclosingEnd is not None and end > enclosingEnd:
            raise error.PyAsn1Error(
                'ASN.1 element exceeds enclosing definite length'
            )

    def _frameRecords(self):
        while True:
            if not self._scanOneRecord():
                return

            end = self._scanCursor
            self._framedRecords.append((self._recordStart, end))
            self._recordStart = end

    def _scanOneRecord(self):
        """Advance the scanner; return True when one record is framed."""
        while True:
            if self._scanStack:
                indefinite, contentEnd = self._scanStack[-1]

                if contentEnd is not None:
                    if self._scanCursor == contentEnd:
                        self._scanStack.pop()
                        if not self._scanStack:
                            return True
                        continue

                    if self._scanCursor > contentEnd:
                        raise error.PyAsn1Error(
                            'ASN.1 element exceeds enclosing definite length'
                        )

                else:
                    firstOctet = self._byte(self._scanCursor)

                    if firstOctet is None:
                        return False

                    if not firstOctet:
                        secondOctet = self._byte(self._scanCursor + 1)

                        if secondOctet is None:
                            return False

                        if not secondOctet:
                            self._checkWithinEnclosure(self._scanCursor + 2)
                            self._scanCursor += 2
                            self._scanStack.pop()

                            if not self._scanStack:
                                return True

                            continue

                        raise error.PyAsn1Error(
                            'Malformed end-of-octets octets'
                        )

            header = self._parseHeader(self._scanCursor)

            if header is None:
                return False

            tagClass, tagId, tagFormat, indefinite, length, contentStart = header

            if not tagClass and not tagId:
                raise error.PyAsn1Error(
                    'Unexpected end-of-octets octets'
                )

            if indefinite:
                if not tagFormat:
                    raise error.PyAsn1Error(
                        'Indefinite length encoding requires a constructed tag'
                    )

                self._checkWithinEnclosure(contentStart)
                self._pushFrame(True, None)
                self._scanCursor = contentStart
                continue

            contentEnd = contentStart + length

            self._checkWithinEnclosure(contentEnd)

            if tagFormat:
                self._pushFrame(False, contentEnd)
                self._scanCursor = contentStart
                continue

            if contentEnd > self._end:
                return False

            self._scanCursor = contentEnd

            if not self._scanStack:
                return True


#: Alias kept for callers that prefer a name describing the interface.
StreamingRecordDecoder = RecordDecoder
BerRecordDecoder = RecordDecoder


def decodeRecords(substrate, asn1Spec=None, **options):
    """Decode every complete BER record from a substrate.

    `substrate` may be bytes-like, a readable object, or an iterable of
    byte chunks.  The return value is a list of :class:`Record` objects.

    This helper is for finite input.  Use :class:`RecordDecoder` directly
    when records arrive over time or acknowledgements are required.
    """
    recordDecoder = RecordDecoder(asn1Spec=asn1Spec, **options)

    if isinstance(substrate, (bytes, bytearray, memoryview, univ.OctetString)):
        recordDecoder.feed(substrate)

    elif hasattr(substrate, 'read'):
        while True:
            chunk = substrate.read(65536)
            if not chunk:
                break
            recordDecoder.feed(chunk)

    else:
        for chunk in substrate:
            recordDecoder.feed(chunk)

    return recordDecoder.readAll()
