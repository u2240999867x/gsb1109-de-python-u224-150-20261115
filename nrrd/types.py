from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from typing_extensions import Literal

NRRDFieldType = Literal['int', 'double', 'string', 'int list', 'double list', 'string list', 'quoted string list',
                        'int vector', 'double vector', 'int matrix', 'double matrix', 'int vector list',
                        'double vector list']

IndexOrder = Literal['F', 'C']

NRRDFieldMap = Dict[str, NRRDFieldType]

_FIELD_KEY_ALIASES = {
    'name': 'name',
    'declaration': 'declaration',
    'original': 'declaration',
    'original_declaration': 'declaration',
    'line': 'declaration',
    'type': 'field_type',
    'field_type': 'field_type',
    'parsed_type': 'field_type',
    'value': 'value',
    'corrected': 'value',
    'corrected_value': 'value',
    'raw': 'raw_value',
    'raw_value': 'raw_value',
    'original_value': 'raw_value',
    'initial_value': 'initial_value',
    'parsed_value': 'initial_value',
    'parsed': 'initial_value',
    'original_parsed_value': 'initial_value',
}


@dataclass
class NRRDField:
    """A single NRRD header field declaration.

    The record keeps both the declaration as it was read from the file and the value obtained after applying the
    field type.  This allows a caller to correct a value and write the header back without losing how the field was
    declared or what type was used to parse it.

    Attributes
    ----------
    name : :class:`str`
        Name of the field.
    value : :obj:`Any`
        Parsed value currently associated with the field. Assigning to this attribute is reflected by the header
        through :attr:`corrected_value`.
    field_type : :class:`str`
        NRRD field type used to parse the declaration.
    declaration : :class:`str`
        Original header line (without its trailing newline).
    raw_value : :class:`str`
        Original value text following the ``:`` or ``:=`` delimiter.
    delimiter : :class:`str`
        Delimiter used by the original declaration, either ``:`` or ``:=``.
    is_standard : :class:`bool`
        Whether the field is defined by the NRRD specification.
    is_custom : :class:`bool`
        Whether the field was declared using the custom field delimiter (``:=``) or was found in a custom field map.
    is_unknown : :class:`bool`
        Whether the field is neither standard nor present in the custom field map.
    """

    name: str
    value: Any
    field_type: Optional[NRRDFieldType] = None
    declaration: str = ''
    raw_value: str = ''
    delimiter: str = ':='
    is_standard: bool = False
    is_custom: bool = False
    is_unknown: bool = False
    initial_value: Any = field(default=None, repr=False)

    def __post_init__(self):
        self.initial_value = self.value

    def clone(self) -> 'NRRDField':
        """Return a copy of this field record."""
        return NRRDField(name=self.name,
                         value=self.value,
                         field_type=self.field_type,
                         declaration=self.declaration,
                         raw_value=self.raw_value,
                         delimiter=self.delimiter,
                         is_standard=self.is_standard,
                         is_custom=self.is_custom,
                         is_unknown=self.is_unknown,
                         initial_value=self.initial_value)

    @property
    def original_declaration(self) -> str:
        """Original declaration line for this field."""
        return self.declaration

    @property
    def declaration_line(self) -> str:
        """Alias for :attr:`original_declaration`."""
        return self.declaration

    @property
    def original_line(self) -> str:
        """Alias for :attr:`original_declaration`."""
        return self.declaration

    @property
    def original(self) -> str:
        """Alias for :attr:`original_declaration`."""
        return self.declaration

    @property
    def parsed_type(self) -> Optional[NRRDFieldType]:
        """Field type used to parse the original declaration."""
        return self.field_type

    @property
    def type(self) -> Optional[NRRDFieldType]:
        """Alias for :attr:`parsed_type`."""
        return self.field_type

    @property
    def corrected(self) -> Any:
        """Alias for :attr:`corrected_value`."""
        return self.value

    @corrected.setter
    def corrected(self, value: Any):
        self.value = value

    @property
    def corrected_value(self) -> Any:
        """Current (possibly corrected) parsed value."""
        return self.value

    @corrected_value.setter
    def corrected_value(self, value: Any):
        self.value = value

    @property
    def original_value(self) -> str:
        """Original value text as declared in the header."""
        return self.raw_value

    @property
    def raw(self) -> str:
        """Alias for :attr:`original_value`."""
        return self.raw_value

    @property
    def parsed_value(self) -> Any:
        """Value obtained after parsing the original declaration."""
        return self.initial_value

    @property
    def parsed(self) -> Any:
        """Alias for :attr:`parsed_value`."""
        return self.initial_value

    @property
    def original_parsed_value(self) -> Any:
        """Alias for :attr:`parsed_value`."""
        return self.initial_value

    def as_tuple(self) -> Tuple[str, Optional[NRRDFieldType], Any]:
        """Return ``(original declaration, parsed type, corrected value)``."""
        return self.declaration, self.field_type, self.value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (tuple, list)):
            return tuple(self.as_tuple()) == tuple(other)
        if not isinstance(other, NRRDField):
            return NotImplemented
        return (self.name, self.value, self.field_type, self.declaration, self.raw_value, self.delimiter,
                self.is_standard, self.is_custom, self.is_unknown) == \
               (other.name, other.value, other.field_type, other.declaration, other.raw_value, other.delimiter,
                other.is_standard, other.is_custom, other.is_unknown)

    def as_dict(self) -> Dict[str, Any]:
        """Return the field declaration metadata as an ordered dictionary."""
        return OrderedDict([
            ('declaration', self.declaration),
            ('field_type', self.field_type),
            ('parsed_value', self.initial_value),
            ('corrected_value', self.value),
            ('raw_value', self.raw_value),
            ('delimiter', self.delimiter),
            ('is_standard', self.is_standard),
            ('is_custom', self.is_custom),
            ('is_unknown', self.is_unknown),
        ])

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self.as_tuple()[key]
        try:
            return getattr(self, _FIELD_KEY_ALIASES[key])
        except KeyError:
            raise KeyError(key)

    def __len__(self) -> int:
        return 3

    def __contains__(self, key: object) -> bool:
        return key in _FIELD_KEY_ALIASES

    def __iter__(self) -> Iterator[Any]:
        return iter(self.as_tuple())


class NRRDHeader(OrderedDict):
    """Ordered mapping of NRRD header fields with declaration metadata.

    The mapping behaves like the dictionaries returned by previous pynrrd versions.  In addition, every field has an
    :class:`NRRDField` record available through :attr:`fields` describing the original declaration, the field type used
    for parsing, and the current (corrected) value.
    """

    def __init__(self, *args, **kwargs):
        self._fields: 'OrderedDict[str, NRRDField]' = OrderedDict()
        self._duplicates: 'OrderedDict[str, List[NRRDField]]' = OrderedDict()
        super().__init__()
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key: str, value: Any):
        if key in self._fields:
            self._fields[key].value = value
        else:
            self._fields[key] = NRRDField(name=key, value=value)
        super().__setitem__(key, value)

    def __delitem__(self, key: str):
        self._fields.pop(key, None)
        self._duplicates.pop(key, None)
        super().__delitem__(key)

    def clear(self):
        self._fields.clear()
        self._duplicates.clear()
        super().clear()

    def pop(self, key: str, *args):
        self._fields.pop(key, None)
        self._duplicates.pop(key, None)
        return super().pop(key, *args)

    def popitem(self, last: bool = True):
        if not self:
            return super().popitem(last)
        key = next(reversed(self)) if last else next(iter(self))
        value = self[key]
        del self[key]
        return key, value

    def setdefault(self, key: str, default: Any = None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args, **kwargs):
        if len(args) > 1:
            raise TypeError(f'update expected at most 1 argument, got {len(args)}')

        other = args[0] if args else ()
        source_fields = other.fields if isinstance(other, NRRDHeader) else {}
        incoming = OrderedDict(other, **kwargs)

        for key, value in incoming.items():
            if key not in self and key in source_fields:
                self._fields[key] = source_fields[key].clone()
                self._duplicates[key] = [field_record.clone() for field_record in other._duplicates.get(key, [])]
                super().__setitem__(key, value)
            else:
                self[key] = value

    def copy(self) -> 'NRRDHeader':
        return NRRDHeader(self)

    def add_field(self, field: NRRDField, duplicate: bool = False):
        """Add a field record to the header.

        When :obj:`duplicate` is :obj:`True`, the field is retained in :attr:`duplicates` while the first declaration
        remains the primary value in the mapping.
        """
        if field.name in self and duplicate:
            self._duplicates.setdefault(field.name, []).append(field)
            return

        self._fields[field.name] = field
        super().__setitem__(field.name, field.value)

    def sync(self):
        """Reflect field record values back into the mapping."""
        for name, field_record in self._fields.items():
            value = field_record.value
            if name in self and self[name] is not value:
                super().__setitem__(name, value)

    def refresh(self):
        """Alias for :meth:`sync`."""
        self.sync()

    def get_field(self, key: str) -> Optional[NRRDField]:
        """Return the :class:`NRRDField` record for :obj:`key`, if present."""
        return self._fields.get(key)

    def get_declaration(self, key: str) -> Optional[str]:
        """Return the original declaration line for :obj:`key`."""
        field = self._fields.get(key)
        return None if field is None else field.declaration

    def get_field_type(self, key: str) -> Optional[NRRDFieldType]:
        """Return the parsed field type for :obj:`key`."""
        field = self._fields.get(key)
        return None if field is None else field.field_type

    def get_original_value(self, key: str) -> Optional[str]:
        """Return the original value text for :obj:`key`."""
        field = self._fields.get(key)
        return None if field is None else field.raw_value

    def get_corrected_value(self, key: str) -> Any:
        """Return the current (possibly corrected) value for :obj:`key`."""
        field = self._fields.get(key)
        return None if field is None else field.value

    def set_corrected_value(self, key: str, value: Any) -> NRRDField:
        """Set the corrected value for :obj:`key` and return its field record."""
        field = self._fields[key]
        field.corrected_value = value
        self.sync()
        return field

    def get_parsed_value(self, key: str) -> Any:
        """Return the value parsed from the original declaration for :obj:`key`."""
        field = self._fields.get(key)
        return None if field is None else field.parsed_value

    @property
    def fields(self) -> 'OrderedDict[str, NRRDField]':
        """Mapping of field names to their :class:`NRRDField` records."""
        return self._fields

    @property
    def field_map(self) -> 'OrderedDict[str, NRRDField]':
        """Alias for :attr:`fields`."""
        return self._fields

    @property
    def original_fields(self) -> 'OrderedDict[str, NRRDField]':
        """Alias for :attr:`fields`."""
        return self._fields

    @property
    def field_info(self) -> 'OrderedDict[str, NRRDField]':
        """Alias for :attr:`fields`."""
        return self._fields

    @property
    def metadata(self) -> 'OrderedDict[str, NRRDField]':
        """Alias for :attr:`fields`."""
        return self._fields

    @property
    def declarations(self) -> 'OrderedDict[str, str]':
        """Mapping of field names to their original declarations."""
        return OrderedDict((name, field.declaration) for name, field in self._fields.items())

    @property
    def original_declarations(self) -> 'OrderedDict[str, str]':
        """Alias for :attr:`declarations`."""
        return self.declarations

    @property
    def field_types(self) -> 'OrderedDict[str, NRRDFieldType]':
        """Mapping of field names to their parsed field types."""
        return OrderedDict((name, field.field_type) for name, field in self._fields.items())

    @property
    def parsed_types(self) -> 'OrderedDict[str, NRRDFieldType]':
        """Alias for :attr:`field_types`."""
        return self.field_types

    @property
    def original_values(self) -> 'OrderedDict[str, str]':
        """Mapping of field names to their original value text."""
        return OrderedDict((name, field.raw_value) for name, field in self._fields.items())

    @property
    def raw_values(self) -> 'OrderedDict[str, str]':
        """Alias for :attr:`original_values`."""
        return self.original_values

    @property
    def corrected_values(self) -> 'OrderedDict[str, Any]':
        """Mapping of field names to their current (possibly corrected) values."""
        return OrderedDict((name, field.value) for name, field in self._fields.items())

    @property
    def duplicates(self) -> 'OrderedDict[str, List[NRRDField]]':
        """Additional declarations for fields that were declared more than once."""
        return self._duplicates

    @property
    def duplicate_fields(self) -> 'OrderedDict[str, List[NRRDField]]':
        """Alias for :attr:`duplicates`."""
        return self._duplicates

    @property
    def duplicate_declarations(self) -> 'OrderedDict[str, List[str]]':
        """Mapping of field names to the original declarations beyond the first one."""
        return OrderedDict((name, [field_record.declaration for field_record in records])
                           for name, records in self._duplicates.items())

    def get_duplicate_declarations(self, key: str) -> List[str]:
        """Return all declarations after the first for :obj:`key`."""
        return [field_record.declaration for field_record in self._duplicates.get(key, [])]

    def get_all_declarations(self, key: str) -> List[str]:
        """Return every declaration for :obj:`key`, including the primary declaration."""
        field_record = self._fields.get(key)
        declarations = [] if field_record is None else [field_record.declaration]
        declarations.extend(self.get_duplicate_declarations(key))
        return declarations


NRRDHeaderInput = Union[Mapping[str, Any], NRRDHeader]
