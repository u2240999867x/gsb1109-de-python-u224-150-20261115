How to use
==========

.. currentmodule:: nrrd

Reading NRRD files
------------------
There are three functions that are used to read NRRD files: :meth:`read`, :meth:`read_header`, and :meth:`read_data`. :meth:`read` is a convenience function that opens the specified filepath and calls :meth:`read_header` and :meth:`read_data` in succession to return the NRRD header and data.

Reading NRRD files using :meth:`read` or :meth:`read_data` will return arrays indexed in Fortran-order by default, i.e array elements are accessed by `data[x, y, z]`. This differs from the C-order where array elements are accessed by `data[z, y, x]`, which is the more common order in Python and libraries (e.g. NumPy, scikit-image, PIL, OpenCV). The :obj:`index_order` parameter can be used to specify which index ordering should be used on the returned array ('C' or 'F'). The :obj:`index_order` parameter needs to be consistent with the parameter of same name in :meth:`write`.

The :meth:`read` and :meth:`read_header` methods accept an optional parameter :obj:`custom_field_map` for parsing custom field types not listed in :doc:`/background/fields` of the header. It is a :class:`dict` where the key is the custom field name and the value is a string identifying datatype for the custom field. See :doc:`/background/datatypes` for a list of supported datatypes.

The :meth:`read_data` will typically be called in conjunction with :meth:`read_header` because header information is required in order to read the data. The function returns a :class:`numpy.ndarray` of the data saved in the given NRRD file.

Some NRRD files, while prohibited by specification, may contain duplicated header fields causing an exception to be raised. Changing :data:`nrrd.reader.ALLOW_DUPLICATE_FIELD` to :obj:`True` will show a warning instead of an error while trying to read the file.

Multi-file data
~~~~~~~~~~~~~~~
The ``data file`` field may reference several detached slice files instead of one. :meth:`read` supports the two NRRD spellings::

    data file: LIST
    slice_000.raw
    slice_001.raw
    slice_003.raw

and a printf-style sequence template::

    data file: slice_%03d.raw

Files are assembled in NRRD axis order. A missing middle file becomes a gap at its declared position; later files are never shifted forward. A gap is filled with NaN for floating point data (or with :obj:`gap_value` when supplied), and zero for integer data.

Relative file names are resolved inside the authorized root, which defaults to the directory containing the header file. Use :obj:`data_root` to name a different authorized root. Absolute names that leave that root and symlinks that resolve outside it are rejected.

For multi-file data, :meth:`read` returns a :class:`nrrd.MultiFileVolume`. Its ``segments`` attribute has one entry per slice position with the source name, resolved path, load status, file/data byte counts, and the physical span from the previous retained slice. ``gaps`` lists missing positions, ``mask`` marks retained voxels, and ``axis_span`` is the physical distance between the first and last retained slice.

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
