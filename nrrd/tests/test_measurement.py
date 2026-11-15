import os
import tempfile
import unittest

import numpy as np

import nrrd


class TestMeasurementTable(unittest.TestCase):
    def test_missing_reserved_zero_and_nan_are_distinct(self):
        data = np.array([0, 1, 2, 3, np.nan], dtype=np.float64)
        declaration = nrrd.SourceDeclaration(missing_codes=[1], reserved_codes=[2])

        table = nrrd.to_measurement_table(data, declaration)

        np.testing.assert_equal(table.status, ['valid', 'missing', 'reserved', 'valid', 'nan'])
        np.testing.assert_array_equal(table.valid, [True, False, False, True, False])
        np.testing.assert_array_equal(table.missing, [False, True, False, False, False])
        np.testing.assert_array_equal(table.reserved, [False, False, True, False, False])
        np.testing.assert_array_equal(table.nan, [False, False, False, False, True])
        np.testing.assert_equal(table.values[0], 0.0)
        self.assertTrue(np.isnan(table.values[1]))
        self.assertTrue(table.valid[0], 'a genuine raw zero must remain a real measurement')
        self.assertFalse(table.valid[1], 'a missing code must not be treated as zero')
        self.assertFalse(table.valid[2], 'a reserved code must not be treated as missing')

    def test_original_integer_codes_are_preserved(self):
        data = np.array([[0, 1, 65535], [10, 20, 30]], dtype=np.uint16)
        declaration = nrrd.SourceDeclaration(missing_codes=[65535], reserved_codes=[10])

        table = nrrd.to_measurement_table(data, declaration)

        np.testing.assert_array_equal(table.raw_codes, data)
        self.assertEqual(table.raw_codes.dtype, np.uint16)
        np.testing.assert_equal(table.status, [['valid', 'valid', 'missing'], ['reserved', 'valid', 'valid']])
        self.assertIsNot(table.raw_codes, data)
        np.testing.assert_array_equal(table.original_codes, data)

    def test_global_scale_and_offset(self):
        data = np.array([0, 1, 2], dtype=np.int16)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(scale=0.5, offset=-1.0))

        np.testing.assert_allclose(table.values, [-1.0, -0.5, 0.0])

    def test_per_channel_scale(self):
        data = np.arange(6, dtype=np.int16).reshape(2, 3)
        declaration = nrrd.SourceDeclaration(scale=[1.0, 2.0, 4.0], channel_axis=1)

        table = nrrd.to_measurement_table(data, declaration)

        np.testing.assert_allclose(table.values, data * np.array([1.0, 2.0, 4.0]))

    def test_negative_scale(self):
        data = np.array([0, 1, 2], dtype=np.int16)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(scale=-2.0, offset=10.0))

        np.testing.assert_allclose(table.values, [10.0, 8.0, 6.0])

    def test_code_range_and_physical_range(self):
        data = np.array([0, 1, 2, 5], dtype=np.uint16)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(code_range=(0, 2), scale=1.0))
        np.testing.assert_equal(table.status, ['valid', 'valid', 'valid', 'out_of_range'])

        spot = np.array([0, 5, 10, 15], dtype=np.uint8)
        table = nrrd.to_measurement_table(spot, nrrd.SourceDeclaration(physical_range=(2, 12)))
        np.testing.assert_equal(table.status, ['out_of_range', 'valid', 'valid', 'out_of_range'])
        np.testing.assert_equal(table.values[[0, 3]], [np.nan, np.nan])

    def test_missing_code_outside_code_range_is_still_missing(self):
        data = np.array([1, 2, 3], dtype=np.int16)
        table = nrrd.to_measurement_table(
            data, nrrd.SourceDeclaration(missing_codes=[3], code_range=(1, 2)))

        np.testing.assert_equal(table.status, ['valid', 'valid', 'missing'])
        self.assertFalse(table.out_of_range[2])

    def test_valid_codes_restrict_the_set(self):
        data = np.array([1, 2, 3], dtype=np.int16)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(valid_codes=[1, 2]))

        np.testing.assert_equal(table.status, ['valid', 'valid', 'out_of_range'])

    def test_nan_and_infinity_are_not_missing_codes(self):
        data = np.array([np.nan, np.inf, -np.inf, 0.0], dtype=np.float64)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(missing_codes=[0]))

        np.testing.assert_equal(table.status, ['nan', 'nonfinite', 'nonfinite', 'missing'])
        np.testing.assert_array_equal(table.nan, [True, False, False, False])
        np.testing.assert_array_equal(table.nonfinite, [False, True, True, False])
        np.testing.assert_array_equal(table.missing, [False, False, False, True])

    def test_status_masks_are_mutually_exclusive(self):
        data = np.array([np.nan, 3.0, 1.0, 2.0], dtype=np.float64)
        table = nrrd.to_measurement_table(
            data, nrrd.SourceDeclaration(missing_codes=[1], reserved_codes=[2],
                                         code_range=(0, 2), physical_range=(0.0, 2.0)))
        masks = np.stack([table.valid, table.missing, table.reserved, table.nan,
                          table.nonfinite, table.out_of_range, table.uncalibrated])
        np.testing.assert_array_equal(masks.sum(axis=0), np.ones(data.shape, dtype=int))

    def test_integer_codes_are_not_cast_through_the_data_dtype(self):
        data = np.array([0, 1, 2], dtype=np.uint8)
        # 256 is outside uint8, so it cannot be a valid sentinel.  It must be ignored instead of
        # wrapping to 0 and marking the real zero as missing.
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(missing_codes=[256]))
        np.testing.assert_array_equal(table.missing, [False, False, False])

    def test_sentinel_on_float_source_does_not_overflow(self):
        data = np.array([0.0, 1.0, 1.8446744e19], dtype=np.float32)
        table = nrrd.to_measurement_table(
            data, nrrd.SourceDeclaration(missing_codes=[int(np.iinfo(np.uint64).max)]))
        np.testing.assert_equal(table.status, ['valid', 'valid', 'missing'])

    def test_large_sentinel_keeps_exact_value(self):
        data = np.array([0, 1, np.iinfo(np.uint64).max], dtype=np.uint64)
        table = nrrd.to_measurement_table(
            data, nrrd.SourceDeclaration(missing_codes=[str(np.iinfo(np.uint64).max)],
                                         strict_precision=False))

        np.testing.assert_equal(table.status, ['valid', 'valid', 'missing'])
        self.assertEqual(int(table.raw_codes[2]), int(np.iinfo(np.uint64).max))

    def test_to_records(self):
        data = np.array([0, 1, 2], dtype=np.int16)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(name='scanner-a', missing_codes=[1],
                                                                       units='kPa'))
        records = table.to_records()
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]['raw_code'], 0)
        self.assertEqual(records[0]['value'], 0.0)
        self.assertTrue(records[0]['valid'])
        self.assertIsNone(records[1]['value'])
        self.assertEqual(records[1]['status'], 'missing')
        self.assertEqual(records[1]['source'], 'scanner-a')
        self.assertEqual(records[1]['units'], 'kPa')

        valid_records = table.to_records(include_invalid=False)
        self.assertEqual([record['raw_code'] for record in valid_records], [0, 2])

    def test_as_dict_keeps_raw_and_validity_separate(self):
        table = nrrd.to_measurement_table(np.array([0, 1], dtype=np.int16),
                                          nrrd.SourceDeclaration(missing_codes=[1]))
        exported = table.as_dict()
        self.assertIs(exported['raw_codes'].dtype, table.raw_codes.dtype)
        self.assertFalse(exported['valid'][1])
        self.assertTrue(np.isnan(exported['values'][1]))

    def test_aliases(self):
        table = nrrd.to_measurement_table(np.array([0, 1], dtype=np.int16),
                                          nrrd.SourceDeclaration(missing_codes=[1]))
        self.assertIs(table.data, table.values)
        self.assertIs(table.raw, table.raw_codes)
        self.assertIs(table.validity, table.valid)
        self.assertIs(table.missing_mask, table.missing)
        self.assertIs(table.reserved_mask, table.reserved)


class TestAxisCalibration(unittest.TestCase):
    def test_scalar_calibration_applies_only_along_its_axis(self):
        data = np.arange(2 * 3, dtype=np.int16).reshape(2, 3)
        declaration = nrrd.SourceDeclaration(
            labels=['rows', 'columns'],
            axis_calibrations={
                'rows': nrrd.AxisCalibration(scale=2.0),
                'columns': nrrd.AxisCalibration(offset=1.0),
            },
        )

        table = nrrd.to_measurement_table(data, declaration)

        np.testing.assert_allclose(table.values, data * 2.0 + 1.0)

    def test_uncovered_axis_keeps_identity_instead_of_broadcasting(self):
        data = np.arange(2 * 3 * 4, dtype=np.int16).reshape(2, 3, 4)
        declaration = nrrd.SourceDeclaration(
            labels=['x', 'y', 'z'],
            axis_calibrations={'x': nrrd.AxisCalibration(scale=2.0)},
        )
        table = nrrd.to_measurement_table(data, declaration)
        np.testing.assert_allclose(table.values, data * 2.0)
        np.testing.assert_allclose(table.values[0, 0], data[0, 0] * 2.0)

    def test_full_coverage_can_be_required(self):
        data = np.zeros((2, 3, 4), dtype=np.int16)
        declaration = nrrd.SourceDeclaration(
            labels=['x', 'y', 'z'],
            axis_calibrations={'x': nrrd.AxisCalibration(scale=2.0)},
            allow_uncalibrated_axes=False,
        )
        with self.assertRaisesRegex(nrrd.CalibrationError, 'not covered'):
            nrrd.to_measurement_table(data, declaration)

    def test_axis_vector_with_missing_entry_marks_only_that_slice(self):
        data = np.zeros((2, 3, 4), dtype=np.int16)
        data[:, :, :] = np.arange(4, dtype=np.int16)
        declaration = nrrd.SourceDeclaration(
            labels=['x', 'y', 'z'],
            axis_calibrations={
                0: nrrd.AxisCalibration(scale=1.0),
                1: nrrd.AxisCalibration(scale=1.0),
                2: nrrd.AxisCalibration(scale=[None, 2.0, 3.0, None]),
            },
        )

        table = nrrd.to_measurement_table(data, declaration)

        np.testing.assert_equal(table.status[0, 0], ['uncalibrated', 'valid', 'valid', 'uncalibrated'])
        np.testing.assert_array_equal(table.uncalibrated[0, 0], [True, False, False, True])
        np.testing.assert_allclose(table.values[0, 0, 1:3], [2.0, 6.0])
        self.assertTrue(np.all(np.isnan(table.values[0, 0, [0, 3]])))

    def test_elementwise_calibration_array_marks_none_entries(self):
        data = np.arange(6, dtype=np.int16).reshape(2, 3)
        declaration = nrrd.SourceDeclaration(
            scale=np.array([[1.0, None, 3.0], [4.0, 5.0, None]], dtype=object))
        table = nrrd.to_measurement_table(data, declaration)
        np.testing.assert_equal(table.status, [['valid', 'uncalibrated', 'valid'],
                                               ['valid', 'valid', 'uncalibrated']])
        np.testing.assert_allclose(table.values[[0, 0, 1, 1], [0, 2, 0, 1]], [0.0, 6.0, 12.0, 20.0])

    def test_wrong_elementwise_calibration_shape_is_rejected(self):
        with self.assertRaisesRegex(nrrd.CalibrationError, 'does not match data shape'):
            nrrd.to_measurement_table(np.zeros((2, 3), dtype=np.int16),
                                      nrrd.SourceDeclaration(scale=np.ones((2, 2))))

    def test_axis_key_must_match_declared_axis(self):
        data = np.zeros((2, 3), dtype=np.int16)
        with self.assertRaisesRegex(nrrd.CalibrationError, 'declares a different axis'):
            nrrd.to_measurement_table(
                data, nrrd.SourceDeclaration(
                    axis_calibrations={0: nrrd.AxisCalibration(axis=1, scale=2.0)}))

    def test_table_equality_is_nan_aware(self):
        first = nrrd.to_measurement_table(np.array([0, np.nan], dtype=np.float64))
        second = nrrd.to_measurement_table(np.array([0, np.nan], dtype=np.float64))
        self.assertEqual(first, second)
        self.assertNotEqual(first, nrrd.to_measurement_table(np.array([0, 1], dtype=np.float64)))

    def test_table_consistency_check(self):
        table = nrrd.to_measurement_table(
            np.array([0, 1, np.nan], dtype=np.float64),
            nrrd.SourceDeclaration(missing_codes=[1]))
        table.check_consistency()

    def test_global_and_axis_calibration_cannot_be_mixed(self):
        with self.assertRaisesRegex(nrrd.CalibrationError, 'cannot be mixed'):
            nrrd.to_measurement_table(
                np.zeros((2, 3), dtype=np.int16),
                nrrd.SourceDeclaration(scale=2.0, labels=['x', 'y'],
                                       axis_calibrations={'x': nrrd.AxisCalibration(scale=2.0),
                                                          'y': nrrd.AxisCalibration(scale=3.0)}))

    def test_axis_label_resolution(self):
        data = np.ones((2, 3), dtype=np.int16)
        declaration = nrrd.SourceDeclaration(labels=['x', 'y'],
                                            axis_calibrations={'x': {'scale': [2.0, 3.0]},
                                                               'y': {'scale': [1.0, 10.0, 100.0]}})
        table = nrrd.to_measurement_table(data, declaration)
        np.testing.assert_allclose(table.values,
                                   np.array([[2.0, 20.0, 200.0], [3.0, 30.0, 300.0]]))

    def test_axis_order_does_not_change_the_affine_result(self):
        data = np.arange(2 * 3 * 4, dtype=np.int32).reshape(2, 3, 4)
        declaration = nrrd.SourceDeclaration(
            labels=['x', 'y', 'z'],
            axis_calibrations={
                0: nrrd.AxisCalibration(scale=0.5),
                1: nrrd.AxisCalibration(offset=2.0),
                2: nrrd.AxisCalibration(scale=[1.0, 2.0, 3.0, 4.0]),
            },
        )
        table = nrrd.to_measurement_table(data, declaration)
        expected = data * 0.5 * np.array([1.0, 2.0, 3.0, 4.0]) + 2.0
        np.testing.assert_allclose(table.values, expected)

    def test_calibration_marked_inapplicable_does_not_broadcast(self):
        data = np.ones((2, 3), dtype=np.int16)
        declaration = nrrd.SourceDeclaration(
            labels=['x', 'y'],
            axis_calibrations={'x': nrrd.AxisCalibration(scale=2.0),
                               'y': nrrd.AxisCalibration(scale=10.0, applicable=False)},
        )
        table = nrrd.to_measurement_table(data, declaration)
        np.testing.assert_equal(table.status, [['uncalibrated'] * 3, ['uncalibrated'] * 3])
        self.assertTrue(np.all(np.isnan(table.values)))

    def test_zero_scale_is_rejected(self):
        with self.assertRaisesRegex(nrrd.DeclarationError, 'scale must not be zero'):
            nrrd.to_measurement_table(np.zeros((2,), dtype=np.int16), nrrd.SourceDeclaration(scale=0.0))


class TestPrecision(unittest.TestCase):
    def test_large_integer_code_beyond_float32_is_rejected(self):
        data = np.array([0, 1, 16_777_217], dtype=np.uint32)
        with self.assertRaises(nrrd.PrecisionError) as context:
            nrrd.to_measurement_table(data, nrrd.SourceDeclaration(target_dtype=np.float32))
        self.assertEqual(context.exception.failing_count, 1)
        self.assertEqual(context.exception.example, 16_777_217)
        self.assertEqual(context.exception.target_dtype, np.dtype(np.float32))

    def test_integer_code_beyond_float64_is_rejected(self):
        data = np.array([2 ** 53 + 1], dtype=np.uint64)
        with self.assertRaisesRegex(nrrd.PrecisionError, 'losslessly'):
            nrrd.to_measurement_table(data, nrrd.SourceDeclaration(target_dtype=np.float64))

    def test_representable_large_integer_code_is_accepted(self):
        data = np.array([2 ** 53], dtype=np.uint64)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(target_dtype=np.float64))
        self.assertEqual(int(table.values[0]), 2 ** 53)

    def test_target_precision_rejects_too_fine_differences(self):
        data = np.arange(10, dtype=np.uint16)
        with self.assertRaisesRegex(nrrd.PrecisionError, 'target_precision'):
            nrrd.to_measurement_table(data, nrrd.SourceDeclaration(scale=0.01, target_precision=0.02))
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(scale=0.01, target_precision=0.01))
        np.testing.assert_allclose(table.values, data * 0.01)

    def test_strict_precision_can_be_disabled(self):
        data = np.array([2 ** 53 + 1], dtype=np.uint64)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(strict_precision=False))
        self.assertTrue(np.isfinite(table.values[0]))

    def test_float_source_to_narrower_dtype_is_rejected(self):
        data = np.array([0.1, 0.2], dtype=np.float64)
        with self.assertRaises(nrrd.PrecisionError):
            nrrd.to_measurement_table(data, nrrd.SourceDeclaration(target_dtype=np.float32))
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(target_dtype=np.float64))
        np.testing.assert_array_equal(table.values, data)


class TestDeclaration(unittest.TestCase):
    def test_no_missing_code_is_invented(self):
        data = np.array([0, 0, 0], dtype=np.uint16)
        table = nrrd.to_measurement_table(data)
        np.testing.assert_array_equal(table.valid, [True, True, True])
        np.testing.assert_array_equal(table.values, [0.0, 0.0, 0.0])

    def test_code_cannot_be_missing_and_reserved(self):
        with self.assertRaisesRegex(nrrd.DeclarationError, 'both missing and reserved'):
            nrrd.SourceDeclaration(missing_codes=[1], reserved_codes=[1])

    def test_invalid_declaration_ranges(self):
        with self.assertRaises(nrrd.DeclarationError):
            nrrd.SourceDeclaration(code_range=(5, 1))
        with self.assertRaises(nrrd.DeclarationError):
            nrrd.SourceDeclaration(physical_range=(1.0, 0.0))
        with self.assertRaises(nrrd.DeclarationError):
            nrrd.SourceDeclaration(target_precision=0)
        with self.assertRaises(nrrd.DeclarationError):
            nrrd.SourceDeclaration(target_dtype=np.int16)

    def test_from_mapping_aliases(self):
        declaration = nrrd.SourceDeclaration.from_mapping({
            'missing code': '65535 10',
            'reserved': '11',
            'range': '(0, 100)',
            'slope': 0.5,
            'intercept': -1.0,
            'resolution': 1e-3,
        })
        self.assertEqual(declaration.missing_codes, (65535, 10))
        self.assertEqual(declaration.reserved_codes, (11,))
        self.assertEqual(declaration.physical_range, (0.0, 100.0))
        self.assertEqual(declaration.scale, 0.5)
        self.assertEqual(declaration.offset, -1.0)
        self.assertEqual(declaration.target_precision, 1e-3)

    def test_string_labels_are_split(self):
        declaration = nrrd.SourceDeclaration(labels='"row" "column"')
        self.assertEqual(declaration.labels, ('row', 'column'))

    def test_declaration_equality_handles_array_calibration(self):
        first = nrrd.SourceDeclaration(scale=np.array([1.0, 2.0]),
                                       axis_calibrations={'x': nrrd.AxisCalibration(scale=np.array([1.0, 2.0]))})
        second = nrrd.SourceDeclaration(scale=[1.0, 2.0],
                                        axis_calibrations={'x': nrrd.AxisCalibration(scale=[1.0, 2.0])})
        self.assertEqual(first, second)
        self.assertNotEqual(first, nrrd.SourceDeclaration(scale=[1.0, 3.0]))

    def test_string_scalars_are_accepted(self):
        declaration = nrrd.SourceDeclaration(scale='0.5', offset='-1')
        table = nrrd.to_measurement_table(np.array([0, 1], dtype=np.int16), declaration)
        np.testing.assert_allclose(table.values, [-1.0, -0.5])


class TestHeaderDeclaration(unittest.TestCase):
    def test_header_fields_are_parsed(self):
        header = {
            'missing codes': '65535 10',
            'reserved codes': '11',
            'code range': '(0, 65534)',
            'physical range': '(0, 100)',
            'scale': '0.5',
            'offset': '-1',
            'units': 'kPa',
            'labels': '"row" "column"',
            'target precision': '1e-3',
        }
        declaration = nrrd.declaration_from_header(header)
        self.assertEqual(declaration.missing_codes, (65535, 10))
        self.assertEqual(declaration.reserved_codes, (11,))
        self.assertEqual(declaration.code_range, (0, 65534))
        self.assertEqual(declaration.physical_range, (0.0, 100.0))
        self.assertEqual(declaration.target_precision, 1e-3)

    def test_header_axis_fields(self):
        declaration = nrrd.declaration_from_header({
            'labels': ['x', 'y'],
            'axis x scale': '2.0',
            'axis y offset': '1.0',
        })
        self.assertEqual(declaration.axis_calibrations, {'x': {'scale': '2.0'}, 'y': {'offset': '1.0'}})

    def test_header_alias_spellings(self):
        declaration = nrrd.declaration_from_header({
            'Missing Code(s)': '1 2',
            'Reserved Values': '3',
            'Measurement Range': '(0, 10)',
            'Slope': '2',
            'Bias': '1',
            'Resolution': '0.5',
        })
        self.assertEqual(declaration.missing_codes, (1, 2))
        self.assertEqual(declaration.reserved_codes, (3,))
        self.assertEqual(declaration.physical_range, (0.0, 10.0))
        self.assertEqual(declaration.scale, '2')
        self.assertEqual(declaration.offset, '1')
        self.assertEqual(declaration.target_precision, 0.5)

    def test_standard_min_max_is_not_a_validity_range(self):
        declaration = nrrd.declaration_from_header({'min': 0.0, 'max': 1.0})
        self.assertIsNone(declaration.physical_range)

    def test_unknown_fields_are_ignored(self):
        declaration = nrrd.declaration_from_header({'content': 'x', 'foo': 'bar'})
        self.assertEqual(declaration, nrrd.SourceDeclaration())

    def test_read_measurement_table_uses_header_declaration(self):
        data = np.array([[0, 1, 65535], [10, 2, 11]], dtype=np.uint16)
        header = {
            'missing codes': '65535 10',
            'reserved codes': '11',
            'scale': '0.5',
            'offset': '-1',
        }
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, header, index_order='F')

            table = nrrd.read_measurement_table(filename, index_order='C')

        np.testing.assert_equal(table.status,
                                [['valid', 'missing'], ['valid', 'valid'], ['missing', 'reserved']])
        np.testing.assert_allclose(table.values[[0, 1, 0], [0, 1, 0]], [-1.0, 0.0, -1.0])
        self.assertEqual(table.source, filename)

    def test_measurement_table_from_header(self):
        data = np.array([0, 1, 65535], dtype=np.uint16)
        table = nrrd.measurement_table_from_header(
            data, {'missing codes': '65535', 'scale': '0.5', 'units': 'kPa'})
        np.testing.assert_equal(table.status, ['valid', 'valid', 'missing'])
        np.testing.assert_allclose(table.values[:2], [0.0, 0.5])

    def test_explicit_axis_declaration_is_interpreted_in_data_order(self):
        data = np.arange(2 * 3 * 4, dtype=np.int16).reshape(4, 3, 2)
        header = {'labels': ['x', 'y', 'z']}
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, header, index_order='C')

            table = nrrd.read_measurement_table(
                filename,
                nrrd.SourceDeclaration(axis_calibrations={0: nrrd.AxisCalibration(scale=10.0)}),
                index_order='C', merge_header=False)

        # Axis 0 is the first axis of the C-order data array (z), not the header's first NRRD axis (x).
        np.testing.assert_allclose(table.values, data * 10.0)

    def test_numeric_header_axis_key_is_translated_in_c_order(self):
        data = np.arange(2 * 3 * 4, dtype=np.int16).reshape(4, 3, 2)
        header = {'labels': ['x', 'y', 'z'], 'axis 0 scale': '10'}
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, header, index_order='C')

            table = nrrd.read_measurement_table(filename, index_order='C')

        # Header axis 0 is x, i.e. the last axis of the C-order array.  A scalar axis calibration
        # applies to every slice along that axis, so both x positions are scaled by 10.
        np.testing.assert_allclose(table.values, data * 10.0)

    def test_explicit_declaration_overrides_header_values(self):
        data = np.array([0, 1, 65535], dtype=np.uint16)
        header = {'missing codes': '65535', 'scale': '0.5'}
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, header, index_order='F')

            table = nrrd.read_measurement_table(
                filename, nrrd.SourceDeclaration(scale=2.0), index_order='C')

        np.testing.assert_allclose(table.values[[0, 1]], [0.0, 2.0])
        self.assertEqual(table.declaration.missing_codes, (65535,))

    def test_merge_header_false_uses_only_explicit_declaration(self):
        data = np.array([0, 1, 65535], dtype=np.uint16)
        header = {'missing codes': '65535'}
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, header, index_order='F')

            table = nrrd.read_measurement_table(
                filename, nrrd.SourceDeclaration(), index_order='C', merge_header=False)

        np.testing.assert_equal(table.status, ['valid', 'valid', 'valid'])

    def test_read_measurement_table_translates_c_order_axis_labels(self):
        data = np.arange(2 * 3 * 4, dtype=np.int16).reshape(4, 3, 2)
        header = {
            'labels': ['x', 'y', 'z'],
            'axis x scale': '2',
            'axis y scale': '3',
            'axis z scale': '1 2 3 4',
        }
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, header, index_order='C')

            table = nrrd.read_measurement_table(filename, index_order='C')

        self.assertEqual(table.labels, ('z', 'y', 'x'))
        # The z-axis vector has one entry per z slice, and z is the first axis of the C-order array.
        expected = data * 2.0 * 3.0 * np.array([1.0, 2.0, 3.0, 4.0]).reshape(4, 1, 1)
        np.testing.assert_allclose(table.values, expected)

    def test_read_measurement_table_keeps_raw_codes(self):
        data = np.array([0, 1, 255], dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'instrument.nrrd')
            nrrd.write(filename, data, index_order='F')

            table = nrrd.read_measurement_table(filename, index_order='C')

        np.testing.assert_array_equal(table.raw_codes, data)
        self.assertEqual(table.raw_codes.dtype, np.uint8)


class TestHarmonization(unittest.TestCase):
    def test_tables_from_instruments_keep_their_own_sentinel_codes(self):
        first = nrrd.to_measurement_table(
            np.array([0, 1, 65535], dtype=np.uint16),
            nrrd.SourceDeclaration(name='scanner-a', missing_codes=[65535], scale=0.5, units='kPa'))
        second = nrrd.to_measurement_table(
            np.array([0, 2, 10], dtype=np.uint16),
            nrrd.SourceDeclaration(name='scanner-b', reserved_codes=[10], scale=1.0, units='kPa'))

        table = nrrd.MeasurementTable.concatenate([first, second], axis=0, source_labels=['a', 'b'])

        np.testing.assert_equal(table.status,
                                ['valid', 'valid', 'missing', 'valid', 'valid', 'reserved'])
        np.testing.assert_array_equal(table.raw_codes, [0, 1, 65535, 0, 2, 10])
        np.testing.assert_allclose(table.values[:2], [0.0, 0.5])
        self.assertEqual(table.metadata['source_labels'], ('a', 'b'))
        self.assertEqual([d.name for d in table.metadata['source_declarations']], ['scanner-a', 'scanner-b'])

    def test_concatenation_rejects_mismatched_units(self):
        first = nrrd.to_measurement_table(np.array([0], dtype=np.int16),
                                          nrrd.SourceDeclaration(units='kPa'))
        second = nrrd.to_measurement_table(np.array([0], dtype=np.int16),
                                           nrrd.SourceDeclaration(units='Pa'))
        with self.assertRaisesRegex(nrrd.MeasurementError, 'different units'):
            nrrd.MeasurementTable.concatenate([first, second])

    def test_harmonize_reads_files(self):
        with tempfile.TemporaryDirectory() as directory:
            filenames = []
            for index, data in enumerate([np.array([0, 1], dtype=np.int16),
                                          np.array([2, 3], dtype=np.int16)]):
                filename = os.path.join(directory, f'{index}.nrrd')
                nrrd.write(filename, data, index_order='F')
                filenames.append(filename)

            table = nrrd.harmonize_measurement_tables(
                filenames,
                [nrrd.SourceDeclaration(name='a'), nrrd.SourceDeclaration(name='b')],
                index_order='C')

        np.testing.assert_array_equal(table.raw_codes, [0, 1, 2, 3])
        np.testing.assert_allclose(table.values, [0.0, 1.0, 2.0, 3.0])

    def test_object_raw_codes_can_be_reconverted(self):
        data = np.array([2 ** 64 - 1, 1], dtype=object)
        table = nrrd.to_measurement_table(data, nrrd.SourceDeclaration(strict_precision=False))
        self.assertEqual(table.raw_codes.dtype, np.dtype(object))
        np.testing.assert_allclose(table.values, [float(2 ** 64 - 1), 1.0])

    def test_concatenation_keeps_exact_codes_when_raw_dtypes_differ(self):
        first = nrrd.to_measurement_table(
            np.array([0, 1, np.iinfo(np.uint64).max], dtype=np.uint64),
            nrrd.SourceDeclaration(strict_precision=False))
        second = nrrd.to_measurement_table(np.array([-5, 3], dtype=np.int64),
                                           nrrd.SourceDeclaration())
        table = nrrd.MeasurementTable.concatenate([first, second], axis=0)
        self.assertEqual(table.raw_codes.dtype, np.dtype(object))
        self.assertEqual(int(table.raw_codes[2]), int(np.iinfo(np.uint64).max))
        self.assertEqual(int(table.raw_codes[3]), -5)

    def test_concatenation_records_per_sample_source(self):
        first = nrrd.to_measurement_table(np.array([0, 1], dtype=np.int16),
                                          nrrd.SourceDeclaration(name='a'))
        second = nrrd.to_measurement_table(np.array([2, 3], dtype=np.int16),
                                           nrrd.SourceDeclaration(name='b'))
        table = nrrd.MeasurementTable.concatenate([first, second])
        self.assertEqual(table.sources, ('a', 'b'))
        np.testing.assert_array_equal(table.source_index, [0, 0, 1, 1])
        self.assertEqual([record['source'] for record in table.to_records()], ['a', 'a', 'b', 'b'])

        relabeled = nrrd.MeasurementTable.concatenate([first, second], source_labels=['first', 'second'])
        self.assertEqual(relabeled.sources, ('first', 'second'))
        self.assertEqual([record['source'] for record in relabeled.to_records()],
                         ['first', 'first', 'second', 'second'])

    def test_nested_concatenation_keeps_provenance(self):
        a = nrrd.to_measurement_table(np.array([0], dtype=np.int16), nrrd.SourceDeclaration(name='a'))
        b = nrrd.to_measurement_table(np.array([1], dtype=np.int16), nrrd.SourceDeclaration(name='b'))
        c = nrrd.to_measurement_table(np.array([2], dtype=np.int16), nrrd.SourceDeclaration(name='c'))
        first = nrrd.MeasurementTable.concatenate([a, b])
        table = nrrd.MeasurementTable.concatenate([first, c])
        self.assertEqual(table.sources, ('a', 'b', 'c'))
        self.assertEqual([record['source'] for record in table.to_records()], ['a', 'b', 'c'])

    def test_concatenation_keeps_source_metadata(self):
        first = nrrd.to_measurement_table(np.array([0], dtype=np.int16),
                                          nrrd.SourceDeclaration(name='a'), metadata={'site': 'north'})
        second = nrrd.to_measurement_table(np.array([1], dtype=np.int16),
                                           nrrd.SourceDeclaration(name='b'), metadata={'site': 'south'})
        table = nrrd.MeasurementTable.concatenate([first, second])
        self.assertEqual([item['site'] for item in table.metadata['source_metadata']], ['north', 'south'])

    def test_empty_concatenation_is_rejected(self):
        with self.assertRaisesRegex(nrrd.MeasurementError, 'At least one'):
            nrrd.MeasurementTable.concatenate([])


if __name__ == '__main__':
    unittest.main()
