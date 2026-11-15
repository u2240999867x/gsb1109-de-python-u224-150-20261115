#
# Copyright (c), 2016-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Offline schema packages driven by a directory manifest.

A package is a local directory with a manifest that maps the schema locations
used by the XSD sources to the files of the package. Remote locations are never
accessed: a location that is not mapped by the manifest is reported as missing,
together with the chain of references that leads to it.

The manifest can be a JSON file or a Python module. Both formats use the same
shape::

    {
        "version": "1.0",
        "root": "http://example.com/schemas/main.xsd",
        "files": {
            "http://example.com/schemas/main.xsd": "schemas/main.xsd",
            "http://example.com/schemas/types.xsd": "schemas/types.xsd"
        },
        "namespaces": {
            "http://example.com/types": "http://example.com/schemas/types.xsd"
        }
    }

A list of couple items is accepted too, so a JSON array or a Python list of
``(location, path)`` couples can be used for the ``files``/``schemas`` entry.
Relative paths are always resolved from the package directory, never from the
referring schema, so a file with the same name in another version of a package
can't be used to fill a missing dependency.
"""
import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from xmlschema.exceptions import XMLResourceError, XMLSchemaValueError
from xmlschema.translation import gettext as _
from xmlschema.utils.urls import normalize_url

import xmlschema.names as nm

logger = logging.getLogger('xmlschema')

MANIFEST_NAMES = ('manifest.json', 'manifest.py', '__init__.py')
"""Default file names for a package manifest."""

PATH_KEYS = ('files', 'schemas', 'locations', 'resources')
"""Manifest keys that can contain a location to path mapping."""


@dataclass
class SchemaReference:
    """A reference between two schema documents of a package."""
    location: str
    kind: str
    namespace: Optional[str] = None
    source: Optional[str] = None


@dataclass
class PackageIssue:
    """A missing dependency of a package, with the chain that leads to it."""
    location: str
    chain: list[SchemaReference] = field(default_factory=list)

    @property
    def message(self) -> str:
        """A description of the missing dependency and of its reference chain."""
        chunks = []
        for ref in self.chain:
            if ref.kind == 'import' and ref.namespace is not None:
                chunks.append(f'import {ref.namespace!r} -> {ref.location!r}')
            else:
                chunks.append(f'{ref.kind} -> {ref.location!r}')

        chunks.append(f'missing {self.location!r}')
        return ' / '.join(chunks)

    def __str__(self) -> str:
        return self.message


class SchemaPackage:
    """
    A local and offline schema package, defined by a directory manifest.

    :param source: the package directory or the manifest file.
    :param root: an optional location of the root schema, overriding the manifest one.
    :param encoding: the text encoding of the manifest, for default UTF-8.
    """
    directory: pathlib.Path
    manifest_path: pathlib.Path
    version: Optional[str]
    root_location: Optional[str]
    entries: dict[str, str]
    namespaces: dict[str, str]
    aliases: dict[str, str]

    def __init__(self, source: Any, root: Optional[str] = None,
                 encoding: str = 'utf-8') -> None:
        if isinstance(source, SchemaPackage):
            self.directory = source.directory
            self.manifest_path = source.manifest_path
            self.version = source.version
            self.root_location = source.root_location
            self.entries = source.entries.copy()
            self.namespaces = source.namespaces.copy()
            self.aliases = source.aliases.copy()
            if root is not None:
                self.root_location = self.resolve(root)
            return

        path = pathlib.Path(source)
        if path.is_dir():
            manifest_path = self._find_manifest(path)
        elif path.is_file():
            manifest_path = path
            path = path.parent
        else:
            msg = _("schema package {!r} doesn't exist")
            raise XMLSchemaValueError(msg.format(str(path)))

        self.directory = path.resolve()
        self.manifest_path = manifest_path.resolve()
        data = self._read_manifest(manifest_path, encoding)
        self.version = data.get('version')

        raw_entries: dict[str, str] = {}
        for key in PATH_KEYS:
            value = data.get(key)
            if value is not None:
                raw_entries.update(self._parse_entries(value, key))
                break

        if not raw_entries:
            msg = _("manifest {!r} doesn't contain any location to path mapping")
            raise XMLSchemaValueError(msg.format(str(self.manifest_path)))

        self.entries = {}
        for location, path in raw_entries.items():
            self.entries[self._normalize(location)] = self._get_path(path)

        raw_aliases = self._get_aliases(data)
        self.aliases = {}
        for location, values in raw_aliases.items():
            normalized = self._normalize(location)
            if normalized not in self.entries:
                msg = _("alias target {!r} is not mapped by the manifest")
                raise XMLSchemaValueError(msg.format(location))
            for alias in values:
                self.aliases[self._normalize(alias)] = normalized

        raw_namespaces = data.get('namespaces', {})
        if not isinstance(raw_namespaces, dict):
            msg = _("wrong type {!r} for 'namespaces' in the manifest")
            raise XMLSchemaValueError(msg.format(type(raw_namespaces)))

        self.namespaces = {}
        for namespace, location in raw_namespaces.items():
            if not isinstance(namespace, str) or not isinstance(location, str):
                msg = _("wrong item ({!r}, {!r}) in 'namespaces' of the manifest")
                raise XMLSchemaValueError(msg.format(namespace, location))
            self.namespaces[namespace] = self.resolve(location)

        root_location = data.get('root')
        if root is not None:
            self.root_location = self.resolve(root)
        elif root_location is not None:
            self.root_location = self.resolve(root_location)
        else:
            self.root_location = next(iter(self.entries))

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({str(self.directory)!r})'

    @staticmethod
    def _find_manifest(directory: pathlib.Path) -> pathlib.Path:
        for name in MANIFEST_NAMES:
            path = directory.joinpath(name)
            if path.is_file():
                return path

        msg = _("no manifest found in directory {!r}")
        raise XMLSchemaValueError(msg.format(str(directory)))

    @staticmethod
    def _read_manifest(path: pathlib.Path, encoding: str) -> dict[str, Any]:
        if path.suffix == '.py':
            namespace: dict[str, Any] = {}
            code = compile(path.read_text(encoding=encoding), str(path), 'exec')
            exec(code, namespace)
            for key in ('MANIFEST', 'SCHEMA_MANIFEST', 'LOCATION_MAP'):
                if key in namespace:
                    data = namespace[key]
                    break
            else:
                data = {k: v for k, v in namespace.items() if not k.startswith('_')}
        else:
            with path.open(encoding=encoding) as fp:
                data = json.load(fp)

        if not isinstance(data, dict):
            msg = _("manifest {!r} doesn't contain a dictionary")
            raise XMLSchemaValueError(msg.format(str(path)))
        return data

    @staticmethod
    def _parse_entries(value: Any, key: str) -> dict[str, str]:
        if isinstance(value, dict):
            entries = value
        elif isinstance(value, (list, tuple)):
            entries = {}
            for item in value:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    msg = _("wrong item {!r} in {!r} of the manifest")
                    raise XMLSchemaValueError(msg.format(item, key))
                location, path = item
                entries[location] = path
        else:
            msg = _("wrong type {!r} for {!r} in the manifest")
            raise XMLSchemaValueError(msg.format(type(value), key))

        for location, path in entries.items():
            if not isinstance(location, str) or not isinstance(path, str):
                msg = _("wrong item ({!r}, {!r}) in {!r} of the manifest")
                raise XMLSchemaValueError(msg.format(location, path, key))
        return entries

    @staticmethod
    def _get_aliases(data: dict[str, Any]) -> dict[str, list[str]]:
        aliases = data.get('aliases', {})
        if not isinstance(aliases, dict):
            msg = _("wrong type {!r} for 'aliases' in the manifest")
            raise XMLSchemaValueError(msg.format(type(aliases)))

        result = {}
        for location, values in aliases.items():
            if isinstance(values, str):
                result[location] = [values]
            elif isinstance(values, (list, tuple)) and \
                    all(isinstance(x, str) for x in values):
                result[location] = list(values)
            else:
                msg = _("wrong aliases {!r} for location {!r}")
                raise XMLSchemaValueError(msg.format(values, location))
        return result

    def _normalize(self, location: str) -> str:
        location = location.strip()
        if location.startswith(('http://', 'https://', 'ftp://', 'ftps://')):
            return location
        elif location in self.entries:
            return location
        elif location in getattr(self, 'aliases', {}):
            return self.aliases[location]
        return normalize_url(location, self.directory.as_uri())

    def _get_path(self, path: str) -> str:
        path = pathlib.Path(path)
        if not path.is_absolute():
            path = self.directory.joinpath(path)

        try:
            path.resolve().relative_to(self.directory)
        except ValueError:
            msg = _("path {!r} is outside the package directory")
            raise XMLSchemaValueError(msg.format(str(path))) from None
        return str(path.resolve())

    def resolve(self, location: str, base_url: Optional[str] = None) -> str:
        """
        Resolves a schema location using the manifest as a location map.

        The manifest keys are the schema locations used by the XSD sources, so
        a relative location is first joined to the manifest location of the
        referring schema and then searched in the manifest. If it's not found,
        the standard URL normalization is used as a fallback.

        :param location: the location to resolve, absolute or relative.
        :param base_url: the manifest location or the local URL of the referring schema.
        :return: the normalized location, or the manifest location if it's mapped.
        """
        location = location.strip()
        if location in self.entries:
            return location
        elif location in self.aliases:
            return self.aliases[location]

        if location.startswith('file:'):
            location = unquote(urlsplit(location).path)

        if pathlib.Path(location).is_absolute():
            local_location = self.get_location(location)
            if local_location is not None:
                return local_location
            return normalize_url(location)

        if base_url is None:
            return normalize_url(location, self.directory.as_uri())

        base_location = self._as_location(base_url)
        if base_location is not None:
            joined = self._join_location(base_location, location)
            if joined in self.aliases:
                return self.aliases[joined]
            return joined

        return normalize_url(location, base_url)

    def _as_location(self, base_url: str) -> Optional[str]:
        """Returns the manifest location of a base URL or file path, if any."""
        if base_url in self.entries or base_url in self.aliases:
            return base_url
        elif base_url.startswith(('http://', 'https://', 'ftp://', 'ftps://')):
            return base_url

        try:
            path = pathlib.Path(unquote(urlsplit(base_url).path))
        except (TypeError, ValueError):
            return None

        if not path.is_absolute():
            path = self.directory.joinpath(path)
        return self.get_location(path)

    @staticmethod
    def _join_location(base_location: str, location: str) -> str:
        """Joins a relative location to an absolute URI location."""
        parts = urlsplit(base_location)
        if not parts.scheme:
            base_location = normalize_url(base_location)
            parts = urlsplit(base_location)

        directories = parts.path.split('/')[:-1]
        segments = [x for x in directories if x]

        for segment in location.split('/'):
            if segment in ('', '.'):
                continue
            elif segment == '..':
                if segments:
                    segments.pop()
            else:
                segments.append(segment)

        path = '/' + '/'.join(segments)
        if parts.scheme:
            return f'{parts.scheme}://{parts.netloc}{path}'
        return path

    def get_filepath(self, location: str, base_url: Optional[str] = None) \
            -> Optional[str]:
        """Returns the local file path mapped by the manifest, `None` if not mapped."""
        path = self.entries.get(self.resolve(location, base_url))
        if path is None or not pathlib.Path(path).is_file():
            return None
        return path

    def get_location(self, path: Any) -> Optional[str]:
        """Returns the manifest location of a local file, `None` if not mapped."""
        try:
            filepath = str(pathlib.Path(path).resolve())
        except (TypeError, ValueError, OSError):
            return None

        for location, entry_path in self.entries.items():
            if entry_path == filepath:
                return location
        return None

    def get_namespace_location(self, namespace: str) -> Optional[str]:
        """Returns the manifest location associated with a namespace, if any."""
        return self.namespaces.get(namespace)

    def get_root(self) -> str:
        """Returns the location of the root schema of the package."""
        assert self.root_location is not None
        return self.root_location

    def get_root_path(self) -> str:
        """Returns the local path of the root schema of the package."""
        path = self.get_filepath(self.get_root())
        if path is None:
            msg = _("root schema {!r} is missing from the package")
            raise XMLSchemaValueError(msg.format(self.get_root()))
        return path

    def iter_missing(self) -> list[PackageIssue]:
        """
        Scans the package sources and returns the missing references,
        with the chain of references from the root schema.
        """
        from xmlschema.utils.etree import iter_schema_declarations

        issues: list[PackageIssue] = []
        visited: set[str] = set()
        pending: list[tuple[str, list[SchemaReference]]] = [(self.get_root(), [])]

        while pending:
            location, chain = pending.pop(0)
            if location in visited:
                continue
            visited.add(location)

            path = self.get_filepath(location)
            if path is None:
                issues.append(PackageIssue(location, chain))
                continue

            try:
                from xmlschema.resources import XMLResource
                resource = XMLResource(path)
            except (XMLResourceError, OSError, SyntaxError):
                issues.append(PackageIssue(location, chain))
                continue

            for elem in iter_schema_declarations(resource.root):
                ref_location = elem.get('schemaLocation')
                namespace = elem.get('namespace', '').strip() \
                    if elem.tag == nm.XSD_IMPORT else None
                kind = elem.tag.split('}')[-1]

                if ref_location is None:
                    if namespace is None or namespace not in self.namespaces:
                        continue
                    ref_location = self.namespaces[namespace]
                    if self.get_filepath(ref_location) is not None:
                        continue

                    ref = SchemaReference(ref_location, kind, namespace, path)
                    issues.append(PackageIssue(ref_location, chain + [ref]))
                    visited.add(ref_location)
                    continue

                target = self.resolve(ref_location, location)
                if target in visited:
                    continue
                elif self.get_filepath(target) is None:
                    ref = SchemaReference(ref_location, kind, namespace, path)
                    issues.append(PackageIssue(target, chain + [ref]))
                    visited.add(target)
                else:
                    ref = SchemaReference(ref_location, kind, namespace, path)
                    pending.append((target, chain + [ref]))

        return issues

    def iter_files(self) -> tuple[str, ...]:
        """Returns the local files mapped by the manifest."""
        return tuple(self.entries.values())

    def summary(self, schema: Any = None) -> list[dict[str, Any]]:
        """
        Returns the summary of the selected XSD sources.

        If a schema instance is provided, the summary is built from the schemas
        actually loaded by the loader, otherwise from the entries of the manifest.

        :param schema: an optional schema instance built from this package.
        """
        if schema is None:
            result = []
            for location, path in self.entries.items():
                result.append({
                    'location': location,
                    'path': str(pathlib.Path(path).relative_to(self.directory)),
                    'namespace': None,
                    'version': self.version,
                    'loaded': pathlib.Path(path).is_file(),
                })
            return result

        loader = schema.maps.loader
        result = []
        for item in sorted(schema.maps.owned_schemas, key=lambda x: str(x.url)):
            if item.url is None:
                continue

            location = self.get_location(item.filepath)
            if location is None:
                location = getattr(loader, '_schema_locations', {}).get(item.url)
            if location is None:
                location = item.url
            try:
                path = str(pathlib.Path(item.filepath).relative_to(self.directory))
            except (TypeError, ValueError):
                path = item.filepath or item.url
            result.append({
                'location': location,
                'path': path,
                'namespace': item.target_namespace,
                'version': item.version or self.version,
                'loaded': True,
            })
        return result

    def is_offline(self) -> bool:
        """Returns `True` if all the mapped paths are local files of the package."""
        return all(pathlib.Path(p).is_file() for p in self.entries.values())

    def create_schema(self, schema_class: Optional[type] = None, **kwargs: Any) -> Any:
        """
        Creates a schema instance from the root schema of the package.

        :param schema_class: the schema class to use, for default :class:`XMLSchema`.
        :param kwargs: other arguments for the schema class.
        """
        if schema_class is None:
            from xmlschema.validators import XMLSchema
            schema_class = XMLSchema

        from xmlschema.loaders import PackageSchemaLoader

        loader_class = kwargs.pop('loader_class', PackageSchemaLoader)
        if not issubclass(loader_class, PackageSchemaLoader):
            msg = _("the loader class of a package must be a {!r} subclass")
            raise XMLSchemaValueError(msg.format(PackageSchemaLoader))

        return schema_class(
            source=self.get_root_path(),
            loader_class=loader_class,
            package=self,
            allow='local',
            **kwargs
        )
