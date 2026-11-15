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
Batch validation of XML documents with cross-record identity constraints.

This module provides the :class:`BatchValidator`, a facility for ingesting a
batch of XML records one at a time when key uniqueness and key references
(*xs:unique*, *xs:key* and *xs:keyref* identity constraints declared on the
document element) have to be enforced on the whole batch instead of on each
single record.

Every added record advances through a sequence of states
(:class:`BatchRecordState`):

* ``PARSED``: the XML source is well-formed and has been parsed;
* ``LOCALLY_VALID``: the record also validates against the schema on its own
  (structure, datatypes and identity constraints not declared on the document
  element). This is *not* a final success state, because cross-record
  constraints are still unchecked;
* ``COMMITTABLE``: the whole batch has been finalized and every cross-record
  constraint (key uniqueness and key references across records) is satisfied.
  Only at this point a record can be considered successfully ingested.

Key references are resolved at finalization time on the key values collected
from all the records, so a reference is satisfied also when the referred key
arrives with a later record ("late" references). Duplicate key values are
reported with an error that points back to both the records involved.
"""
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterator, Optional, Union

from elementpath import XPathContext

import xmlschema.names as nm
from .aliases import XMLSourceType
from .exceptions import XMLSchemaException
from .resources import XMLResource
from .translation import gettext as _
from .validators import XMLSchema, XMLSchemaBase
from .validators.identities import XsdIdentity, XsdKeyref


class BatchRecordState(IntEnum):
    """The ingestion state of a record of a batch."""
    PARSED = 1
    LOCALLY_VALID = 2
    COMMITTABLE = 3


class BatchError(XMLSchemaException):
    """Base class for batch validation errors."""


class DuplicateKeyError(BatchError):
    """
    Raised when the same key value is collected from more than one record.
    The attributes *first* and *second* are the two records involved, so the
    error points back to both the locations where the key value occurs.
    """
    def __init__(self, identity: str, value: tuple[Any, ...],
                 first: 'BatchRecord', second: 'BatchRecord') -> None:
        self.identity = identity
        self.value = value
        self.first = first
        self.second = second
        msg = _(
            "duplicated value {0!r} for key {1!r}: present in record "
            "#{2} ({3}) and again in record #{4} ({5})"
        )
        super().__init__(msg.format(
            value, identity, first.index, first.name, second.index, second.name
        ))


class UnresolvedKeyrefError(BatchError):
    """
    Raised at finalization when a key reference value has no matching key
    in any record of the batch.
    """
    def __init__(self, keyref: str, refer: str, value: tuple[Any, ...],
                 record: 'BatchRecord') -> None:
        self.keyref = keyref
        self.refer = refer
        self.value = value
        self.record = record
        msg = _(
            "value {0!r} not found for key {1!r}: referenced by keyref "
            "{2!r} in record #{3} ({4})"
        )
        super().__init__(msg.format(value, refer, keyref, record.index, record.name))


class BatchValidationError(BatchError):
    """
    Raised by :meth:`BatchValidator.commit` when the batch is not committable.
    The attribute *errors* contains the cross-record errors and the attribute
    *records* the records that failed local validation.
    """
    def __init__(self, errors: list[BatchError],
                 invalid_records: Optional[list['BatchRecord']] = None) -> None:
        self.errors = errors
        self.invalid_records = invalid_records if invalid_records is not None else []

        parts = []
        if errors:
            parts.append(_("{0} cross-record error(s)").format(len(errors)))
        if self.invalid_records:
            parts.append(_("{0} invalid record(s)").format(len(self.invalid_records)))
        message = _("batch is not committable") + ": " + ", ".join(parts)
        if errors:
            message += ":\n" + "\n".join(str(err) for err in errors)
        super().__init__(message)


@dataclass
class BatchRecord:
    """
    The ingestion report of a single XML record added to a :class:`BatchValidator`.

    :ivar index: the position of the record within the batch.
    :ivar name: a display name for the record (the source URL/path when available).
    :ivar source: the original XML source of the record.
    :ivar state: the highest ingestion state reached by the record. It is \
    `None` until the record is successfully parsed.
    :ivar errors: local errors (parse or validation errors) of the record.
    :ivar batch_errors: cross-record errors involving the record, computed \
    at batch finalization.
    """
    index: int
    name: str
    source: Any = None
    state: Optional[BatchRecordState] = None
    errors: list[Exception] = field(default_factory=list)
    batch_errors: list[BatchError] = field(default_factory=list)

    @property
    def parsed(self) -> bool:
        """Whether the record has been successfully parsed."""
        return self.state is not None and self.state >= BatchRecordState.PARSED

    @property
    def locally_valid(self) -> bool:
        """
        Whether the record is valid on its own. This is not a final success
        condition: cross-record constraints are verified only when the batch
        is finalized.
        """
        return self.state is not None and self.state >= BatchRecordState.LOCALLY_VALID

    @property
    def committable(self) -> bool:
        """Whether the record is part of a successfully finalized batch."""
        return self.state is BatchRecordState.COMMITTABLE

    def __repr__(self) -> str:
        state = self.state.name.lower() if self.state is not None else 'not parsed'
        return '%s(#%d, %r, %s)' % (self.__class__.__name__, self.index, self.name, state)


class BatchValidator:
    """
    Validator for ingesting a batch of XML records with identity constraints
    that span across records.

    :param schema: an :class:`xmlschema.XMLSchema` instance or a schema source \
    from which a schema instance is built.
    :param resource_kwargs: optional keyword arguments for the initialization \
    of the :class:`xmlschema.XMLResource` instances of the added records.

    The *xs:unique*, *xs:key* and *xs:keyref* constraints declared on the
    document element are verified on the whole batch when the batch is
    finalized: key values collected from all the records must be unique and
    every key reference must match a collected key, regardless of the record
    it belongs to. Identity constraints declared on inner elements keep their
    usual per-record semantics and are verified during local validation.
    """

    def __init__(self, schema: Union[XMLSchemaBase, XMLSourceType],
                 **resource_kwargs: Any) -> None:
        if isinstance(schema, XMLSchemaBase):
            self.schema = schema
        else:
            self.schema = XMLSchema(schema)
        self._resource_kwargs = resource_kwargs
        self.records: list[BatchRecord] = []
        self.errors: list[BatchError] = []
        self._finalized = False
        self._key_values: dict[str, dict[tuple[Any, ...], list[BatchRecord]]] = {}
        self._keyref_values: dict[str, list[tuple[tuple[Any, ...], BatchRecord]]] = {}
        self._keyref_refers: dict[str, str] = {}

    def __repr__(self) -> str:
        return '%s(records=%d, finalized=%r)' % (
            self.__class__.__name__, len(self.records), self._finalized
        )

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[BatchRecord]:
        return iter(self.records)

    @property
    def finalized(self) -> bool:
        """Whether the batch has been finalized after the last added record."""
        return self._finalized

    @property
    def committable(self) -> bool:
        """Whether the batch has been finalized and can be committed."""
        return self._finalized and not self.errors and all(
            record.state is BatchRecordState.COMMITTABLE for record in self.records
        )

    def add(self, source: XMLSourceType, name: Optional[str] = None) -> BatchRecord:
        """
        Adds an XML record to the batch, parsing and validating it locally.

        The returned record reaches at most the ``LOCALLY_VALID`` state: the
        record is never reported as fully successful by this method, because
        cross-record constraints are verified only at batch finalization.

        :param source: the XML source of the record (a string with XML data, \
        a path, a file-like object or an ElementTree structure).
        :param name: an optional display name for the record.
        :return: the :class:`BatchRecord` report of the added record.
        """
        record = BatchRecord(index=len(self.records),
                             name=name or 'record #%d' % len(self.records),
                             source=source)
        self.records.append(record)
        self._finalized = False
        for item in self.records:
            if item.state is BatchRecordState.COMMITTABLE:
                item.state = BatchRecordState.LOCALLY_VALID

        try:
            resource = XMLResource(source, **self._resource_kwargs)
        except Exception as err:
            record.errors.append(err)
            return record

        if name is None:
            url = getattr(resource, 'url', None)
            if url:
                record.name = url

        record.state = BatchRecordState.PARSED

        xsd_element = self.schema.get_element(
            resource.root.tag, resource.get_absolute_path(), resource.get_namespaces()
        )

        # Key references declared on the document element are excluded from
        # local validation and verified on the whole batch at finalization,
        # so that references to keys of later records are not rejected here.
        identities = None
        if xsd_element is not None:
            identities = xsd_element.identities
            others = [x for x in identities if not isinstance(x, XsdKeyref)]
            if len(others) < len(identities):
                xsd_element.identities = others
            else:
                identities = None

        try:
            record.errors.extend(self.schema.iter_errors(resource))
        finally:
            if identities is not None and xsd_element is not None:
                xsd_element.identities = identities

        if record.errors:
            return record

        try:
            self._collect_identities(resource, xsd_element, record)
        except Exception as err:
            record.errors.append(err)
            return record

        record.state = BatchRecordState.LOCALLY_VALID
        return record

    def finalize(self) -> bool:
        """
        Verifies the cross-record constraints of the batch: uniqueness of the
        key values collected from all the records and resolution of every key
        reference against the collected keys, including keys that arrived with
        later records.

        Every record is marked as ``COMMITTABLE`` only if the whole batch
        satisfies all the constraints and every record is locally valid,
        otherwise locally valid records keep the ``LOCALLY_VALID`` state.
        The method can be called again after adding more records.

        :return: `True` if the batch is committable, `False` otherwise.
        """
        self.errors = []
        for record in self.records:
            record.batch_errors.clear()

        for name, values in self._key_values.items():
            for fields, records in values.items():
                if len(records) > 1:
                    first = records[0]
                    for other in records[1:]:
                        self._add_error(DuplicateKeyError(name, fields, first, other),
                                        first, other)

        for name, entries in self._keyref_values.items():
            try:
                refer_values = self._key_values[self._keyref_refers[name]]
            except KeyError:
                refer_values = {}
            for fields, record in entries:
                if fields not in refer_values:
                    error = UnresolvedKeyrefError(
                        name, self._keyref_refers.get(name, name), fields, record
                    )
                    self._add_error(error, record)

        self._finalized = True
        if not self.errors and all(record.locally_valid and not record.errors
                                   for record in self.records):
            for record in self.records:
                record.state = BatchRecordState.COMMITTABLE
        return self.committable

    def commit(self) -> None:
        """
        Finalizes the batch, raising a :class:`BatchValidationError` if the
        batch is not committable.

        :raises BatchValidationError: if cross-record constraints are violated \
        or some record is not locally valid.
        """
        if not self.finalize():
            invalid = [record for record in self.records if record.errors]
            raise BatchValidationError(self.errors, invalid)

    def reset(self) -> None:
        """Clears the batch, discarding all the added records."""
        self.records.clear()
        self.errors = []
        self._finalized = False
        self._key_values.clear()
        self._keyref_values.clear()
        self._keyref_refers.clear()

    def _add_error(self, error: BatchError, *records: BatchRecord) -> None:
        self.errors.append(error)
        for record in records:
            record.batch_errors.append(error)

    def _collect_identities(self, resource: XMLResource, xsd_element: Any,
                            record: BatchRecord) -> None:
        if xsd_element is None:
            return

        namespaces = resource.get_namespaces()
        root_node = resource.get_xpath_node(resource.root)

        for identity in xsd_element.identities:
            if not identity.built or identity.selector is None:
                continue

            context = XPathContext(root_node)
            for elem in identity.selector.token.select_results(context):
                selectors = None
                for xsd_elem, field_selectors in identity.elements.items():
                    if xsd_elem.is_matching(elem.tag):
                        selectors = field_selectors
                        break
                if selectors is None:
                    continue

                element_node = resource.get_xpath_node(elem)
                fields = tuple(s.get_value(element_node, namespaces) for s in selectors)
                nilled = elem.get(nm.XSI_NIL, 'false').strip() in ('1', 'true')
                if any(x is not None for x in fields) or nilled:
                    self._register_identity_value(identity, fields, record)

    def _register_identity_value(self, identity: XsdIdentity,
                                 fields: tuple[Any, ...], record: BatchRecord) -> None:
        if isinstance(identity, XsdKeyref):
            self._keyref_values.setdefault(identity.name, []).append((fields, record))
            refer = identity.refer
            if isinstance(refer, XsdIdentity):
                self._keyref_refers[identity.name] = refer.name
            elif isinstance(refer, str):
                self._keyref_refers[identity.name] = refer
        else:
            values = self._key_values.setdefault(identity.name, {})
            values.setdefault(fields, []).append(record)
