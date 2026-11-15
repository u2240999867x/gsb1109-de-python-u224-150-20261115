# Copyright (C) 2015-2022  Chris Lalancette <clalancette@gmail.com>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation;
# version 2.1 of the License.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

"""Various utilities for PyCdlib."""

import io
import math
import os
import re
import struct
import sys
import time
from typing import TYPE_CHECKING

from pycdlib import pycdlibexception

win32_has_pywin32 = False
if sys.platform == "win32":
    try:
        import win32con  # pylint: disable=import-error
        import win32file  # pylint: disable=import-error
        import winioctlcon  # pylint: disable=import-error
        win32_has_pywin32 = True
    except ImportError:
        pass

if TYPE_CHECKING:
    from typing import Any, BinaryIO, Generator, IO, List, Optional, Tuple  # noqa: F401


def swab_32bit(x):
    # type: (int) -> int
    """
    A function to swab a 32-bit integer.

    Args:
        x: The 32-bit integer to swab.
    Returns:
        The swabbed version of the 32-bit integer.
    """
    if x > (((1 << 32) - 1) & 0xFFFFFFFF) or x < 0:
        raise pycdlibexception.PyCdlibInternalError('Invalid integer passed to swab; must be unsigned 32-bits!')

    return struct.unpack("<I", struct.pack(">I", x))[0]


def swab_16bit(x):
    # type: (int) -> int
    """
    A function to swab a 16-bit integer.

    Args:
        x: The 16-bit integer to swab.
    Returns:
        The swabbed version of the 16-bit integer.
    """
    if x > (((1 << 16) - 1) & 0xFFFFFFFF) or x < 0:
        raise pycdlibexception.PyCdlibInternalError('Invalid integer passed to swab; must be unsigned 16-bits!')

    return struct.unpack("<H", struct.pack(">H", x))[0]


def ceiling_div(numer, denom):
    # type: (int, int) -> int
    """
    A function to do ceiling division; that is, dividing numerator by denominator
    and taking the ceiling.

    Args:
        numer: The numerator for the division.
        denom: The denominator for the division.
    Returns:
        The ceiling after dividing numerator by denominator.
    """
    # Doing division and then getting the ceiling is tricky; we do upside-down
    # floor division to make this happen.
    # See https://stackoverflow.com/questions/14822184/is-there-a-ceiling-equivalent-of-operator-in-python.
    return -(-numer // denom)


def copy_data_yield(data_length, blocksize, infp, outfp):
    # type: (int, int, BinaryIO, IO[Any]) -> Generator
    """
    A utility function to copy data from the input file object to the output
    file object.

    Args:
        data_length: The amount of data to copy.
        blocksize: How much data to copy per iteration.
        infp: The file object to copy data from.
        outfp: The file object to copy data to.
    Returns:
        Nothing.
    """
    left = data_length
    while left > 0:
        readsize = min(blocksize, left)
        data = infp.read(readsize)
        # We have seen ISOs in the wild (Tribes Vengeance 1of4.iso) that
        # lie about the size of their files, causing reads to fail (since
        # we hit EOF before the supposed end of the file).  If we got less data
        # than we asked for, abort the loop silently.
        data_len = len(data)
        if data_len != readsize:
            data_len = left
        outfp.write(data)
        left -= data_len
        yield data_len


def copy_data(data_length, blocksize, infp, outfp):
    # type: (int, int, BinaryIO, IO[Any]) -> None
    """
    A utility function to copy data from the input file object to the output
    file object.

    Args:
        data_length: The amount of data to copy.
        blocksize: How much data to copy per iteration.
        infp: The file object to copy data from.
        outfp: The file object to copy data to.
    Returns:
        Nothing.
    """
    for len_unused in copy_data_yield(data_length, blocksize, infp, outfp):
        pass


def encode_space_pad(instr, length, encoding):
    # type: (bytes, int, str) -> bytes
    """
    A function to pad out an input string with spaces to the length specified.
    The space is first encoded into the specified encoding, then appended to
    the input string until the length is reached.

    Args:
        instr: The input string to encode and pad.
        length: The length to pad the input string to.
        encoding: The encoding to use.
    Returns:
        The input string encoded in the encoding and padded with encoded spaces.
    """
    output = instr.decode('utf-8').encode(encoding)
    if len(output) > length:
        raise pycdlibexception.PyCdlibInvalidInput('Input string too long!')

    # Pad to `length` bytes with encoded spaces in a single concat.
    # `encoded_space` may be multi-byte (e.g. utf-16_be), so we
    # over-allocate the padding and trim to length; in practice this
    # wastes at most len(encoded_space)-1 bytes and is still vastly
    # faster than the per-character concat loop this replaced.
    encoded_space = ' '.encode(encoding)
    return (output + encoded_space * (length - len(output)))[:length]


def normpath(path):
    # type: (str) -> bytes
    """
    Normalize the given path, eliminating double slashes, etc.  This function is
    a copy of the built-in python normpath, except we do *not* allow double
    slashes at the start.

    Args:
        path: The path to normalize.
    Returns:
        The normalized path.
    """
    sep = '/'
    empty = ''
    dot = '.'
    dotdot = '..'

    if path == empty:
        return dot.encode('utf-8')

    initial_slashes = path.startswith(sep)
    comps = path.split(sep)
    new_comps = []  # type: List[str]
    for comp in comps:
        if comp in (empty, dot):
            continue
        if comp != dotdot or (not initial_slashes and not new_comps) or (new_comps and new_comps[-1] == dotdot):
            new_comps.append(comp)
        elif new_comps:
            new_comps.pop()
    newpath = sep * initial_slashes + sep.join(new_comps)
    newpath_bytes = newpath.encode('utf-8')
    if not starts_with_slash(newpath_bytes):
        raise pycdlibexception.PyCdlibInvalidInput('Must be a path starting with /')

    return newpath_bytes


def gmtoffset_from_tm(tm, localtime):
    # type: (Optional[float], time.struct_time) -> int
    """
    A function to compute the GMT offset in 15 minute intervals from the time
    in seconds since the epoch and the local time object.

    Args:
        tm: The time in seconds since the epoch.
        localtime: The struct_time object representing the local time.
    Returns:
        The GMT offset in 15 minute intervals.
    """

    # Note that this algorithm comes from cdrtools 3.01
    # mkisofs/mkisofs.c::iso9660_date

    gmtime = time.gmtime(tm)

    tmpmin = localtime.tm_min - gmtime.tm_min
    tmphour = localtime.tm_hour - gmtime.tm_hour
    tmpyday = localtime.tm_yday - gmtime.tm_yday
    tmpyear = localtime.tm_year - gmtime.tm_year
    if tmpyear:
        tmpyday = tmpyear

    return (tmpmin + 60 * (tmphour + 24 * tmpyday)) // 15


def zero_pad(fp, data_size, pad_size):
    # type: (IO[Any], int, int) -> int
    """
    A function to write padding out from data_size up to pad_size
    efficiently.

    Args:
        fp: The file object to use to write padding out to.
        data_size: The current size of the data.
        pad_size: The boundary size of data to pad out to.
    Returns:
        The number of bytes that were padded.
    """
    padbytes = pad_size - (data_size % pad_size)
    if padbytes == pad_size:
        # Nothing to pad, get out.
        return 0

    fp.seek(padbytes - 1, os.SEEK_CUR)
    fp.write(b'\x00')
    return padbytes - 1


def starts_with_slash(path):
    # type: (bytes) -> bool
    """
    A function to determine if a path starts with a slash.  This is somewhat
    difficult to do portably with performance, so we have a dedicated function
    for it.

    Args:
        path: The path to determine if it starts with a slash
    Returns:
        Whether the path starts with a slash.
    """
    return bytearray(path)[0] == 47


def split_path(iso_path):
    # type: (bytes) -> List[bytes]
    """
    A function to take a fully-qualified iso path and split it into components.

    Args:
        iso_path: The path to split.
    Returns:
        The components of the path as a list.
    """
    if not starts_with_slash(iso_path):
        raise pycdlibexception.PyCdlibInvalidInput('Must be a path starting with /')

    # Split the path along the slashes.  Since our paths are always absolute,
    # the front is blank.
    return iso_path.split(b'/')[1:]


def file_object_supports_binary(fp):
    # type: (BinaryIO) -> bool
    """
    A function to check whether a file-like object supports binary mode.

    Args:
        fp: The file-like object to check for binary mode support.
    Returns:
        True if the file-like object supports binary mode, False otherwise.
    """
    if hasattr(fp, 'mode'):
        return 'b' in fp.mode

    return isinstance(fp, (io.RawIOBase, io.BufferedIOBase))


def truncate_basename(basename, iso_level, is_dir):
    # type: (str, int, bool) -> str
    """
    A function to truncate a basename and make it conformant to the passed-in
    ISO interchange level.

    Args:
        basename: The initial basename to truncate and translate
        iso_level: The ISO interchange level to follow when truncating/translating
        is_dir: Whether this is a directory or a file
    Returns:
        The truncated and translated name suitable for the ISO interchange level
        specified.
    """
    if iso_level == 4:
        # ISO level 4 allows "anything", so just return the original.
        return basename

    if iso_level == 1:
        maxlen = 8
    else:
        maxlen = 31 if is_dir else 30

    # For performance reasons, we first truncate the string to the length
    # allowed.  Second, ISO9660 Levels 1, 2, and 3 require all uppercase names,
    # so we uppercase it.
    valid_base = basename[:maxlen].upper()

    # Finally, ISO9660 requires only uppercase letters, 0-9, and underscore.
    # Translate any non-compliant characters to underscore and return that.
    return re.sub('[^A-Z0-9_]{1}', r'_', valid_base)


def mangle_file_for_iso9660(orig, iso_level):
    # type: (str, int) -> Tuple[str, str]
    """
    A function to take a regular Unix-style filename (including extension) and
    produce a tuple consisting of an ISO9660-valid basename and an ISO9660-valid
    extension.

    Args:
        orig: The original filename
        iso_level: The ISO interchange level to conform to
    Returns:
        A tuple where the first entry is the ISO9660-compliant basename and where
        the second entry is the ISO9660-compliant extension.
    """
    # ISO9660 has a lot of restrictions on what valid names are.  Here, we mangle
    # the names to conform to those rules.  In particular, the rules for
    # filenames are:
    # 1.  Filenames can only consist of d-characters or d1-characters; these are
    #     defined in the Appendix as: 0-9A-Z_
    # 2.  Filenames look like:
    #     - zero or more d-characters (filename)
    #     - separator 1 (.)
    #     - zero or more d-characters (extension)
    #     - separate 2 (;)
    #     - version, between 0 and 32767
    # If the filename contains zero characters, then the extension must contain
    # at least one character, and vice versa.
    # 3.  If this is iso level one, then the length of the filename cannot
    #     exceed 8 and the length of the extension cannot exceed 3.  In levels 2
    #     and 3, the length of the filename+extension cannot exceed 30.
    #
    # This function takes any valid Unix filename and converts it into one that
    # is allowed by the above rules.  It does this by substituting _ for any
    # invalid characters in the filename, and by shortening the name to a form
    # of aaa_xxxx.eee;1 (if necessary).  The aaa is always the first three
    # characters of the original filename; the xxxx is the next number in a
    # sequence starting from 0.

    valid_ext = ''
    splitter = orig.split('.')
    if iso_level == 4:
        # A level 4 ISO allows 'anything', so just return the original.
        if len(splitter) == 1:
            return orig, valid_ext

        ext = splitter[-1]
        return orig[:len(orig) - len(ext) - 1], ext

    if len(splitter) == 1:
        # No extension specified, leave ext empty
        basename = orig
    else:
        ext = splitter[-1]
        basename = orig[:len(orig) - len(ext) - 1]

        # If the extension is empty, too long (> 3), or contains any illegal
        # characters, we treat it as part of the basename instead
        extlen = len(ext)
        if extlen == 0 or extlen > 3:
            valid_ext = ''
            basename = orig
        else:
            tmpext = ext.upper()
            valid_ext, numsub = re.subn('[^A-Z0-9_]{1}', r'_', tmpext)
            if numsub > 0:
                valid_ext = ''
                basename = orig

    # All right, now we have the basename of the file, and (optionally) an
    # extension.
    return truncate_basename(basename, iso_level, False), valid_ext + ';1'


def mangle_dir_for_iso9660(orig, iso_level):
    # type: (str, int) -> str
    """
    A function to take a regular Unix-style directory name and produce an
    ISO9660-valid directory name.

    Args:
        orig: The original filename
        iso_level: The ISO interchange level to conform to
    Returns:
        An ISO9660-compliant directory name.
    """
    # ISO9660 has a lot of restrictions on what valid directory names are.
    # Here, we mangle the names to conform to those rules.  In particular, the
    # rules for dirnames are:
    # 1.  Filenames can only consist of d-characters or d1-characters; these are
    #     defined in the Appendix as: 0-9A-Z_
    # 2.  If this is ISO level one, then directory names consist of no more than
    #     8 characters
    # This function takes any valid Unix directory name and converts it into one
    # that is allowed by the above rules.  It does this by substituting _ for
    # any invalid character in the directory name, and by shortening the name to
    # a form of aaaaxxx (if necessary).  The aaa is always the first three
    # characters of the original filename; the xxxx is the next number in a
    # sequence starting from 0.

    return truncate_basename(orig, iso_level, True)


# Joliet (the Microsoft SVD extension) stores identifiers as UCS-2 and limits a
# single file or directory identifier to 64 Unicode characters (128 UTF-16-BE
# bytes).
JOLIET_MAX_NAME_CHARS = 64

# The numeric suffix used to disambiguate a name once the preferred (possibly
# truncated) identifier is already taken.  It is deliberately ASCII digits, so it
# survives the ISO9660 uppercase/d-character translation and never collides with
# a hand-authored name that uses an underscore in the same position.
_NAME_CONFLICT_TEMPLATE = '_%d'


def _iso_collision_key(identifier):
    # type: (str) -> str
    """
    Reduce an ISO9660 identifier to the key used for collision detection.

    Files carry a ``;1`` version and, when there is no extension, a trailing dot,
    while directories carry neither.  Stripping both lets a file and a directory
    that would otherwise render with the same visible name share one occupied
    slot, so the two can never be assigned the same position.

    Args:
        identifier: The on-disk ISO9660 identifier.
    Returns:
        A normalized key suitable for comparing files and directories.
    """
    key = identifier
    if key.endswith(';1'):
        key = key[:-2]
    if key.endswith('.'):
        key = key[:-1]
    return key


def _iso_file_identifier(basename, ext):
    # type: (str, str) -> str
    """
    Join an ISO9660 mangled basename and extension (including the ``;1``
    version) into the exact identifier used on an ISO9660 directory record.

    Args:
        basename: The ISO9660-compliant basename.
        ext: The ISO9660-compliant extension, or the empty string.
    Returns:
        The fully-qualified ISO9660 file identifier.
    """
    if ext == '':
        return basename
    return '.'.join([basename, ext])


def mangle_file_for_joliet(orig, max_chars=JOLIET_MAX_NAME_CHARS):
    # type: (str, int) -> str
    """
    A function to take a regular Unix-style filename and produce a
    Joliet-valid basename (Joliet does not split a name into a basename and an
    extension the way ISO9660 does).

    Args:
        orig: The original filename.
        max_chars: The maximum number of Unicode characters allowed.
    Returns:
        A Joliet-compliant filename no longer than ``max_chars`` characters.
    """
    return orig[:max_chars]


def mangle_dir_for_joliet(orig, max_chars=JOLIET_MAX_NAME_CHARS):
    # type: (str, int) -> str
    """
    A function to take a regular Unix-style directory name and produce a
    Joliet-valid directory name.

    Args:
        orig: The original directory name.
        max_chars: The maximum number of Unicode characters allowed.
    Returns:
        A Joliet-compliant directory name no longer than ``max_chars``
        characters.
    """
    return orig[:max_chars]


class NameMapper:
    """
    A stable, collision-free mapper from long (Unicode) names to the paired
    identifiers required on a hybrid disc: a Joliet name and a restricted
    ISO9660 name.

    The mapper is meant to live for the duration of building one directory
    level.  Every original name is remembered forever, so:

      * the same original name always maps to the same Joliet and ISO9660
        identifiers, even if it is requested more than once;
      * adding a new batch of files never renames an entry that was mapped
        earlier, because previously assigned identifiers are never reused or
        reassigned;
      * names that only differ by case (``Foo`` versus ``foo``) or that truncate
        to the same restricted spelling are given distinct, recognizable
        numeric aliases;
      * files and directories draw from the same occupied-name pool, so a file
        and a directory can never occupy the same slot;
      * the Joliet and ISO9660 namespaces are tracked independently, since they
        are separate directory hierarchies on the disc.
    """

    # Owner recorded for identifiers pre-seeded with :meth:`seed` (for
    # instance, names that already exist on an ISO being amended).  A NUL byte
    # can never appear in a filesystem path component, so this can never match
    # a real ``(original, is_dir)`` owner token.
    _SEEDED_OWNER = ('\x00seeded', False)

    __slots__ = ('iso_level', 'joliet_max_chars', '_iso_occupied',
                 '_joliet_occupied', '_iso_by_orig', '_joliet_by_orig',
                 '_records')

    def __init__(self, iso_level, joliet_max_chars=JOLIET_MAX_NAME_CHARS):
        # type: (int, int) -> None
        self.iso_level = iso_level
        self.joliet_max_chars = joliet_max_chars
        # ISO9660 collisions are keyed through _iso_collision_key so that a
        # file and a directory with the same visible spelling share one slot.
        self._iso_occupied = {}  # type: dict
        self._joliet_occupied = {}  # type: dict
        self._iso_by_orig = {}  # type: dict
        self._joliet_by_orig = {}  # type: dict
        self._records = {}  # type: dict

    @staticmethod
    def _next_alias(base, max_len, occupied, owner, key_func):
        # type: (str, int, dict, tuple, Any) -> str
        """
        Build a unique numeric alias from ``base``.

        The alias has the form ``<trimmed-base>_<counter>``; the base is
        trimmed so that the alias never exceeds ``max_len``, and the counter
        advances until ``key_func`` reports a free slot in ``occupied``.  The
        trim is recomputed for each counter so that widening the counter's
        digit count (9 -> 10 -> 100) never pushes the alias past the limit.

        Args:
            base: The preferred (already mangled) name.
            max_len: The hard length limit for the resulting alias.
            occupied: Mapping of occupied key to the owner token that owns it.
            owner: The ``(original, is_dir)`` token the alias is generated for.
            key_func: Callable turning a candidate alias into the key used for
                collision detection.
        Returns:
            A unique alias.
        """
        counter = 0
        while True:
            suffix = _NAME_CONFLICT_TEMPLATE % (counter,)
            room = max_len - len(suffix)
            if room < 1:
                room = 1
            alias = '%s%s' % (base[:room], suffix)
            current_owner = occupied.get(key_func(alias))
            if current_owner is None or current_owner == owner:
                return alias
            counter += 1

    def _reserve_iso_dir(self, preferred, owner):
        # type: (str, tuple) -> str
        """
        Reserve a unique ISO9660 directory identifier.

        Args:
            preferred: The mangled, preferred directory identifier.
            owner: The ``(original, is_dir)`` token reserving the identifier.
        Returns:
            The reserved, unique ISO9660 directory identifier.
        """
        key = _iso_collision_key(preferred)
        current_owner = self._iso_occupied.get(key)
        if current_owner is None or current_owner == owner:
            self._iso_occupied[key] = owner
            return preferred

        if self.iso_level == 4:
            max_len = len(preferred) + len(_NAME_CONFLICT_TEMPLATE % (0,)) + 1
        else:
            max_len = 8 if self.iso_level == 1 else 31
        alias = self._next_alias(preferred, max_len, self._iso_occupied,
                                 owner, _iso_collision_key)
        self._iso_occupied[_iso_collision_key(alias)] = owner
        return alias

    def _reserve_iso_file(self, basename, ext, owner):
        # type: (str, str, tuple) -> Tuple[str, str, bool]
        """
        Reserve a unique ISO9660 file identifier, keeping the basename and
        extension structure intact.

        Args:
            basename: The mangled, preferred basename.
            ext: The mangled extension (including the ``;1`` version), or the
                empty string.
            owner: The ``(original, is_dir)`` token reserving the identifier.
        Returns:
            A tuple of the reserved basename, the reserved extension, and
            whether a numeric alias had to be generated.
        """
        preferred = _iso_file_identifier(basename, ext)
        key = _iso_collision_key(preferred)
        current_owner = self._iso_occupied.get(key)
        if current_owner is None or current_owner == owner:
            self._iso_occupied[key] = owner
            return basename, ext, False

        suffix = _NAME_CONFLICT_TEMPLATE % (0,)
        if self.iso_level == 4:
            # Level 4 imposes no length limit, so the full basename is kept.
            max_base_len = len(basename) + len(suffix) + 1
        elif self.iso_level == 1:
            # Level 1 caps the basename itself at 8 characters (the extension
            # is independently capped at 3 by the initial mangling).
            max_base_len = 8
        else:
            # Levels 2 and 3 cap the basename at 30 characters, matching
            # truncate_basename(); the dot/extension/version tail is separate.
            max_base_len = 30

        def file_alias_key(alias_basename):
            # type: (str) -> str
            return _iso_collision_key(
                _iso_file_identifier(alias_basename, ext))

        aliased_base = self._next_alias(basename, max_base_len,
                                        self._iso_occupied, owner,
                                        file_alias_key)
        self._iso_occupied[file_alias_key(aliased_base)] = owner
        return aliased_base, ext, True

    def _reserve_joliet(self, preferred, owner):
        # type: (str, tuple) -> Tuple[str, bool]
        """
        Reserve a unique Joliet identifier, trimming the preferred name first
        so a numeric suffix always fits within the 64-character limit.

        Args:
            preferred: The (possibly truncated) preferred Joliet identifier.
            owner: The ``(original, is_dir)`` token reserving the identifier.
        Returns:
            A tuple of the reserved Joliet identifier and whether a numeric
            alias had to be generated.
        """
        current_owner = self._joliet_occupied.get(preferred)
        if current_owner is None or current_owner == owner:
            self._joliet_occupied[preferred] = owner
            return preferred, False

        alias = self._next_alias(preferred, self.joliet_max_chars,
                                 self._joliet_occupied, owner,
                                 lambda candidate: candidate)
        self._joliet_occupied[alias] = owner
        return alias, True

    def seed(self, iso_identifier=None, joliet_identifier=None):
        # type: (Optional[str], Optional[str]) -> None
        """
        Mark identifiers as already occupied without mapping a new name.

        This is used when amending an existing disc: names already present on
        it must be treated as taken so that newly added files receive aliases
        rather than colliding with them.

        Args:
            iso_identifier: An existing ISO9660 identifier (files may include
                the ``;1`` version), or None.
            joliet_identifier: An existing Joliet identifier, or None.
        Returns:
            Nothing.
        """
        if iso_identifier is not None:
            self._iso_occupied.setdefault(
                _iso_collision_key(iso_identifier), self._SEEDED_OWNER)
        if joliet_identifier is not None:
            self._joliet_occupied.setdefault(joliet_identifier,
                                             self._SEEDED_OWNER)

    def map_name(self, original, is_dir):
        # type: (str, bool) -> dict
        """
        Map one long Unicode name to its stable Joliet and ISO9660
        identifiers.

        Args:
            original: The original file or directory name (a single path
                component, with no slashes).
            is_dir: Whether the name refers to a directory.  Files and
                directories share an occupied-name pool, so a name taken by a
                directory forces a later file with the same spelling into an
                alias (and vice versa).
        Returns:
            A record dictionary with these keys:

            * ``original``: the name passed in;
            * ``is_dir``: whether it is a directory;
            * ``joliet_name``: the reserved Joliet identifier;
            * ``iso_name``: the reserved ISO9660 identifier (including the
              ``;1`` version for files);
            * ``iso_basename`` and ``iso_ext``: the ISO9660 basename and
              extension components (the extension includes ``;1`` for files);
            * ``iso_aliased`` and ``joliet_aliased``: True when the preferred
              identifier was already taken and a numeric alias was generated;
            * ``iso_conflicts`` and ``joliet_conflicts``: the original names
              that already held the preferred identifier, preserving the
              original-name-to-alias correspondence.
        """
        # Records are keyed by (original, is_dir) so that the same spelling can
        # be mapped once as a file and once as a directory, each getting its own
        # distinct slot in the shared occupied-name pool.  The same tuple is used
        # as the ownership token stored in each occupied-name map.
        cache_key = (original, bool(is_dir))
        existing = self._records.get(cache_key)
        if existing is not None:
            return existing

        if is_dir:
            iso_preferred = mangle_dir_for_iso9660(original, self.iso_level)
            joliet_preferred = mangle_dir_for_joliet(original,
                                                     self.joliet_max_chars)
            iso_key = _iso_collision_key(iso_preferred)
            iso_owner = self._iso_occupied.get(iso_key)
            joliet_owner = self._joliet_occupied.get(joliet_preferred)
            iso_conflicts = self._owner_conflict(iso_owner, cache_key)
            joliet_conflicts = self._owner_conflict(joliet_owner, cache_key)

            iso_basename = self._reserve_iso_dir(iso_preferred, cache_key)
            iso_ext = ''
            iso_name = iso_basename
            iso_aliased = iso_name != iso_preferred
            joliet_name, joliet_aliased = self._reserve_joliet(joliet_preferred,
                                                               cache_key)
        else:
            mangled_base, mangled_ext = mangle_file_for_iso9660(original,
                                                                self.iso_level)
            iso_preferred = _iso_file_identifier(mangled_base, mangled_ext)
            joliet_preferred = mangle_file_for_joliet(original,
                                                      self.joliet_max_chars)
            iso_owner = self._iso_occupied.get(
                _iso_collision_key(iso_preferred))
            joliet_owner = self._joliet_occupied.get(joliet_preferred)
            iso_conflicts = self._owner_conflict(iso_owner, cache_key)
            joliet_conflicts = self._owner_conflict(joliet_owner, cache_key)

            iso_basename, iso_ext, iso_aliased = self._reserve_iso_file(
                mangled_base, mangled_ext, cache_key)
            iso_name = _iso_file_identifier(iso_basename, iso_ext)
            joliet_name, joliet_aliased = self._reserve_joliet(joliet_preferred,
                                                               cache_key)

        self._iso_by_orig[cache_key] = iso_name
        self._joliet_by_orig[cache_key] = joliet_name

        record = {
            'original': original,
            'is_dir': bool(is_dir),
            'joliet_name': joliet_name,
            'iso_name': iso_name,
            'iso_basename': iso_basename,
            'iso_ext': iso_ext,
            'iso_aliased': iso_aliased,
            'joliet_aliased': joliet_aliased,
            'iso_conflicts': iso_conflicts,
            'joliet_conflicts': joliet_conflicts,
        }
        self._records[cache_key] = record
        return record

    @staticmethod
    def _owner_conflict(owner, cache_key):
        # type: (Any, tuple) -> List[str]
        """
        Translate the current owner of a preferred identifier into the
        conflict list recorded on the mapping result.

        Args:
            owner: The token that currently owns the identifier, or None when
                the slot is free.  Real owners are ``(original, is_dir)``
                tuples; the seeded-owner sentinel marks names pre-existing on
                a disc and is reported as an explicit marker rather than
                leaked verbatim.
            cache_key: The ``(original, is_dir)`` token attempting to take the
                identifier.
        Returns:
            A list containing the conflicting owner's original name (an empty
            list when there is no conflict, including an idempotent repeat).
        """
        if owner is None or owner == cache_key:
            return []
        if owner == NameMapper._SEEDED_OWNER:
            return ['<already on disc>']
        # A real owner is a (original, is_dir) tuple; surface just the name.
        return [owner[0]]

    def lookup(self, original, is_dir=False):
        # type: (str, bool) -> Optional[dict]
        """
        Return the record previously produced for ``original``, if any.

        Args:
            original: The original name to look up.
            is_dir: Whether to look up the directory mapping (True) or the
                file mapping (False, the default).  The same spelling may have
                one record of each type.
        Returns:
            The record dictionary, or None if the name was never mapped.
        """
        return self._records.get((original, bool(is_dir)))

    def iso_name_for(self, original, is_dir=False):
        # type: (str, bool) -> Optional[str]
        """
        Return the reserved ISO9660 identifier for an original name.

        Args:
            original: The original name to look up.
            is_dir: Whether to look up the directory mapping (True) or the
                file mapping (False, the default).
        Returns:
            The ISO9660 identifier, or None if the name was never mapped.
        """
        return self._iso_by_orig.get((original, bool(is_dir)))

    def joliet_name_for(self, original, is_dir=False):
        # type: (str, bool) -> Optional[str]
        """
        Return the reserved Joliet identifier for an original name.

        Args:
            original: The original name to look up.
            is_dir: Whether to look up the directory mapping (True) or the
                file mapping (False, the default).
        Returns:
            The Joliet identifier, or None if the name was never mapped.
        """
        return self._joliet_by_orig.get((original, bool(is_dir)))

    def records(self):
        # type: () -> List[dict]
        """
        Return every mapping record produced so far, in insertion order.

        This is the complete original-name-to-alias correspondence table: each
        record retains the original name alongside the Joliet and ISO9660
        identifiers (and any conflict owners), which is useful for emitting a
        translation table alongside the disc image.

        Returns:
            A list of the record dictionaries returned by :meth:`map_name`.
        """
        return list(self._records.values())


class Win32RawDevice:
    """
    Class to read and seek a Windows Raw Device IO object without bother.
    It deals with getting the full size, allowing full access to all sectors,
    and alignment with the discs sector size.
    """
    __slots__ = ('target', 'sector_size', 'disc_size', 'position', 'handle',
                 'geometry')

    def __init__(self, target):
        # type: (str) -> None

        if not win32_has_pywin32:  # type: ignore
            raise RuntimeError("The 'pywin32' module is missing, which is needed to access raw devices on Windows")

        self.target = target
        self.sector_size = None
        self.disc_size = None
        self.position = 0

        self.handle = self.get_handle()
        self.geometry = self.get_geometry()

    def __enter__(self):
        return self

    def __exit__(self, *_, **__):
        self.dispose()

    def __len__(self):
        # type: () -> int
        return self.geometry[-1]

    def dispose(self):
        # type: () -> None
        """Close the win32 handle opened by get_handle."""
        if self.handle != win32file.INVALID_HANDLE_VALUE:  # type: ignore
            win32file.CloseHandle(self.handle)  # type: ignore

    def get_target(self):
        # type: () -> str
        """Get UNC target name. Can be `E:` or `PhysicalDriveN`."""
        target = self.target
        if not target.startswith("\\\\.\\"):
            target = "\\\\.\\" + target
        return target

    def get_handle(self):
        # type: () -> int
        """Get a direct handle to the raw UNC target, and unlock its IO capabilities."""
        handle = win32file.CreateFile(  # type: ignore
            # https://docs.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea
            self.get_target(),  # target
            win32con.MAXIMUM_ALLOWED,  # type: ignore
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,  # type: ignore
            None,  # security attributes
            win32con.OPEN_EXISTING,  # type: ignore
            win32con.FILE_ATTRIBUTE_NORMAL,  # type: ignore
            None  # template file
        )
        if handle == win32file.INVALID_HANDLE_VALUE:  # type: ignore
            raise RuntimeError("Failed to obtain device handle...")
        # elevate accessible sectors, without this the last 5 sectors (in my case) will not be readable
        win32file.DeviceIoControl(handle, winioctlcon.FSCTL_ALLOW_EXTENDED_DASD_IO, None, None)  # type: ignore
        return handle

    def get_geometry(self):
        # type: () -> Tuple[int, int, int, int, int, int, int]
        """
        Retrieves information about the physical disk's geometry.
        https://docs.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-disk_geometry_ex

        Returns a tuple of:
            Cylinders-Lo
            Cylinders-Hi
            Media Type
            Tracks Per Cylinder
            Sectors Per Track
            Bytes Per Sector
            Disk Size
        """
        geometry_ex = win32file.DeviceIoControl(  # type: ignore
            self.handle,  # handle
            winioctlcon.IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,  # type: ignore
            None,  # in buffer
            32  # out buffer
        )
        geometry = struct.unpack("6L", geometry_ex[:24])
        disk_size = struct.unpack("<Q", geometry_ex[24:])[0]
        return (geometry[0], geometry[1], geometry[2], geometry[3], geometry[4], geometry[5], disk_size)

    def tell(self):
        # type: () -> int
        """Get current (spoofed) position."""
        return self.position

    def _tell(self):
        # type: () -> int
        """Get current real position."""
        if not self.handle:
            self.handle = self.get_handle()
        return win32file.SetFilePointer(self.handle, 0, win32file.FILE_CURRENT)  # type: ignore

    def seek(self, offset, whence=os.SEEK_SET):
        # type: (int, int) -> int
        """Seek at any point in the stream, in an aligned way."""
        if whence == os.SEEK_CUR:
            whence = self.tell()
        elif whence == os.SEEK_END:
            whence = len(self)

        to = whence + offset
        closest = self.align(to)  # get as close as we can while being aligned

        if not self.handle:
            self.handle = self.get_handle()

        pos = win32file.SetFilePointer(self.handle, closest, win32file.FILE_BEGIN)  # type: ignore
        if pos != closest:
            raise IOError("Seek was not precise...")

        self.position = to  # not actually at this location, read will deal with it
        return to

    def read(self, size=-1):
        # type: (int) -> bytes
        """Read any amount of bytes in the stream, in an aligned way."""
        if not self.handle:
            self.handle = self.get_handle()

        sector_size = self.geometry[-2]
        offset = abs(self._tell() - self.tell())

        has_data = b''
        while self._tell() < self.tell() + size:
            res, data = win32file.ReadFile(self.handle, sector_size, None)  # type: ignore
            if res != 0:
                raise IOError("An error occurred: %d %s" % (res, data))
            if len(data) < sector_size:
                raise IOError("Read %d less bytes than requested..." % (sector_size - len(data)))
            has_data += data
        # seek to the position wanted + size read, which will then be re-aligned
        self.seek(self.tell() + size)

        return has_data[offset:offset + size]

    def align(self, size, to=None):
        # type: (int, Optional[int]) -> int
        """
        Align size to the closest but floor mod `to` value.
        Examples:
            align(513, to=512)
            >>>512
            align(1023, to=512)
            >>>512
            align(1026, to=512)
            >>>1024
            align(12, to=10)
            >>>10
        """
        if to is None:
            to = self.geometry[-2]  # logical bytes per sector value
        return math.floor(size / to) * to
