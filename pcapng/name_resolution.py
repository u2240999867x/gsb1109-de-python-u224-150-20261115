"""
Address dimension tables built from Name Resolution Blocks.

This module is an alias of :py:mod:`pcapng.names`; both import paths expose
the same objects.
"""

from pcapng.names import (  # noqa: F401
    AddressBook,
    AddressDimension,
    AddressDimensionTable,
    AddressEntry,
    AddressRow,
    AddressTable,
    NameDeclaration,
    NameResolutionTable,
    ResolutionResult,
    build_address_dimension,
    build_address_table,
    build_name_resolution_table,
    declarations_for_block,
)

__all__ = [
    "AddressBook",
    "AddressDimension",
    "AddressDimensionTable",
    "AddressEntry",
    "AddressRow",
    "AddressTable",
    "NameDeclaration",
    "NameResolutionTable",
    "ResolutionResult",
    "build_address_dimension",
    "build_address_table",
    "build_name_resolution_table",
    "declarations_for_block",
]
