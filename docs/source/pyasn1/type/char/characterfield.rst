
.. _char.characterfield:

Character field provenance
--------------------------

A character value read from a legacy source may be declared as a restricted
type (``TeletexString``, ``PrintableString``, ...) while the same logical
field is declared as :ref:`UTF8String <char.UTF8String>` in a newer source.
:class:`~pyasn1.type.char.CharacterField` keeps the declared ASN.1 type, the
exact wire octets and the standard text side by side, so a cleansing export
does not have to choose among them.

A value that can not be decoded under its declaration is quarantined: the
octets are retained byte-for-byte and the failure is recorded, rather than
substituting question marks. Text outside the declaration's repertoire is
quarantined the same way.

.. autoclass:: pyasn1.type.char.CharacterField
   :members: isQuarantined, kind, originalType, originalOctets, standardText,
             repertoire, getText, getOctets, asRecord, asJsonRecord, asAsn1,
             migrateTo

.. autoclass:: pyasn1.type.char.CharacterFieldSet
   :members: add, getBySource, clean, quarantined, sourceLabels, asRecords,
             asJsonRecords

.. autoclass:: pyasn1.type.char.CharacterFieldIndex
   :members: add, get, getAll, getByKind, getBySource, sourceLabels

.. autoclass:: pyasn1.type.char.MergedCharacterField
   :members: getField, getSource, getSources, getBySource, sourceLabels,
             migrateTo, asRecord

.. autofunction:: pyasn1.type.char.decodeCharacterField

.. autofunction:: pyasn1.type.char.mergeCharacterFields

.. autofunction:: pyasn1.type.char.migrateCharacterField

.. autofunction:: pyasn1.type.char.getCharacterRepertoire

.. autoclass:: pyasn1.type.char.CharacterFieldError

.. autoclass:: pyasn1.type.char.CharacterRepertoireError

.. autoclass:: pyasn1.type.char.QuarantinedCharacterFieldError
