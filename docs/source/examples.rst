Examples
========

.. currentmodule:: nrrd

Basic Example
-------------
.. code-block:: python

    import numpy as np
    import nrrd

    data = np.linspace(1, 50, 50)
    nrrd.write('output.nrrd', data)

    data2, header = nrrd.read('output.nrrd')
    print(np.all(data == data2))
    >>> True

    print(header)
    >>> OrderedDict([('type', 'double'), ('dimension', 1), ('sizes', array([50])), ('endian', 'little'), ('encoding', 'gzip')])

Example only reading header
---------------------------
.. code-block:: python

    import numpy as np
    import nrrd

    data = np.linspace(1, 50, 50)
    nrrd.write('output.nrrd', data)

    header = nrrd.read_header('output.nrrd')
    print(header)
    >>> OrderedDict([('type', 'double'), ('dimension', 1), ('sizes', array([50])), ('endian', 'little'), ('encoding', 'gzip')])

Example write and read from memory
----------------------------------
.. code-block:: python

    import io
    import numpy as np
    import nrrd

    memory_nrrd = io.BytesIO()

    data = np.linspace(1, 50, 50)
    nrrd.write(memory_nrrd, data)

    memory_nrrd.seek(0)

    header = nrrd.read_header(memory_nrrd)

    print(header)
    >>> OrderedDict([('type', 'double'), ('dimension', 1), ('sizes', array([50])), ('endian', 'little'), ('encoding', 'gzip')])

    data2 = nrrd.read_data(header, memory_nrrd)

    print(np.all(data == data2))
    >>> True

Example with fields and custom fields
-------------------------------------
.. code-block:: python

    import numpy as np
    import nrrd

    data = np.linspace(1, 60, 60).reshape((3, 10, 2))
    header = {'kinds': ['domain', 'domain', 'domain'], 'units': ['mm', 'mm', 'mm'], 'spacings': [1.0458, 1.0458, 2.5], 'space': 'right-anterior-superior', 'space directions': np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]]), 'encoding': 'ASCII', 'custom_field_here1': 24.34, 'custom_field_here2': np.array([1, 2, 3, 4])}
    custom_field_map = {'custom_field_here1': 'double', 'custom_field_here2': 'int list'}

    nrrd.write('output.nrrd', data, header, custom_field_map=custom_field_map)

    data2, header2 = nrrd.read('output.nrrd', custom_field_map)

    print(np.all(data == data2))
    >>> True

    print(header)
    >>> {'units': ['mm', 'mm', 'mm'], 'spacings': [1.0458, 1.0458, 2.5], 'custom_field_here1': 24.34, 'space': 'right-anterior-superior', 'space directions': array([[1, 0, 0],
       [0, 1, 0],
       [0, 0, 1]]), 'type': 'double', 'encoding': 'ASCII', 'kinds': ['domain', 'domain', 'domain'], 'dimension': 3, 'custom_field_here2': array([1, 2, 3, 4]), 'sizes': [3, 10, 2]}

    print(header2)
    >>> OrderedDict([('type', 'double'), ('dimension', 3), ('space', 'right-anterior-superior'), ('sizes', array([ 3, 10,  2])), ('space directions', array([[1., 0., 0.],
       [0., 1., 0.],
       [0., 0., 1.]])), ('kinds', ['domain', 'domain', 'domain']), ('encoding', 'ASCII'), ('spacings', array([1.0458, 1.0458, 2.5   ])), ('units', ['mm', 'mm', 'mm']), ('custom_field_here1', 24.34), ('custom_field_here2', array([1, 2, 3, 4]))])

Example reading NRRD file with duplicated header field
------------------------------------------------------
.. code-block:: python

    import nrrd

    # Set this field to True to enable the reading of files with duplicated header fields
    nrrd.reader.ALLOW_DUPLICATE_FIELD = True

    # Name of the file you want to read with a duplicated header field
    filename = "filename.nrrd"

    # Read the file
    # filedata = numpy array
    # fileheader = header of the NRRD file
    # A warning is now received about duplicate headers rather than an error being thrown
    filedata, fileheader = nrrd.read(filename)
    >>> UserWarning: Duplicate header field: 'space' warnings.warn(dup_message)

Example reading and writing NRRD files with C-order indexing
------------------------------------------------------------
.. code-block:: python

    import numpy as np
    import nrrd

    # Treat this data array as a 3D volume using C-order indexing
    # This means we have a volume with a shape of 600x800x70 (x by y by z)
    data = np.zeros((70, 800, 600))
    # Save the NRRD object with the correct index order
    nrrd.write('output.nrrd', data, index_order='C')

    # Read the NRRD file with C-order indexing
    # Note: We can specify either index order here, this is just a preference unlike in nrrd.write where
    # it MUST be set based on the data setup
    data, header = nrrd.read('output.nrrd', index_order='C')

    # The data shape is exactly the same shape as what we wrote
    print(data.shape)
    >>> (70, 800, 600)

    # But the shape saved in the header is in Fortran-order
    print(header['sizes'])
    >>> [600 800  70]

    # Read the NRRD file with Fortran-order indexing now
    data, header = nrrd.read('output.nrrd', index_order='F')

    # The data shape is exactly the same shape as what we wrote
    # The data shape is now in Fortran order, or transposed from what we wrote
    print(data.shape)
    >>> (600, 800, 70)
    print(header['sizes'])
    >>> [600 800  70]

Example transforming a batch of vector fields into world components
--------------------------------------------------------------------
Each three-component vector field may carry its own ``measurement frame``. The array returned by :meth:`read`
holds the raw, on-disk components (measured in that frame); :meth:`world_components` returns the components in
world coordinates without modifying the raw array. The index order used when reading must be passed through so the
component axis is located correctly.

.. code-block:: python

    import nrrd

    for filename in ['vector_field_1.nrrd', 'vector_field_2.nrrd']:
        # Raw components as stored on disk, in the per-file measurement frame
        raw_data, header = nrrd.read(filename, index_order='F')

        # The component axis is identified from 'kinds', not guessed from the shape,
        # so a scalar batch is never mistaken for a one-component vector.
        vector_axis = nrrd.find_vector_axis(header)

        # World components: v_world = M @ v_measurement. Raises NRRDError if the
        # measurement frame is missing, non-finite or singular, instead of inventing values.
        world_data = nrrd.world_components(raw_data, header, index_order='F')

        # Recover the original measurement-frame components when needed
        measurement_data = nrrd.measurement_components(world_data, header, index_order='F')

        print(filename, raw_data.shape, world_data.shape, vector_axis)

    # The same call works for a C-ordered array; only the axis position changes.
    raw_c, header = nrrd.read('vector_field_1.nrrd', index_order='C')
    world_c = nrrd.world_components(raw_c, header, index_order='C')
