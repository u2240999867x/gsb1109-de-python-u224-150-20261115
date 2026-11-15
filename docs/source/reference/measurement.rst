Measurement tables
==================

.. currentmodule:: nrrd

Instrument NRRD files commonly disagree about how they encode a missing sample, which raw codes are
reserved, and how a raw integer becomes a physical measurement.  The measurement-table layer converts
those sources to one canonical floating-point table while retaining the original integer codes and a
per-sample validity record.

.. autosummary::

    nrrd.SourceDeclaration
    nrrd.AxisCalibration
    nrrd.MeasurementTable
    nrrd.MeasurementStatus
    nrrd.MEASUREMENT_STATUS_LABELS
    nrrd.to_measurement_table
    nrrd.convert_to_measurement_table
    nrrd.read_measurement_table
    nrrd.measurement_table_from_nrrd
    nrrd.measurement_table_from_header
    nrrd.declaration_from_header
    nrrd.harmonize_measurement_tables
    nrrd.concatenate_measurement_tables
    nrrd.MeasurementError
    nrrd.DeclarationError
    nrrd.CalibrationError
    nrrd.PrecisionError

.. automodule:: nrrd.measurement
    :members: SourceDeclaration, AxisCalibration, MeasurementTable, MeasurementStatus
    :show-inheritance:

.. autofunction:: nrrd.to_measurement_table

.. autofunction:: nrrd.convert_to_measurement_table

.. autofunction:: nrrd.read_measurement_table

.. autofunction:: nrrd.measurement_table_from_nrrd

.. autofunction:: nrrd.measurement_table_from_header

.. autofunction:: nrrd.declaration_from_header

.. autofunction:: nrrd.harmonize_measurement_tables

.. autofunction:: nrrd.concatenate_measurement_tables
