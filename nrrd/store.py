import hashlib
import os
import uuid
from typing import List, Optional, Tuple

import numpy.typing as npt

from nrrd.errors import NRRDError
from nrrd.reader import read, read_header
from nrrd.types import IndexOrder, NRRDFieldMap, NRRDHeader
from nrrd.writer import _handle_header, _write_data, _write_header

_DATA_DIR = 'data'
_VERSIONS_DIR = 'versions'
_TMP_DIR = 'tmp'
_CURRENT_FILE = 'CURRENT'

_HASH_CHUNKSIZE: int = 2 ** 20

_ENCODING_EXTENSIONS = {
    'raw': '.raw',
    'ascii': '.txt',
    'text': '.txt',
    'txt': '.txt',
    'gzip': '.raw.gz',
    'gz': '.raw.gz',
    'bzip2': '.raw.bz2',
    'bz2': '.raw.bz2',
}


def _fsync_directory(path: str):
    """Best-effort fsync of a directory so that renames within it are durable."""
    if not hasattr(os, 'O_DIRECTORY'):
        return

    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return

    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()

    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(_HASH_CHUNKSIZE)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


class NrrdStore:
    """Versioned local store for incremental publishing of detached-header NRRD files

    The store keeps published NRRD data as a content-addressed pool of detached data files plus one header per
    published version. A version only becomes visible once its header and every referenced data file are fully
    written, so readers always see either the complete previous version or the complete new version, never a
    partially published one.

    Layout on disk::

        root/
            CURRENT                 # id of the visible version, updated atomically last
            data/<sha256><ext>      # content-addressed data files, never overwritten in place
            versions/<id>.nhdr      # one detached header per version
            tmp/                    # staging area for in-progress publishes

    Because data files are named by the hash of their content, identical data is stored only once and reused by
    every version that references it, and a data file referenced by an older version is never overwritten in
    place by a newer publish. Use :meth:`collect_garbage` to remove data files that are no longer referenced by
    any version; files still referenced are always kept.

    .. note::
            The store supports a single writer process at a time. Concurrent readers are safe since a version
            is only visible after it has been completely published.

    Parameters
    ----------
    root : :class:`str`
        Root directory of the store. Created if it does not exist.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(self._versions_dir, exist_ok=True)
        os.makedirs(self._tmp_dir, exist_ok=True)

        # Remove leftover staging files from an interrupted publish
        self._clean_tmp()

    @property
    def _data_dir(self) -> str:
        return os.path.join(self.root, _DATA_DIR)

    @property
    def _versions_dir(self) -> str:
        return os.path.join(self.root, _VERSIONS_DIR)

    @property
    def _tmp_dir(self) -> str:
        return os.path.join(self.root, _TMP_DIR)

    @property
    def _current_path(self) -> str:
        return os.path.join(self.root, _CURRENT_FILE)

    def _clean_tmp(self):
        for name in os.listdir(self._tmp_dir):
            os.remove(os.path.join(self._tmp_dir, name))

    def versions(self) -> List[int]:
        """Return the sorted list of published version identifiers"""
        versions = []

        for name in os.listdir(self._versions_dir):
            base, ext = os.path.splitext(name)
            if ext == '.nhdr' and base.isdigit():
                versions.append(int(base))

        return sorted(versions)

    def current_version(self) -> Optional[int]:
        """Return the currently visible version identifier or :obj:`None` if nothing was published"""
        if not os.path.exists(self._current_path):
            return None

        with open(self._current_path, 'r') as fh:
            return int(fh.read().strip())

    def header_path(self, version: Optional[int] = None) -> str:
        """Return the path of the detached header for :obj:`version` (defaults to the current version)"""
        if version is None:
            version = self.current_version()

            if version is None:
                raise NRRDError('No version has been published to the store yet')

        path = os.path.join(self._versions_dir, f'{version:06d}.nhdr')

        if not os.path.exists(path):
            raise NRRDError(f'Version does not exist in the store: {version}')

        return path

    def read_header(self, version: Optional[int] = None) -> NRRDHeader:
        """Read the header of :obj:`version` (defaults to the current version)"""
        return read_header(self.header_path(version))

    def read(self, version: Optional[int] = None, index_order: IndexOrder = 'F') -> Tuple[npt.NDArray, NRRDHeader]:
        """Read the data and header of :obj:`version` (defaults to the current version)"""
        return read(self.header_path(version), index_order=index_order)

    def publish(self, data: npt.NDArray, header: Optional[NRRDHeader] = None, *,
                custom_field_map: Optional[NRRDFieldMap] = None, compression_level: int = 9,
                index_order: IndexOrder = 'F') -> int:
        """Publish a new version of the data and return its version identifier

        The data is written to a content-addressed data file first (reusing the existing file if identical
        content was already published), then the detached header is written, and only then is the new version
        made visible by atomically updating the CURRENT pointer. If the process is interrupted at any point,
        the previously published version remains visible and readable.

        Parameters
        ----------
        data : :class:`numpy.ndarray`
            Data to publish
        header : :class:`dict` (:class:`str`, :obj:`Object`), optional
            NRRD header fields. The 'data file' field is always set by the store.
        custom_field_map : :class:`dict` (:class:`str`, :class:`str`), optional
            Dictionary used for parsing custom field types where the key is the custom field name and the value
            is a string identifying datatype for the custom field.
        compression_level : :class:`int`
            Integer between 1 and 9 specifying the compression level when using a compressed encoding (gzip or
            bzip).
        index_order : {'C', 'F'}, optional
            Specifies the index order used for writing. Either 'C' (C-order) or 'F' (Fortran-order).

        Returns
        -------
        version : :class:`int`
            Identifier of the newly published version
        """
        header = _handle_header(data, header, index_order)

        encoding = header['encoding'].lower()
        if encoding not in _ENCODING_EXTENSIONS:
            raise NRRDError(f'Unsupported encoding for versioned store: {header["encoding"]}')

        data_name = self._stage_data(data, header, compression_level, index_order)

        version = max(self.versions(), default=0) + 1

        # Reference the data file relative to the header directory so the store stays relocatable
        header['data file'] = f'../{_DATA_DIR}/{data_name}'

        self._stage_header(version, header, custom_field_map)
        self._commit_current(version)

        return version

    def _stage_data(self, data: npt.NDArray, header: NRRDHeader, compression_level: int,
                    index_order: IndexOrder) -> str:
        """Write data to the content-addressed pool and return the data file name

        The data is first written to a temporary file, then moved to its content-addressed name. An existing
        file with identical content is reused as-is and never overwritten.
        """
        tmp_path = os.path.join(self._tmp_dir, f'data-{uuid.uuid4().hex}')

        try:
            with open(tmp_path, 'wb') as fh:
                _write_data(data, fh, header, compression_level=compression_level, index_order=index_order)
                os.fsync(fh.fileno())

            data_name = _hash_file(tmp_path) + _ENCODING_EXTENSIONS[header['encoding'].lower()]
            data_path = os.path.join(self._data_dir, data_name)

            if os.path.exists(data_path):
                # Identical content already published, reuse the existing data file
                os.remove(tmp_path)
            else:
                os.replace(tmp_path, data_path)
                _fsync_directory(self._data_dir)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        return data_name

    def _stage_header(self, version: int, header: NRRDHeader, custom_field_map: Optional[NRRDFieldMap]):
        tmp_path = os.path.join(self._tmp_dir, f'header-{version:06d}-{uuid.uuid4().hex}.nhdr')

        with open(tmp_path, 'wb') as fh:
            _write_header(fh, header, custom_field_map)
            os.fsync(fh.fileno())

        os.replace(tmp_path, os.path.join(self._versions_dir, f'{version:06d}.nhdr'))
        _fsync_directory(self._versions_dir)

    def _commit_current(self, version: int):
        tmp_path = os.path.join(self._tmp_dir, f'current-{uuid.uuid4().hex}')

        with open(tmp_path, 'w') as fh:
            fh.write(f'{version:06d}\n')
            fh.flush()
            os.fsync(fh.fileno())

        # Atomically make the fully staged version visible
        os.replace(tmp_path, self._current_path)
        _fsync_directory(self.root)

    def _referenced_data_files(self) -> set:
        """Return the names of all data files referenced by any published version"""
        referenced = set()

        for version in self.versions():
            header = read_header(os.path.join(self._versions_dir, f'{version:06d}.nhdr'))
            data_files = header.get('data file', header.get('datafile'))

            if data_files is None:
                continue

            if isinstance(data_files, str):
                data_files = [data_files]

            for data_file in data_files:
                # Only consider data files inside the store's data directory
                resolved = os.path.normpath(os.path.join(self._versions_dir, data_file))
                if os.path.dirname(resolved) == self._data_dir:
                    referenced.add(os.path.basename(resolved))

        return referenced

    def collect_garbage(self) -> List[str]:
        """Remove unreferenced data files and staging leftovers, returning the removed paths

        Every data file still referenced by any published version is kept, including files referenced only by
        older versions.
        """
        self._clean_tmp()

        referenced = self._referenced_data_files()
        removed = []

        for name in os.listdir(self._data_dir):
            if name not in referenced:
                path = os.path.join(self._data_dir, name)
                os.remove(path)
                removed.append(path)

        return removed

    def delete_version(self, version: int):
        """Delete the header of :obj:`version`

        The current version cannot be deleted. Data files are not removed here; call
        :meth:`collect_garbage` afterwards to drop data files that are no longer referenced.
        """
        if version == self.current_version():
            raise NRRDError(f'Cannot delete the current version: {version}')

        path = os.path.join(self._versions_dir, f'{version:06d}.nhdr')

        if not os.path.exists(path):
            raise NRRDError(f'Version does not exist in the store: {version}')

        os.remove(path)
        _fsync_directory(self._versions_dir)

    def __repr__(self):
        return f'NrrdStore({self.root!r}, current_version={self.current_version()})'
