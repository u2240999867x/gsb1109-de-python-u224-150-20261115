#
# This file is part of pyasn1 software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://pyasn1.readthedocs.io/en/latest/license.html
#
"""Flatten ASN.1 values into traceable field rows and rebuild them back.

The same ASN.1 tag can appear under several alternatives of a CHOICE.  A
nested view keyed by tag therefore merges fields that are semantically
different: context tag ``[0]`` under one alternative is not the same field
as context tag ``[0]`` under another one.  This module walks an ASN.1 value
tree along its schema and emits one flat row per field whose path records
both the CHOICE branch actually taken and the repetition index inside
SEQUENCE OF/SET OF containers, so every row can be traced back to the exact
position it came from.

Rows are plain :class:`dict` objects with the keys documented on
:func:`flatten`.  :func:`rebuild` performs the inverse operation: rows are
reassembled against a schema without ever merging distinct CHOICE
alternatives, and fields whose type is known keep their native type and
value byte-for-byte.  Fields that the schema leaves open (ANY) are carried
through untouched.
"""
from pyasn1 import debug
from pyasn1 import error
from pyasn1.type import base
from pyasn1.type import univ
from pyasn1.codec.native import decoder as _nativeDecoder
from pyasn1.codec.native import encoder as _nativeEncoder

__all__ = ['flatten', 'rebuild',
           'PATH', 'FIELD', 'TAG', 'TYPE', 'VALUE', 'CHOICE_BRANCH',
           'REPETITION']

LOG = debug.registerLoggee(__name__, flags=debug.DEBUG_DECODER)

#: Row key for the full field path: a tuple of path segments.
PATH = 'path'

#: Row key for the field name (the innermost path segment).
FIELD = 'field'

#: Row key for the dotted tag path, one ``class:format:id`` group per tag.
TAG = 'tag'

#: Row key for the concrete ASN.1 class name of the field value.
TYPE = 'type'

#: Row key for the field value in its native Python representation.
VALUE = 'value'

#: Row key for the CHOICE branch name at the enclosing CHOICE, if any.
CHOICE_BRANCH = 'choice_branch'

#: Row key for the zero-based repetition index inside a SEQUENCE OF/SET OF.
REPETITION = 'repetition'


def _renderTagSet(tagSet):
    # Untagged types (bare CHOICE, ANY) carry no tag of their own.
    if not len(tagSet):
        return ''

    return '.'.join(
        ['%d:%d:%d' % (superTag.tagClass, superTag.tagFormat,
                       superTag.tagId)
         for superTag in tagSet.superTags]
    )


def _namedField(container, idx):
    componentType = container.componentType
    if componentType:
        namedType = componentType[idx]
        return namedType.name, namedType.asn1Object

    return container._dynamicNames.getNameByPosition(idx), None


def _emitRow(rows, path, field, value, branchStack, repetitionStack):
    row = {
        PATH: tuple(path),
        FIELD: field,
        TAG: _renderTagSet(value.tagSet),
        TYPE: type(value).__name__,
        VALUE: _nativeEncoder.encode(value),
        CHOICE_BRANCH: branchStack[-1] if branchStack else None,
        REPETITION: (repetitionStack[-1] if repetitionStack
                     else None),
    }
    rows.append(row)
    if LOG:
        LOG('flatten produced row %r' % (row,))


def _walk(value, path, rows, branchStack, repetitionStack):
    if isinstance(value, univ.Choice):
        # Only the chosen alternative exists; its name records the branch.
        idx = value._currentIdx
        name, _spec = _namedField(value, idx)
        branchStack.append(name)
        _walk(value.getComponent(), path + (name,), rows,
              branchStack, repetitionStack)
        branchStack.pop()
        return

    if isinstance(value, univ.SequenceOfAndSetOfBase):
        for idx, component in enumerate(value):
            repetitionStack.append(idx)
            _walk(component, path + (idx,), rows,
                  branchStack, repetitionStack)
            repetitionStack.pop()
        return

    if isinstance(value, univ.SequenceAndSetBase):
        for idx in range(value._componentTypeLen or
                         len(value._dynamicNames)):
            component = value.getComponentByPosition(
                idx, instantiate=False)
            if component is univ.noValue:
                # Absent OPTIONAL/DEFAULT components leave no trace.
                continue
            name, _spec = _namedField(value, idx)
            _walk(component, path + (name,), rows,
                  branchStack, repetitionStack)
        return

    # Simple types (including ANY, whose octets the native encoder keeps
    # verbatim) and any unknown extension are emitted as leaf rows.
    field = path[-1] if path else type(value).__name__
    _emitRow(rows, path, field, value, branchStack, repetitionStack)


def flatten(asn1Value):
    """Expand an ASN.1 value into a flat, traceable field table.

    Parameters
    ----------
    asn1Value: :py:class:`~pyasn1.type.base.Asn1Item` derivative
        ASN.1 value object to flatten.

    Returns
    -------
    : :py:class:`list` of :py:class:`dict`
        One row per field actually present in *asn1Value*.  Each row holds:

        * :data:`PATH` -- tuple of segments from the root to the field.
          Segments are field names, CHOICE branch names and integer
          repetition indices for SEQUENCE OF/SET OF items;
        * :data:`FIELD` -- innermost segment (the field name);
        * :data:`TAG` -- dotted tag path
          (``tagClass:tagFormat:tagId`` per tag), empty for untagged
          ANY/CHOICE nodes;
        * :data:`TYPE` -- concrete ASN.1 class name of the value;
        * :data:`VALUE` -- value in its native Python representation;
        * :data:`CHOICE_BRANCH` -- chosen branch name at the enclosing
          CHOICE, :obj:`None` outside a CHOICE;
        * :data:`REPETITION` -- zero-based repetition index at the
          enclosing SEQUENCE OF/SET OF, :obj:`None` otherwise.

    Raises
    ------
    ~pyasn1.error.PyAsn1Error
        On an unset CHOICE or any other inconsistent value.

    Examples
    --------
    .. code-block:: python

       rows = flatten(myChoiceValue)
       # rows[0]['path'] and rows[0]['choice_branch'] tell exactly which
       # alternative an otherwise ambiguous tag belongs to.
    """
    if not isinstance(asn1Value, base.Asn1Item):
        raise error.PyAsn1Error(
            'value is not valid (should be an instance of an ASN.1 Item)')

    if isinstance(asn1Value, base.ConstructedAsn1Type) and \
            asn1Value.isInconsistent:
        raise error.PyAsn1Error(
            'ASN.1 object %s is inconsistent' %
            asn1Value.__class__.__name__)

    rows = []
    _walk(asn1Value, (), rows, [], [])
    return rows


def _childEdges(table):
    # Map every container path to the ordered, de-duplicated segments that
    # appear directly beneath it, derived from leaf paths alone.  Constructed
    # containers have no row of their own.
    edges = {}
    for path in table:
        for depth in range(len(path)):
            parent = path[:depth]
            segment = path[depth]
            bucket = edges.setdefault(parent, {})
            if segment not in bucket:
                bucket[segment] = path[:depth + 1]
    return edges


def _rebuild(schema, prefix, leafRows, edges):
    if prefix in leafRows:
        # Delegate scalar (and ANY) conversion to the native decoder so the
        # original native type and value are reconstructed exactly.
        return _nativeDecoder.decode(
            leafRows[prefix][VALUE], asn1Spec=schema)

    segments = edges.get(prefix, {})

    if isinstance(schema, univ.Choice):
        if len(segments) != 1:
            raise error.PyAsn1Error(
                'CHOICE at %r requires exactly one branch, got %r' %
                (prefix, sorted(segments)))
        name = next(iter(segments))
        idx = schema.componentType.getPositionByName(name)
        branchSpec = schema.componentType[idx].asn1Object
        branchValue = _rebuild(
            branchSpec, prefix + (name,), leafRows, edges)
        choice = schema.clone()
        choice.setComponentByName(name, branchValue)
        return choice

    if isinstance(schema, univ.SequenceOfAndSetOfBase):
        container = schema.clone()
        for index in segments:
            if not isinstance(index, int) or isinstance(index, bool):
                raise error.PyAsn1Error(
                    'Non-index segment %r inside %s at %r' %
                    (index, schema.__class__.__name__, prefix))
        indices = sorted(segments)
        if indices != list(range(len(indices))):
            raise error.PyAsn1Error(
                'Non-contiguous repetition indices %r at %r' %
                (indices, prefix))
        for index in indices:
            component = _rebuild(
                schema.componentType, prefix + (index,), leafRows, edges)
            container.append(component)
        return container

    if isinstance(schema, univ.SequenceAndSetBase):
        container = schema.clone()
        for name in segments:
            idx = container.componentType.getPositionByName(name)
            fieldSpec = container.componentType[idx].asn1Object
            fieldValue = _rebuild(
                fieldSpec, prefix + (name,), leafRows, edges)
            container.setComponentByName(name, fieldValue)
        return container

    raise error.PyAsn1Error(
        'No rows for scalar type %s at %r' %
        (schema.__class__.__name__, prefix))


def rebuild(rows, asn1Spec):
    """Reassemble a field table produced by :func:`flatten`.

    Parameters
    ----------
    rows: :py:class:`list` of :py:class:`dict`
        Field rows, such as those returned by :func:`flatten`.
    asn1Spec: :py:class:`~pyasn1.type.base.Asn1Item` derivative
        Schema of the original root value.

    Returns
    -------
    : :py:class:`~pyasn1.type.base.Asn1Item` derivative
        A value equal to the one flattened into *rows*.  CHOICE branches
        are rebuilt from their explicit path segments, so two alternatives
        sharing an outer tag are never collapsed into one column.  Values
        of schema-known fields keep their original type and value; fields
        left as ANY are restored from the preserved octets.

    Raises
    ------
    ~pyasn1.error.PyAsn1Error
        On a table whose paths do not match the schema, in particular a
        CHOICE path containing more than one branch.
    """
    if not isinstance(asn1Spec, base.Asn1Item):
        raise error.PyAsn1Error(
            'asn1Spec is not valid (should be an instance of an ASN.1 '
            'Item, not %s)' % asn1Spec.__class__.__name__)

    leafRows = {}
    for row in rows:
        path = tuple(row[PATH])
        if path in leafRows:
            raise error.PyAsn1Error(
                'Duplicate field path %r' % (path,))
        leafRows[path] = row

    edges = _childEdges(leafRows)
    return _rebuild(asn1Spec, (), leafRows, edges)
