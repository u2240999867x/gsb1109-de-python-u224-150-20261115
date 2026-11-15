#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""Offline record migration across schema revisions.

This module implements a building block for migrating persisted
dict-like records from an old schema revision to a new one without
any live service (offline migration). It covers the common revision
changes:

* the upstream schema gains new optional fields
* two old states are merged into a single enum field

The field/value mapping is always supplied by the caller, never
hard-coded here. Legacy fields the migration does not know about
(unknown extensions) are preserved in a separate bucket rather than
dropped. Merges that can not be reversed unambiguously report which
old value was lost. Fields not participating in the change are
deep-copied through, so a migrate/restore round-trip never resets
them to default empty values.
"""
import collections
import copy

from pyasn1 import error


class MigrationError(error.PyAsn1Error):
    """Offline record migration error."""


class LostValue(collections.namedtuple(
        'LostValue', ('field', 'old_value', 'new_value'))):
    """Old value erased by an irreversible enum merge.

    Attributes
    ----------
    field: :py:class:`str`
        Name of the new enum field the merge targeted
    old_value:
        Old state value that can no longer be derived back
    new_value:
        New enum value the old state was merged into
    """


class MigrationResult(object):
    """Outcome of a single :meth:`OfflineMigration.migrate` call.

    Attributes
    ----------
    record: :py:class:`dict`
        The migrated record in the new schema revision
    extensions: :py:class:`dict`
        Unknown legacy fields, stored separately from the schema
        fields (also carried inside `record` under the migration's
        extension key so they survive serialization)
    lost: :py:class:`tuple` of :class:`LostValue`
        Old values erased by irreversible enum merges
    """
    def __init__(self, record, extensions, lost):
        self._record = record
        self._extensions = extensions
        self._lost = tuple(lost)

    @property
    def record(self):
        return self._record

    @property
    def extensions(self):
        return self._extensions

    @property
    def lost(self):
        return self._lost


class OfflineMigration(object):
    """Migrate records between two schema revisions offline.

    Parameters
    ----------
    renames: :py:class:`dict`, optional
        Mapping of old field name to new field name for fields that
        only changed their name
    passthrough: iterable of :py:class:`str`, optional
        Field names carried over unchanged. Their values are
        deep-copied, so complex fields not participating in the
        change survive a round-trip intact instead of degrading to
        default empty values
    state_merges: :py:class:`dict`, optional
        Mapping of new enum field name to
        ``{'source': old_field, 'values': {old_value: new_value}}``
        describing how old states merge into the new enum. The value
        mapping is caller-supplied; whenever two old values collapse
        into one new value the merge is irreversible and the lost old
        value is reported via :attr:`MigrationResult.lost`
    defaults: :py:class:`dict`, optional
        Values for optional fields the new schema revision added.
        Applied only when the field is absent from the migrated
        record; absent optional fields are otherwise left absent
    extension_key: :py:class:`str`
        Record key under which unknown legacy extensions are stored
    """
    def __init__(self, renames=None, passthrough=(), state_merges=None,
                 defaults=None, extension_key='_extensions'):
        self._renames = dict(renames or {})
        self._passthrough = tuple(passthrough)
        self._defaults = dict(defaults or {})
        self._extension_key = extension_key

        self._state_merges = {}
        self._inverse = {}

        for new_key, merge in (state_merges or {}).items():
            try:
                source = merge['source']
                values = dict(merge['values'])

            except (TypeError, KeyError):
                raise MigrationError(
                    'state merge for %r must define source and values'
                    % (new_key,))

            inverse = {}
            for old_value, new_value in values.items():
                inverse.setdefault(new_value, []).append(old_value)

            self._state_merges[new_key] = (source, values)
            self._inverse[new_key] = inverse

        targets = (list(self._renames.values()) + list(self._passthrough) +
                   list(self._state_merges))

        if len(set(targets)) != len(targets):
            raise MigrationError('overlapping target fields in migration')

        sources = (list(self._renames) + list(self._passthrough) +
                   [merge[0] for merge in self._state_merges.values()])

        if extension_key in targets or extension_key in sources:
            raise MigrationError(
                'extension key %r collides with a migrated field'
                % (extension_key,))

    @property
    def extension_key(self):
        return self._extension_key

    def migrate(self, old_record):
        """Migrate *old_record* into the new schema revision.

        Parameters
        ----------
        old_record: :py:class:`dict`
            Record persisted under the old schema revision

        Returns
        -------
        :class:`MigrationResult`
            Migrated record, separately stored unknown extensions and
            the report of old values lost to irreversible merges
        """
        new_record = {}
        lost = []
        consumed = set()

        for old_key, new_key in self._renames.items():
            if old_key in old_record:
                new_record[new_key] = copy.deepcopy(old_record[old_key])
                consumed.add(old_key)

        for new_key, (source, values) in self._state_merges.items():
            if source not in old_record:
                continue

            old_value = old_record[source]

            if old_value not in values:
                raise MigrationError(
                    'no mapping for old state %r of field %r'
                    % (old_value, source))

            new_value = values[old_value]
            new_record[new_key] = copy.deepcopy(new_value)

            if len(self._inverse[new_key][new_value]) > 1:
                lost.append(LostValue(new_key, old_value, new_value))

            consumed.add(source)

        for key in self._passthrough:
            if key in old_record:
                new_record[key] = copy.deepcopy(old_record[key])
                consumed.add(key)

        for key, value in self._defaults.items():
            if key not in new_record:
                new_record[key] = copy.deepcopy(value)

        extensions = {}

        for key in old_record:
            if key in consumed:
                continue

            if key == self._extension_key and isinstance(
                    old_record[key], dict):
                extensions.update(copy.deepcopy(old_record[key]))

            else:
                extensions[key] = copy.deepcopy(old_record[key])

        if extensions:
            new_record[self._extension_key] = extensions

        return MigrationResult(new_record, extensions, lost)

    def reverse(self, new_record, lost=None):
        """Restore an old-revision record from a migrated one.

        Parameters
        ----------
        new_record: :py:class:`dict`
            Record produced by :meth:`migrate`
        lost: mapping or iterable of :class:`LostValue`, optional
            Disambiguation for irreversible enum merges, typically
            :attr:`MigrationResult.lost` captured at migration time.
            Without it, reversing an irreversible merge fails

        Returns
        -------
        :py:class:`dict`
            Record in the old schema revision
        """
        if lost is None:
            lost_hints = {}

        elif isinstance(lost, dict):
            lost_hints = dict(lost)

        else:
            lost_hints = dict(
                (entry.field, entry.old_value) for entry in lost)

        old_record = {}
        consumed = set()

        for old_key, new_key in self._renames.items():
            if new_key in new_record:
                old_record[old_key] = copy.deepcopy(new_record[new_key])
                consumed.add(new_key)

        for new_key, (source, values) in self._state_merges.items():
            if new_key not in new_record:
                continue

            new_value = new_record[new_key]
            candidates = self._inverse[new_key].get(new_value)

            if candidates is None:
                raise MigrationError(
                    'no mapping for new state %r of field %r'
                    % (new_value, new_key))

            if len(candidates) == 1:
                old_value = candidates[0]

            elif new_key in lost_hints:
                old_value = lost_hints[new_key]

                if old_value not in candidates:
                    raise MigrationError(
                        'lost value %r is not a source of new state %r '
                        'for field %r' % (old_value, new_value, new_key))

            else:
                raise MigrationError(
                    'merge into field %r is irreversible: new state %r '
                    'has old sources %r, supply the lost value'
                    % (new_key, new_value, candidates))

            old_record[source] = copy.deepcopy(old_value)
            consumed.add(new_key)

        for key in self._passthrough:
            if key in new_record:
                old_record[key] = copy.deepcopy(new_record[key])
                consumed.add(key)

        for key, value in self._defaults.items():
            if key not in new_record:
                continue

            consumed.add(key)

            if new_record[key] != value:
                raise MigrationError(
                    'new optional field %r carries non-default value %r '
                    'that has no old-revision counterpart'
                    % (key, new_record[key]))

        extensions = new_record.get(self._extension_key)

        if extensions:
            if not isinstance(extensions, dict):
                raise MigrationError(
                    'extension bucket %r is not a mapping'
                    % (self._extension_key,))

            collision = set(extensions) & set(old_record)

            if collision:
                raise MigrationError(
                    'extension fields collide with migrated fields: %r'
                    % (sorted(collision),))

            old_record.update(copy.deepcopy(extensions))
            consumed.add(self._extension_key)

        unknown = set(new_record) - consumed

        if unknown:
            raise MigrationError(
                'new record carries fields unknown to this migration: %r'
                % (sorted(unknown),))

        return old_record
