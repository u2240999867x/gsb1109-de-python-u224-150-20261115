Measurement tables
==================

.. currentmodule:: nrrd

A NRRD file stores raw samples.  A measurement workflow usually needs physical values, but replacing
every suspicious integer by ``NaN`` destroys information: a missing code, a reserved code, a genuine
zero, and an input ``NaN`` are four different facts.  :func:`to_measurement_table` keeps all of them.

Declaring a source
------------------
Describe each instrument with :class:`SourceDeclaration` (or a mapping with the same field names):
missing and reserved integer codes, an optional raw ``code_range``, an optional physical range, a
global or per-channel ``scale``/``offset``, and a target dtype or target resolution.

.. code-block:: python

    import numpy as np
    import nrrd

    declaration = nrrd.SourceDeclaration(
        name='scanner-a',
        missing_codes=[65535],
        reserved_codes=[10],
        code_range=(0, 65534),
        scale=0.01,
        offset=-5.0,
        units='kPa',
    )
    # equivalently: nrrd.SourceDeclaration.from_mapping({...})

    raw = np.array([[0, 1, 65535], [10, 2, 3]], dtype=np.uint16)
    table = nrrd.to_measurement_table(raw, declaration)

    table.values       # canonical float table, invalid samples are NaN
    table.raw_codes    # exact original uint16 codes
    table.valid        # per-sample bool
    table.status       # 'valid', 'missing', 'reserved', 'nan', ...

The result keeps the source codes and validity as separate arrays.  ``table.raw_codes`` is a copy of
the original array (including its integer dtype); ``table.values`` is the only array intended for
physical calculations.  Every invalid sample is ``NaN`` in ``table.values``, while its category is
preserved in ``table.status`` and the matching mask.  ``table.to_records()`` flattens the same
information into one record per sample, with ``value=None`` for invalid samples so ``NaN`` cannot be
mistaken for a measurement.  ``table.check_consistency()`` verifies these invariants for a manually
built or deserialized table.

Status precedence
-----------------
Each sample has exactly one status, in this order:

1. ``nan`` for an input floating-point NaN.
2. ``nonfinite`` for input positive or negative infinity.
3. ``missing`` for a declared missing code.
4. ``reserved`` for a declared reserved code.
5. ``out_of_range`` for a code outside ``valid_codes``/``code_range``, or a physical value outside
   ``physical_range``.
6. ``uncalibrated`` for a sample whose calibration entry was explicitly ``None``.
7. ``valid`` otherwise.

A raw zero is ``valid`` unless the declaration explicitly lists zero as missing or reserved.  No
heuristic sentinel detection is applied.

Per-axis calibration
--------------------
A calibration for one axis is never broadcast to the whole array.  Use ``axis_calibrations`` with
one entry per axis, either by index or by an axis label from ``labels``:

.. code-block:: python

    declaration = nrrd.SourceDeclaration(
        labels=['row', 'column', 'channel'],
        axis_calibrations={
            'row': nrrd.AxisCalibration(scale=0.5),
            'column': nrrd.AxisCalibration(offset=-1.0),
            'channel': nrrd.AxisCalibration(scale=[1.0, 2.0, 3.0, 4.0]),
        },
    )

An axis that is not covered keeps identity scaling; it is never filled from another axis' value.
Set ``allow_uncalibrated_axes=False`` to require every data axis to be declared explicitly.  A vector
entry of ``None`` for a particular slice marks only that slice ``uncalibrated``.  Global
``scale``/``offset`` and ``axis_calibrations`` cannot be mixed, so an axis-specific value cannot
silently become global.

With a global ``scale``/``offset``, a one-dimensional value is a per-channel vector along
``channel_axis`` (or the last axis when ``channel_axis`` is omitted).  A NumPy array whose shape
exactly matches the data is treated as an element-wise calibration; any other multi-dimensional
shape is rejected.

Reading a declared file
-----------------------
Custom NRRD header fields can carry the declaration, so the file alone is enough:

.. code-block:: text

    missing codes:=65535 10
    reserved codes:=11
    code range:=(0, 65534)
    physical range:=(0, 100)
    scale:=0.5
    offset:=-1

.. code-block:: python

    table = nrrd.read_measurement_table('instrument.nrrd')

When the header and data were already read by another reader, use
``nrrd.measurement_table_from_header(data, header)`` instead.

:func:`declaration_from_header` recognises ``missing code(s)``, ``reserved code(s)``,
``valid code(s)``, ``code range``, ``physical range``/``measurement range``/``range``,
``scale``/``scaling``/``slope``, ``offset``/``intercept``, ``target precision``/``resolution``,
``units`` and ``labels``, plus ``axis <name> scale``/``axis <name> offset`` fields.  Standard NRRD
``min``/``max`` fields are not treated as validity limits because they describe observed extrema.

Lossless conversion
-------------------
The default target dtype is ``float64``.  Before returning, the converter verifies that every valid
integer code can be recovered exactly from the target value after reversing the declared calibration.
If the target dtype cannot represent a code (for example a ``uint32`` code above ``2**24`` with a
``float32`` target, or an integer above ``2**53`` with ``float64``), :class:`PrecisionError` is raised.
The exception reports the target dtype, affected count and an example code.  An explicit absolute
``target_precision`` adds the same check at the physical-resolution level.  There is no silent
rounding; pass ``strict_precision=False`` only when approximate output is explicitly acceptable.

Several instruments
-------------------
Each source is converted with its own declaration, then the canonical tables are concatenated:

.. code-block:: python

    table = nrrd.harmonize_measurement_tables(
        ['scanner-a.nrrd', 'scanner-b.nrrd'],
        [declaration_a, declaration_b],
        axis=0,
    )

Raw codes and status masks stay aligned with the concatenated values.  ``table.sources`` lists the
inputs and ``table.source_index`` records which input produced each sample, so a merged record can
always be traced back to its instrument.  The resulting metadata keeps ``source_declarations`` and
``source_metadata`` so the sentinel and calibration rules of every source remain auditable.  If two
sources use raw integer dtypes that cannot share one exact integer type, the concatenated
``raw_codes`` uses ``object`` dtype to preserve each code exactly rather than promoting through a
lossy float.
