How to use
==========

.. currentmodule:: nrrd

Reading NRRD files
------------------
There are three functions that are used to read NRRD files: :meth:`read`, :meth:`read_header`, and :meth:`read_data`. :meth:`read` is a convenience function that opens the specified filepath and calls :meth:`read_header` and :meth:`read_data` in succession to return the NRRD header and data.

Reading NRRD files using :meth:`read` or :meth:`read_data` will return arrays indexed in Fortran-order by default, i.e array elements are accessed by `data[x, y, z]`. This differs from the C-order where array elements are accessed by `data[z, y, x]`, which is the more common order in Python and libraries (e.g. NumPy, scikit-image, PIL, OpenCV). The :obj:`index_order` parameter can be used to specify which index ordering should be used on the returned array ('C' or 'F'). The :obj:`index_order` parameter needs to be consistent with the parameter of same name in :meth:`write`.

The :meth:`read` and :meth:`read_header` methods accept an optional parameter :obj:`custom_field_map` for parsing custom field types not listed in :doc:`/background/fields` of the header. It is a :class:`dict` where the key is the custom field name and the value is a string identifying datatype for the custom field. See :doc:`/background/datatypes` for a list of supported datatypes. A custom field may not have the same name as a standard field; this raises an :class:`NRRDError` naming the conflicting field.

Headers returned by :meth:`read_header` and :meth:`read` are :class:`NRRDHeader` mappings. In addition to behaving like an ordered dictionary, the header keeps a :class:`NRRDField` record for every field under :attr:`NRRDHeader.fields`. Each record preserves the original declaration line, the parsed field type, the original value text, and the current parsed/corrected value. This makes it possible to correct a field and write the header back without losing how the original file declared it:

.. code-block:: python

    data, header = nrrd.read('input.nrrd', custom_field_map)

    # Inspect the original declaration and parsed type
    header.fields['instrument gain'].original_declaration
    header.fields['instrument gain'].parsed_type

    # Correct the value; the original declaration and parsed value remain available
    header.fields['instrument gain'].corrected_value = 12.5

    nrrd.write('output.nrrd', data, header, custom_field_map=custom_field_map)

Fields that are neither standard nor in the custom field map are preserved as unknown fields. Their original value text is written back verbatim when a header is re-exported, so unknown metadata survives a read/write round-trip even without a custom field map.

The :meth:`read_data` will typically be called in conjunction with :meth:`read_header` because header information is required in order to read the data. The function returns a :class:`numpy.ndarray` of the data saved in the given NRRD file.

Some NRRD files, while prohibited by specification, may contain duplicated header fields causing an exception to be raised. Changing :data:`nrrd.reader.ALLOW_DUPLICATE_FIELD` to :obj:`True` will show a warning instead of an error while trying to read the file, provided all declarations of the field agree. Duplicate declarations with conflicting values always raise an error; the last declaration is never silently selected.

Writing NRRD files
------------------
Writing to NRRD files can be done with the function :meth:`write`. The :obj:`filename` parameter specifies either an absolute or relative filename to write the NRRD file to. If the :obj:`filename` extension is .nhdr, then :obj:`detached_header` will be set to true automatically. If :obj:`detached_header` is :obj:`True` and :obj:`filename` extension is .nrrd, then the header file will have the same path and base name as :obj:`filename` but with an extension of .nhdr. In all other cases, the header and data are saved in the same file.

The :obj:`data` parameter is a :class:`numpy.ndarray` of data to be saved. :obj:`header` is an optional parameter of type :class:`dict` containing the field/values to be saved to the NRRD file.

Writing NRRD files will by default index the :obj:`data` array in Fortran-order where array elements are accessed by `data[x, y, z]`. This differs from C-order where array elements are accessed by `data[z, y, x]`, which is the more common order in Python and libraries (e.g. NumPy, scikit-image, PIL, OpenCV). The :obj:`index_order` parameter can be used to specify which index ordering should be used for the given :obj:`data` array ('C' or 'F').

.. note::

    The following fields are automatically generated based on the :obj:`data` parameter ignoring these values in the :obj:`header`: 'type', 'endian', 'dimension', 'sizes'.

.. note::

    The default encoding field used if not specified in :obj:`header` is 'gzip'.

.. note::

    The :obj:`index_order` parameter must be consistent with the index order specified in :meth:`read`. Reading an NRRD file in C-order and then writing as Fortran-order or vice versa will result in the data being transposed in the NRRD file.
