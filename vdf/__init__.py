"""
Module for deserializing/serializing to and from VDF
"""
__version__ = "3.4"
__author__ = "Rossen Georgiev"

import re
import sys
import struct
from binascii import crc32
from io import BytesIO
from io import StringIO as unicodeIO

try:
    from collections.abc import Mapping
except:
    from collections import Mapping

from vdf.vdict import VDFDict

# Py2 & Py3 compatibility
if sys.version_info[0] >= 3:
    string_type = str
    int_type = int
    BOMS = '\ufffe\ufeff'

    def strip_bom(line):
        return line.lstrip(BOMS)
else:
    from StringIO import StringIO as strIO
    string_type = basestring
    int_type = long
    BOMS = '\xef\xbb\xbf\xff\xfe\xfe\xff'
    BOMS_UNICODE = '\\ufffe\\ufeff'.decode('unicode-escape')

    def strip_bom(line):
        return line.lstrip(BOMS if isinstance(line, str) else BOMS_UNICODE)

# string escaping
_unescape_char_map = {
    r"\n": "\n",
    r"\t": "\t",
    r"\v": "\v",
    r"\b": "\b",
    r"\r": "\r",
    r"\f": "\f",
    r"\a": "\a",
    r"\\": "\\",
    r"\?": "?",
    r"\"": "\"",
    r"\'": "\'",
}
_escape_char_map = {v: k for k, v in _unescape_char_map.items()}

def _re_escape_match(m):
    return _escape_char_map[m.group()]

def _re_unescape_match(m):
    return _unescape_char_map[m.group()]

def _escape(text):
    return re.sub(r"[\n\t\v\b\r\f\a\\\?\"']", _re_escape_match, text)

def _unescape(text):
    return re.sub(r"(\\n|\\t|\\v|\\b|\\r|\\f|\\a|\\\\|\\\?|\\\"|\\')", _re_unescape_match, text)

# parsing and dumping for KV1
def parse(fp, mapper=dict, merge_duplicate_keys=True, escaped=True):
    """
    Deserialize ``s`` (a ``str`` or ``unicode`` instance containing a VDF)
    to a Python object.

    ``mapper`` specifies the Python object used after deserializetion. ``dict` is
    used by default. Alternatively, ``collections.OrderedDict`` can be used if you
    wish to preserve key order. Or any object that acts like a ``dict``.

    ``merge_duplicate_keys`` when ``True`` will merge multiple KeyValue lists with the
    same key into one instead of overwriting. You can se this to ``False`` if you are
    using ``VDFDict`` and need to preserve the duplicates.
    """
    if not issubclass(mapper, Mapping):
        raise TypeError("Expected mapper to be subclass of dict, got %s" % type(mapper))
    if not hasattr(fp, 'readline'):
        raise TypeError("Expected fp to be a file-like object supporting line iteration")

    stack = [mapper()]
    expect_bracket = False

    re_keyvalue = re.compile(r'^("(?P<qkey>(?:\\.|[^\\"])*)"|(?P<key>#?[a-z0-9\-\_\\\?$%<>]+))'
                             r'([ \t]*('
                             r'"(?P<qval>(?:\\.|[^\\"])*)(?P<vq_end>")?'
                             r'|(?P<val>(?:(?<!/)/(?!/)|[a-z0-9\-\_\\\?\*\.$<> ])+)'
                             r'|(?P<sblock>{[ \t]*)(?P<eblock>})?'
                             r'))?',
                             flags=re.I)

    for lineno, line in enumerate(fp, 1):
        if lineno == 1:
            line = strip_bom(line)

        line = line.lstrip()

        # skip empty and comment lines
        if line == "" or line[0] == '/':
            continue

        # one level deeper
        if line[0] == "{":
            expect_bracket = False
            continue

        if expect_bracket:
            raise SyntaxError("vdf.parse: expected openning bracket",
                              (getattr(fp, 'name', '<%s>' % fp.__class__.__name__), lineno, 1, line))

        # one level back
        if line[0] == "}":
            if len(stack) > 1:
                stack.pop()
                continue

            raise SyntaxError("vdf.parse: one too many closing parenthasis",
                              (getattr(fp, 'name', '<%s>' % fp.__class__.__name__), lineno, 0, line))

        # parse keyvalue pairs
        while True:
            match = re_keyvalue.match(line)

            if not match:
                try:
                    line += next(fp)
                    continue
                except StopIteration:
                    raise SyntaxError("vdf.parse: unexpected EOF (open key quote?)",
                                      (getattr(fp, 'name', '<%s>' % fp.__class__.__name__), lineno, 0, line))

            key = match.group('key') if match.group('qkey') is None else match.group('qkey')
            val = match.group('qval')
            if val is None:
                val = match.group('val')
                if val is not None:
                    val = val.rstrip()
                    if val == "":
                        val = None

            if escaped:
                key = _unescape(key)

            # we have a key with value in parenthesis, so we make a new dict obj (level deeper)
            if val is None:
                if merge_duplicate_keys and key in stack[-1]:
                    _m = stack[-1][key]
                    # we've descended a level deeper, if value is str, we have to overwrite it to mapper
                    if not isinstance(_m, mapper):
                        _m = stack[-1][key] = mapper()
                else:
                    _m = mapper()
                    stack[-1][key] = _m

                if match.group('eblock') is None:
                    # only expect a bracket if it's not already closed or on the same line
                    stack.append(_m)
                    if match.group('sblock') is None:
                        expect_bracket = True

            # we've matched a simple keyvalue pair, map it to the last dict obj in the stack
            else:
                # if the value is line consume one more line and try to match again,
                # until we get the KeyValue pair
                if match.group('vq_end') is None and match.group('qval') is not None:
                    try:
                        line += next(fp)
                        continue
                    except StopIteration:
                        raise SyntaxError("vdf.parse: unexpected EOF (open quote for value?)",
                                          (getattr(fp, 'name', '<%s>' % fp.__class__.__name__), lineno, 0, line))

                stack[-1][key] = _unescape(val) if escaped else val

            # exit the loop
            break

    if len(stack) != 1:
        raise SyntaxError("vdf.parse: unclosed parenthasis or quotes (EOF)",
                           (getattr(fp, 'name', '<%s>' % fp.__class__.__name__), lineno, 0, line))

    return stack.pop()


def loads(s, **kwargs):
    """
    Deserialize ``s`` (a ``str`` or ``unicode`` instance containing a JSON
    document) to a Python object.
    """
    if not isinstance(s, string_type):
        raise TypeError("Expected s to be a str, got %s" % type(s))

    try:
        fp = unicodeIO(s)
    except TypeError:
        fp = strIO(s)

    return parse(fp, **kwargs)


def load(fp, **kwargs):
    """
    Deserialize ``fp`` (a ``.readline()``-supporting file-like object containing
    a JSON document) to a Python object.
    """
    return parse(fp, **kwargs)


def dumps(obj, pretty=False, escaped=True):
    """
    Serialize ``obj`` to a VDF formatted ``str``.
    """
    if not isinstance(obj, Mapping):
        raise TypeError("Expected data to be an instance of``dict``")
    if not isinstance(pretty, bool):
        raise TypeError("Expected pretty to be of type bool")
    if not isinstance(escaped, bool):
        raise TypeError("Expected escaped to be of type bool")

    return ''.join(_dump_gen(obj, pretty, escaped))


def dump(obj, fp, pretty=False, escaped=True):
    """
    Serialize ``obj`` as a VDF formatted stream to ``fp`` (a
    ``.write()``-supporting file-like object).
    """
    if not isinstance(obj, Mapping):
        raise TypeError("Expected data to be an instance of``dict``")
    if not hasattr(fp, 'write'):
        raise TypeError("Expected fp to have write() method")
    if not isinstance(pretty, bool):
        raise TypeError("Expected pretty to be of type bool")
    if not isinstance(escaped, bool):
        raise TypeError("Expected escaped to be of type bool")

    for chunk in _dump_gen(obj, pretty, escaped):
        fp.write(chunk)


def _dump_gen(data, pretty=False, escaped=True, level=0):
    indent = "\t"
    line_indent = ""

    if pretty:
        line_indent = indent * level

    for key, value in data.items():
        if escaped and isinstance(key, string_type):
            key = _escape(key)

        if isinstance(value, Mapping):
            yield '%s"%s"\n%s{\n' % (line_indent, key, line_indent)
            for chunk in _dump_gen(value, pretty, escaped, level+1):
                yield chunk
            yield "%s}\n" % line_indent
        else:
            if escaped and isinstance(value, string_type):
                value = _escape(value)

            yield '%s"%s" "%s"\n' % (line_indent, key, value)


# binary VDF
class BASE_INT(int_type):
    def __repr__(self):
        return "%s(%d)" % (self.__class__.__name__, self)

class UINT_64(BASE_INT):
    pass

class INT_64(BASE_INT):
    pass

class POINTER(BASE_INT):
    pass

class COLOR(BASE_INT):
    pass

BIN_NONE        = b'\x00'
BIN_STRING      = b'\x01'
BIN_INT32       = b'\x02'
BIN_FLOAT32     = b'\x03'
BIN_POINTER     = b'\x04'
BIN_WIDESTRING  = b'\x05'
BIN_COLOR       = b'\x06'
BIN_UINT64      = b'\x07'
BIN_END         = b'\x08'
BIN_INT64       = b'\x0A'
BIN_END_ALT     = b'\x0B'

def binary_loads(b, mapper=dict, merge_duplicate_keys=True, alt_format=False, raise_on_remaining=True):
    """
    Deserialize ``b`` (``bytes`` containing a VDF in "binary form")
    to a Python object.

    ``mapper`` specifies the Python object used after deserializetion. ``dict` is
    used by default. Alternatively, ``collections.OrderedDict`` can be used if you
    wish to preserve key order. Or any object that acts like a ``dict``.

    ``merge_duplicate_keys`` when ``True`` will merge multiple KeyValue lists with the
    same key into one instead of overwriting. You can se this to ``False`` if you are
    using ``VDFDict`` and need to preserve the duplicates.
    """
    if not isinstance(b, bytes):
        raise TypeError("Expected s to be bytes, got %s" % type(b))

    return binary_load(BytesIO(b), mapper, merge_duplicate_keys, alt_format, raise_on_remaining)

def binary_load(fp, mapper=dict, merge_duplicate_keys=True, alt_format=False, raise_on_remaining=False):
    """
    Deserialize ``fp`` (a ``.read()``-supporting file-like object containing
    binary VDF) to a Python object.

    ``mapper`` specifies the Python object used after deserializetion. ``dict` is
    used by default. Alternatively, ``collections.OrderedDict`` can be used if you
    wish to preserve key order. Or any object that acts like a ``dict``.

    ``merge_duplicate_keys`` when ``True`` will merge multiple KeyValue lists with the
    same key into one instead of overwriting. You can se this to ``False`` if you are
    using ``VDFDict`` and need to preserve the duplicates.
    """
    if not hasattr(fp, 'read') or not hasattr(fp, 'tell') or not hasattr(fp, 'seek'):
        raise TypeError("Expected fp to be a file-like object with tell()/seek() and read() returning bytes")
    if not issubclass(mapper, Mapping):
        raise TypeError("Expected mapper to be subclass of dict, got %s" % type(mapper))

    # helpers
    int32 = struct.Struct('<i')
    uint64 = struct.Struct('<Q')
    int64 = struct.Struct('<q')
    float32 = struct.Struct('<f')

    def read_string(fp, wide=False):
        buf, end = b'', -1
        offset = fp.tell()

        # locate string end
        while end == -1:
            chunk = fp.read(64)

            if chunk == b'':
                raise SyntaxError("Unterminated cstring (offset: %d)" % offset)

            buf += chunk
            end = buf.find(b'\x00\x00' if wide else b'\x00')

        if wide:
            end += end % 2

        # rewind fp
        fp.seek(end - len(buf) + (2 if wide else 1), 1)

        # decode string
        result = buf[:end]

        if wide:
            result = result.decode('utf-16')
        elif bytes is not str:
            result = result.decode('utf-8', 'replace')
        else:
            try:
                result.decode('ascii')
            except:
                result = result.decode('utf-8', 'replace')

        return result

    stack = [mapper()]
    CURRENT_BIN_END = BIN_END if not alt_format else BIN_END_ALT

    for t in iter(lambda: fp.read(1), b''):
        if t == CURRENT_BIN_END:
            if len(stack) > 1:
                stack.pop()
                continue
            break

        key = read_string(fp)

        if t == BIN_NONE:
            if merge_duplicate_keys and key in stack[-1]:
                _m = stack[-1][key]
            else:
                _m = mapper()
                stack[-1][key] = _m
            stack.append(_m)
        elif t == BIN_STRING:
            stack[-1][key] = read_string(fp)
        elif t == BIN_WIDESTRING:
            stack[-1][key] = read_string(fp, wide=True)
        elif t in (BIN_INT32, BIN_POINTER, BIN_COLOR):
            val = int32.unpack(fp.read(int32.size))[0]

            if t == BIN_POINTER:
                val = POINTER(val)
            elif t == BIN_COLOR:
                val = COLOR(val)

            stack[-1][key] = val
        elif t == BIN_UINT64:
            stack[-1][key] = UINT_64(uint64.unpack(fp.read(int64.size))[0])
        elif t == BIN_INT64:
            stack[-1][key] = INT_64(int64.unpack(fp.read(int64.size))[0])
        elif t == BIN_FLOAT32:
            stack[-1][key] = float32.unpack(fp.read(float32.size))[0]
        else:
            raise SyntaxError("Unknown data type at offset %d: %s" % (fp.tell() - 1, repr(t)))

    if len(stack) != 1:
        raise SyntaxError("Reached EOF, but Binary VDF is incomplete")
    if raise_on_remaining and fp.read(1) != b'':
        fp.seek(-1, 1)
        raise SyntaxError("Binary VDF ended at offset %d, but there is more data remaining" % (fp.tell() - 1))

    return stack.pop()


# per-object reading of concatenated binary VDF streams
class BinaryVDFRecord(tuple):
    """
    A single binary VDF object read from a stream.

    The record unpacks as ``(data, alt_format, consumed)`` and additionally
    exposes ``variant``, ``length`` and ``end_marker`` properties. ``offset`` is
    the position of the object in the source stream. Fields are also readable
    by name, e.g. ``record['consumed']``.
    """
    _fields = ('data', 'alt_format', 'consumed')

    def __new__(cls, data, alt_format, consumed, offset=None):
        self = tuple.__new__(cls, (data, alt_format, consumed))
        self.offset = offset
        return self

    def __getitem__(self, item):
        if isinstance(item, string_type):
            if item in self._fields:
                return tuple.__getitem__(self, self._fields.index(item))
            if item == 'variant':
                return self.variant
            if item in ('length', 'size'):
                return self[2]
            if item in ('end_marker', 'end'):
                return self.end_marker
            if item == 'offset':
                return self.offset
            raise KeyError(item)
        return tuple.__getitem__(self, item)

    def get(self, item, default=None):
        try:
            return self[item]
        except KeyError:
            return default

    def keys(self):
        return self._fields + ('variant', 'length', 'end_marker', 'offset')

    def items(self):
        return [(key, self[key]) for key in self.keys()]

    @property
    def data(self):
        return self[0]

    @property
    def alt_format(self):
        return self[1]

    @property
    def consumed(self):
        return self[2]

    @property
    def length(self):
        return self[2]

    @property
    def variant(self):
        return 'alt' if self[1] else 'base'

    @property
    def end_marker(self):
        return BIN_END_ALT if self[1] else BIN_END

    def __repr__(self):
        return "BinaryVDFRecord(data=%r, alt_format=%r, consumed=%r, offset=%r)" % (
            self[0], self[1], self[2], self.offset)


class TruncatedBinaryVDF(SyntaxError):
    """
    Raised when the stream ends in the middle of a binary VDF object.

    The stream is rewound to ``offset`` (the start of the incomplete object)
    before this exception is raised, so the object remains available as an
    unconsumed tail. ``consumed`` is the number of bytes inspected from the
    start of the unfinished object.
    """
    def __init__(self, offset, consumed=0, tail=None):
        SyntaxError.__init__(
            self,
            "Binary VDF object at offset %d is truncated after %d byte(s)" % (offset, consumed))
        self.offset = offset
        self.consumed = consumed
        self.tail = tail


BinaryVDFTruncated = TruncatedBinaryVDF
TruncatedVDF = TruncatedBinaryVDF
IncompleteBinaryVDF = TruncatedBinaryVDF


class _BinaryVDFEOF(Exception):
    pass


def _binary_object_reader(fp, mapper, merge_duplicate_keys, alt_format):
    int32 = struct.Struct('<i')
    uint64 = struct.Struct('<Q')
    int64 = struct.Struct('<q')
    float32 = struct.Struct('<f')

    def read_exact(size):
        buf = b''

        while len(buf) < size:
            chunk = fp.read(size - len(buf))

            if chunk == b'':
                raise _BinaryVDFEOF()

            buf += chunk

        return buf

    def read_type():
        t = fp.read(1)

        if t == b'':
            raise _BinaryVDFEOF()

        return t

    def read_cstring(wide=False):
        buf, end = b'', -1

        while end == -1:
            chunk = fp.read(64)

            if chunk == b'':
                raise _BinaryVDFEOF()

            buf += chunk
            end = buf.find(b'\x00\x00' if wide else b'\x00')

        if wide:
            end += end % 2

        fp.seek(-(len(buf) - end) + (2 if wide else 1), 1)

        result = buf[:end]

        if wide:
            return result.decode('utf-16')
        if bytes is not str:
            return result.decode('utf-8', 'replace')

        try:
            result.decode('ascii')
        except:
            return result.decode('utf-8', 'replace')
        return result

    stack = [mapper()]
    expected_end = None if alt_format is None else (BIN_END_ALT if alt_format else BIN_END)

    while True:
        t = read_type()

        if t in (BIN_END, BIN_END_ALT):
            if len(stack) > 1:
                stack.pop()
                continue

            if expected_end is not None and t != expected_end:
                raise SyntaxError(
                    "Expected binary VDF end marker %s at offset %d, got %s"
                    % (repr(expected_end), fp.tell() - 1, repr(t)))

            return stack.pop(), t == BIN_END_ALT

        key = read_cstring()

        if t == BIN_NONE:
            if merge_duplicate_keys and key in stack[-1]:
                _m = stack[-1][key]
            else:
                _m = mapper()
                stack[-1][key] = _m
            stack.append(_m)
        elif t == BIN_STRING:
            stack[-1][key] = read_cstring()
        elif t == BIN_WIDESTRING:
            stack[-1][key] = read_cstring(wide=True)
        elif t in (BIN_INT32, BIN_POINTER, BIN_COLOR):
            val = int32.unpack(read_exact(int32.size))[0]

            if t == BIN_POINTER:
                val = POINTER(val)
            elif t == BIN_COLOR:
                val = COLOR(val)

            stack[-1][key] = val
        elif t == BIN_UINT64:
            stack[-1][key] = UINT_64(uint64.unpack(read_exact(uint64.size))[0])
        elif t == BIN_INT64:
            stack[-1][key] = INT_64(int64.unpack(read_exact(int64.size))[0])
        elif t == BIN_FLOAT32:
            stack[-1][key] = float32.unpack(read_exact(float32.size))[0]
        else:
            raise SyntaxError("Unknown data type at offset %d: %s" % (fp.tell() - 1, repr(t)))


def _load_one(fp, mapper, merge_duplicate_keys, alt_format, source):
    start = fp.tell()

    if fp.read(1) == b'':
        return None
    fp.seek(start)

    try:
        result = _binary_object_reader(fp, mapper, merge_duplicate_keys, alt_format)
    except _BinaryVDFEOF:
        consumed = fp.tell() - start
        fp.seek(start)
        tail = None
        if source is not None:
            tail = source[start:]
        raise TruncatedBinaryVDF(start, consumed, tail)

    data, object_alt_format = result
    return BinaryVDFRecord(data, object_alt_format, fp.tell() - start, start)


def _stream_source(fp):
    """Return ``(stream, source_bytes_or_None)`` for a bytes or file-like input."""
    if isinstance(fp, bytes):
        return BytesIO(fp), fp
    if not hasattr(fp, 'read') or not hasattr(fp, 'tell') or not hasattr(fp, 'seek'):
        raise TypeError("Expected fp to be a file-like object with tell()/seek() and read() returning bytes")
    return fp, None


def _check_mapper(mapper):
    if not issubclass(mapper, Mapping):
        raise TypeError("Expected mapper to be subclass of dict, got %s" % type(mapper))


def binary_load_object(fp, mapper=dict, merge_duplicate_keys=True, alt_format=None, offset=0):
    """
    Read exactly one binary VDF object from ``fp``.

    The end marker is detected per object when ``alt_format`` is ``None``, so
    ordinary (``0x08``) and alternate (``0x0B``) objects may be concatenated
    in one stream. End markers belonging to nested sections only close that
    section; they never terminate the object.

    Returns a :class:`BinaryVDFRecord` carrying the parsed ``data``, the
    detected ``alt_format`` and the exact number of bytes ``consumed``. The
    stream is left immediately after the object's own top-level end marker.

    Returns ``None`` at a clean end of stream. If the stream ends inside an
    object, :class:`TruncatedBinaryVDF` is raised and the stream is rewound to
    the object's first byte, leaving the incomplete object unconsumed.

    ``fp`` may also be ``bytes``; in that case ``offset`` selects the first
    byte of the object and the truncated tail is available on the exception.
    """
    _check_mapper(mapper)

    stream, source = _stream_source(fp)
    if source is not None:
        stream.seek(offset)
    return _load_one(stream, mapper, merge_duplicate_keys, alt_format, source)


def binary_load_next(fp, offset=0, mapper=dict, merge_duplicate_keys=True, alt_format=None):
    """
    Read the next binary VDF object from a ``bytes`` buffer or file-like object.

    Alias for :func:`binary_load_object`. For ``bytes`` sources, ``offset``
    selects the first byte of the object.
    """
    return binary_load_object(fp, mapper=mapper, merge_duplicate_keys=merge_duplicate_keys,
                              alt_format=alt_format, offset=offset)


def _binary_load_stream(stream, mapper, merge_duplicate_keys, alt_format, source, on_truncated):
    while True:
        start = stream.tell()
        try:
            record = _load_one(stream, mapper, merge_duplicate_keys, alt_format, source)
        except TruncatedBinaryVDF:
            if on_truncated == 'raise':
                raise
            stream.seek(start)
            return
        if record is None:
            return
        yield record


def binary_load_stream(fp, mapper=dict, merge_duplicate_keys=True, alt_format=None, on_truncated='stop'):
    """
    Iterate over consecutive binary VDF objects in ``fp``.

    Each item is a :class:`BinaryVDFRecord`. The end variant is detected
    per object unless ``alt_format`` is given. Reading stops at a clean end of
    stream, or when an incomplete object is found; in the latter case the
    incomplete object's bytes are left unconsumed at the current position.

    Set ``on_truncated='raise'`` to propagate :class:`TruncatedBinaryVDF`
    instead of stopping. ``fp`` may also be ``bytes``.
    """
    if on_truncated not in ('stop', 'raise'):
        raise ValueError("Expected on_truncated to be 'stop' or 'raise'")
    _check_mapper(mapper)
    stream, source = _stream_source(fp)

    return _binary_load_stream(stream, mapper, merge_duplicate_keys, alt_format, source, on_truncated)


def binary_load_iter(fp, mapper=dict, merge_duplicate_keys=True, alt_format=None):
    """Alias for :func:`binary_load_stream`."""
    return binary_load_stream(fp, mapper, merge_duplicate_keys, alt_format)


class BinaryVDFStream(object):
    """
    Resumable reader for a stream of consecutive binary VDF objects.

    ``BinaryVDFStream`` accepts a file-like object or a ``bytes`` buffer.
    ``read()`` returns the next :class:`BinaryVDFRecord`, ``None`` at a clean
    end of stream, and raises :class:`TruncatedBinaryVDF` (leaving the
    incomplete object at the current position) when more data is required.

    For ``bytes``/``BytesIO`` sources, :meth:`feed` appends additional bytes
    without disturbing the current position. Iterating the stream stops at
    truncation; after feeding the remaining bytes, the next iteration yields
    only the object that was previously incomplete.
    """
    def __init__(self, fp, mapper=dict, merge_duplicate_keys=True, alt_format=None):
        _check_mapper(mapper)
        if isinstance(fp, bytes):
            self.source = fp
            self.fp = BytesIO(fp)
        else:
            if not hasattr(fp, 'read') or not hasattr(fp, 'tell') or not hasattr(fp, 'seek'):
                raise TypeError("Expected fp to be a file-like object with tell()/seek() and read() returning bytes")
            self.source = None
            self.fp = fp
        self.mapper = mapper
        self.merge_duplicate_keys = merge_duplicate_keys
        self.alt_format = alt_format

    def read(self):
        return binary_load_object(
            self.fp, mapper=self.mapper, merge_duplicate_keys=self.merge_duplicate_keys,
            alt_format=self.alt_format)

    def __iter__(self):
        while True:
            start = self.fp.tell()
            try:
                record = self.read()
            except TruncatedBinaryVDF:
                self.fp.seek(start)
                return
            if record is None:
                return
            yield record

    def __next__(self):
        record = self.read()
        if record is None:
            raise StopIteration
        return record

    next = __next__

    def feed(self, data):
        """Append ``data`` to a ``bytes``/``BytesIO`` backed stream."""
        if not isinstance(data, bytes):
            raise TypeError("Expected data to be bytes, got %s" % type(data))
        if not isinstance(self.fp, BytesIO):
            raise TypeError("feed() requires a bytes or BytesIO source")

        position = self.fp.tell()
        self.fp.seek(0, 2)
        self.fp.write(data)
        self.fp.seek(position)

    @property
    def position(self):
        return self.fp.tell()

    @property
    def remaining(self):
        """Bytes after the current position (available for ``bytes`` sources)."""
        if isinstance(self.fp, BytesIO):
            position = self.fp.tell()
            self.fp.seek(0, 2)
            end = self.fp.tell()
            self.fp.seek(position)
            return end - position
        return None

def binary_dumps(obj, alt_format=False):
    """
    Serialize ``obj`` to a binary VDF formatted ``bytes``.
    """
    buf = BytesIO()
    binary_dump(obj, buf, alt_format)
    return buf.getvalue()

def binary_dump(obj, fp, alt_format=False):
    """
    Serialize ``obj`` to a binary VDF formatted ``bytes`` and write it to ``fp`` filelike object
    """
    if not isinstance(obj, Mapping):
        raise TypeError("Expected obj to be type of Mapping")
    if not hasattr(fp, 'write'):
        raise TypeError("Expected fp to have write() method")

    for chunk in _binary_dump_gen(obj, alt_format=alt_format):
        fp.write(chunk)

def _binary_dump_gen(obj, level=0, alt_format=False):
    if level == 0 and len(obj) == 0:
        return

    int32 = struct.Struct('<i')
    uint64 = struct.Struct('<Q')
    int64 = struct.Struct('<q')
    float32 = struct.Struct('<f')

    for key, value in obj.items():
        if isinstance(key, string_type):
            key = key.encode('utf-8')
        else:
            raise TypeError("dict keys must be of type str, got %s" % type(key))

        if isinstance(value, Mapping):
            yield BIN_NONE + key + BIN_NONE
            for chunk in _binary_dump_gen(value, level+1, alt_format=alt_format):
                yield chunk
        elif isinstance(value, UINT_64):
            yield BIN_UINT64 + key + BIN_NONE + uint64.pack(value)
        elif isinstance(value, INT_64):
            yield BIN_INT64 + key + BIN_NONE + int64.pack(value)
        elif isinstance(value, string_type):
            try:
                value = value.encode('utf-8') + BIN_NONE
                yield BIN_STRING
            except:
                value = value.encode('utf-16') + BIN_NONE*2
                yield BIN_WIDESTRING
            yield key + BIN_NONE + value
        elif isinstance(value, float):
            yield BIN_FLOAT32 + key + BIN_NONE + float32.pack(value)
        elif isinstance(value, (COLOR, POINTER, int, int_type)):
            if isinstance(value, COLOR):
                yield BIN_COLOR
            elif isinstance(value, POINTER):
                yield BIN_POINTER
            else:
                yield BIN_INT32
            yield key + BIN_NONE
            yield int32.pack(value)
        else:
            raise TypeError("Unsupported type: %s" % type(value))

    yield BIN_END if not alt_format else BIN_END_ALT


def vbkv_loads(s, mapper=dict, merge_duplicate_keys=True):
    """
    Deserialize ``s`` (``bytes`` containing a VBKV to a Python object.

    ``mapper`` specifies the Python object used after deserializetion. ``dict` is
    used by default. Alternatively, ``collections.OrderedDict`` can be used if you
    wish to preserve key order. Or any object that acts like a ``dict``.

    ``merge_duplicate_keys`` when ``True`` will merge multiple KeyValue lists with the
    same key into one instead of overwriting. You can se this to ``False`` if you are
    using ``VDFDict`` and need to preserve the duplicates.
    """
    if s[:4] != b'VBKV':
        raise ValueError("Invalid header")

    checksum, = struct.unpack('<i', s[4:8])

    if checksum != crc32(s[8:]):
        raise ValueError("Invalid checksum")

    return binary_loads(s[8:], mapper, merge_duplicate_keys, alt_format=True)

def vbkv_dumps(obj):
    """
    Serialize ``obj`` to a VBKV formatted ``bytes``.
    """
    data =  b''.join(_binary_dump_gen(obj, alt_format=True))
    checksum = crc32(data)

    return b'VBKV' + struct.pack('<i', checksum) + data
