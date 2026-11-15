How to use
==========

.. currentmodule:: nrrd

Reading NRRD files
------------------
There are three functions that are used to read NRRD files: :meth:`read`, :meth:`read_header`, and :meth:`read_data`. :meth:`read` is a convenience function that opens the specified filepath and calls :meth:`read_header` and :meth:`read_data` in succession to return the NRRD header and data.

Reading NRRD files using :meth:`read` or :meth:`read_data` will return arrays indexed in Fortran-order by default, i.e array elements are accessed by `data[x, y, z]`. This differs from the C-order where array elements are accessed by `data[z, y, x]`, which is the more common order in Python and libraries (e.g. NumPy, scikit-image, PIL, OpenCV). The :obj:`index_order` parameter can be used to specify which index ordering should be used on the returned array ('C' or 'F'). The :obj:`index_order` parameter needs to be consistent with the parameter of same name in :meth:`write`.

The :meth:`read` and :meth:`read_header` methods accept an optional parameter :obj:`custom_field_map` for parsing custom field types not listed in :doc:`/background/fields` of the header. It is a :class:`dict` where the key is the custom field name and the value is a string identifying datatype for the custom field. See :doc:`/background/datatypes` for a list of supported datatypes.

The :meth:`read_data` will typically be called in conjunction with :meth:`read_header` because header information is required in order to read the data. The function returns a :class:`numpy.ndarray` of the data saved in the given NRRD file.

Reading an index region
-----------------------
:meth:`read_region` (and :meth:`read`/:meth:`read_data` with the :obj:`region` argument) reads a single contiguous index region instead of the whole data set. The region has one entry per axis and is expressed in the same :obj:`index_order` as the returned array, so C-order callers pass C-order ranges and Fortran-order callers pass Fortran-order ranges. Each entry may be an inclusive ``(start, stop)`` pair, a :class:`slice` with unit step, a single integer, or a contiguous sequence of indices; :obj:`None` selects the whole axis.

.. code-block:: python

    # Fortran-order: (x, y, z) selection
    data, header, info = nrrd.read_region('file.nrrd', [(10, 19), None, (5, 9)])

    # C-order: (z, y, x) selection
    data, header, info = nrrd.read_region('file.nrrd', [(5, 9), None, (10, 19)], index_order='C')

The returned header is cropped: ``sizes`` describes the selected region, ``space origin`` is shifted by the removed voxels, and location fields such as ``data file``/``byteskip`` are dropped. ``info`` is a :class:`nrrd.types.RegionInfo` containing the original full ``sizes`` plus the inclusive ``starts``/``stops`` of the selection, so region-local indices can be mapped back with :meth:`nrrd.types.RegionInfo.to_original`.

For the uncompressed ``raw`` encoding only the selected bytes are read. Reads follow the actual storage layout (in a NRRD file the first axis varies fastest): each contiguous run of selected values is fetched with one seek/read, and runs are coalesced when all faster axes are fully selected. Selecting one slice of a 3D volume therefore reads one contiguous block rather than the whole file.

.. note::

    Bounded reads apply to the ``raw`` encoding only. ``gzip``/``bzip2`` streams have ``sizes`` in the header but no index into the decompressed stream, and ``ASCII`` data must be tokenized, so those encodings are decoded in full before the region is extracted. In that case a :class:`UserWarning` states how much data had to be decoded, and ``info.full_decode`` is :obj:`True`. The library does not claim to read only the selected region for these encodings.

Some NRRD files, while prohibited by specification, may contain duplicated header fields causing an exception to be raised. Changing :data:`nrrd.reader.ALLOW_DUPLICATE_FIELD` to :obj:`True` will show a warning instead of an error while trying to read the file.

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
