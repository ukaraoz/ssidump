"""Tests for the essdive package (no database needed).

    python3 -m unittest discover -s tests -v
"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from essdive import spec, ssi_source, validate, writers   # noqa: E402


def catalog_row(site, logger, sensor_id, name, z, table, ds_id):
    """Shape of an ssiselect catalog row (only the keys the adapter uses)."""
    return {
        'datastream_id': str(ds_id), 'datastream_table': table, 'datastream_type': 'physicalsensor',
        'variable': 'soilTemperature', 'variable_name': 'Soil Temperature', 'units': 'degC; Cel',
        'site_code': site, 'site_name': 'Test transect', 'latitude': '38.9', 'longitude': '-106.9',
        'site_elevation_m': '3100.5', 'logger_equipment_id': str(logger), 'logger_name': 'unknown logger',
        'sensor_equipment_id': str(sensor_id), 'sensor_name': name, 'sensor_model': 'LBNL TMP 117 Temperature Probe',
        'sensor_manufacturer': 'Lawrence Berkeley National Laboratory', 'sensor_z_offset_m': str(z),
        'sensor_installed_utc': '2022-10-03 01:00:00', 'sensor_removed_utc': None,
        'feeder_table': f'feeder.influxdb_SNOD1_Node{logger}', 'feeder_column': name,
    }


ROWS = [catalog_row('SG-TEST1', 26, 101, 'Temp01', 0.05, 't01', 1),
        catalog_row('SG-TEST1', 26, 102, 'Temp02', -0.25, 't02', 2),
        catalog_row('SG-TEST2', 28, 201, 'Temp01', -0.1, 't21', 3)]
T0 = 1664758800   # 2022-10-03T01:00:00Z
DATA = {
    't01': [(T0, 2.28125, 'z'), (T0 + 900, None, 'c'), (T0 + 1800, 127.99, 'f')],
    't02': [(T0, 8.375, 'z'), (T0 + 1800, 8.25, 'z')],            # no row at T0+900
    't21': [(T0, 1.0, 'z')],
}
CODES = {'z': {'term': 'good', 'definition': 'Data passed all quality assessment tests.'},
         'c': {'term': 'NaN', 'definition': 'Data reported as not a number.'},
         'f': {'term': 'outOfRange', 'definition': 'Data value is outside a valid range.'}}
SITES = {'SG-TEST1': {'sampling_feature_name': 'Test transect A', 'sampling_feature_description': 'A test site.',
                      'latitude': '38.9', 'longitude': '-106.9', 'elevation_m': '3100.5', 'elevation_datum_cv': 'NAVD88'}}


def fetch(table, start=None, end=None):
    return iter(DATA[table])


def read(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


class PackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / 'pkg'
        cls.pkg = ssi_source.build_package(ROWS, prefix='test_soil', title='Test soil temperature', fetch=fetch,
                                           quality_codes=CODES, sites=SITES,
                                           config={'methods': {'method_soil_temperature_lbnl_tmp_117_temperature_probe':
                                                               {'description': 'Custom method text.', 'lower_bound': -40}}})
        cls.res = writers.write_package(cls.pkg, cls.out, log=lambda m: None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_validates_without_errors(self):
        rep = validate.validate_package(self.out)
        self.assertEqual(rep.errors, [], rep.summary())

    def test_files(self):
        names = {p.name for p in self.out.iterdir()}
        self.assertEqual(names, {
            'test_soil_SG-TEST1.csv', 'test_soil_SG-TEST1_dd.csv', 'test_soil_SG-TEST2.csv',
            'test_soil_SG-TEST2_dd.csv', 'test_soil_sensor_attr.csv', 'test_soil_sensor_attr_dd.csv',
            'test_soil_locations.csv', 'test_soil_flmd.csv'})

    def test_data_file(self):
        rows = read(self.out / 'test_soil_SG-TEST1.csv')
        self.assertEqual(list(rows[0]), ['datetime_measured', 'soil_temperature_temp01', 'soil_temperature_temp01_flag',
                                         'soil_temperature_temp02', 'soil_temperature_temp02_flag'])
        self.assertEqual([r['datetime_measured'] for r in rows],
                         ['2022-10-03T01:00:00Z', '2022-10-03T01:15:00Z', '2022-10-03T01:30:00Z'])
        self.assertEqual(rows[0]['soil_temperature_temp01'], '2.28125')
        self.assertEqual(rows[1]['soil_temperature_temp01'], '-9999')          # NULL -> missing code
        self.assertEqual(rows[1]['soil_temperature_temp01_flag'], 'qa_c')
        self.assertEqual(rows[1]['soil_temperature_temp02_flag'], ssi_source.NO_RECORD)   # no row at that time
        self.assertEqual(rows[2]['soil_temperature_temp01_flag'], 'qa_f')
        self.assertEqual(self.res['rows']['test_soil_SG-TEST1.csv'], 3)

    def test_data_dictionary(self):
        dd = {r['column_or_row_name']: r for r in read(self.out / 'test_soil_SG-TEST1_dd.csv')}
        self.assertEqual(dd['datetime_measured']['definition'], spec.DD_TEMPLATE_ROWS['datetime_measured']['definition'])
        self.assertEqual(dd['soil_temperature_temp01_flag']['definition'],
                         spec.DD_TEMPLATE_ROWS[spec.FLAG_TEMPLATE_KEY]['definition'])
        c = dd['soil_temperature_temp02']
        self.assertEqual((c['unit'], c['measured_variable'], c['material_measured']), ('degree Celsius', 'temperature', 'soil'))
        self.assertEqual(c['location_id'], 'SG-TEST1_Temp02')
        self.assertEqual(c['sensor_id'], 'sensor_102')
        self.assertIn('above the ground surface', dd['soil_temperature_temp01']['notes'])

    def test_attributes_and_config(self):
        attrs = {r['attr_id']: r for r in read(self.out / 'test_soil_sensor_attr.csv')}
        m = attrs['method_soil_temperature_lbnl_tmp_117_temperature_probe']
        self.assertEqual((m['attr_type'], m['attr_description'], m['lower_bound']), ('method_id', 'Custom method text.', '-40'))
        self.assertEqual({k for k, v in attrs.items() if v['attr_type'] == 'flag_id'}, {'qa_c', 'qa_f', 'qa_z', 'no_record'})
        self.assertEqual({k for k, v in attrs.items() if v['attr_type'] == 'sensor_id'}, {'sensor_101', 'sensor_102', 'sensor_201'})

    def test_locations(self):
        locs = {r['location_id']: r for r in read(self.out / 'test_soil_locations.csv')}
        self.assertEqual(locs['SG-TEST1']['elevation_datum'], 'NAVD88')
        self.assertEqual(locs['SG-TEST1']['location_description'], 'A test site.')
        child = locs['SG-TEST1_Temp02']
        self.assertEqual((child['vertical_position'], child['vertical_position_reference'], child['parent_location_id']),
                         ('-0.25', 'ground_surface', 'SG-TEST1'))
        self.assertEqual(locs['SG-TEST2']['elevation_datum'], 'unknown')   # elevation without a datum

    def test_flmd(self):
        flmd = {r['file_name']: r for r in read(self.out / 'test_soil_flmd.csv')}
        self.assertEqual(flmd['test_soil_SG-TEST1.csv']['standard'], spec.STANDARD_SENSOR_FULL)
        self.assertEqual(flmd['test_soil_SG-TEST1.csv']['data_dictionary_file_name'], 'test_soil_SG-TEST1_dd.csv')
        self.assertEqual(flmd['test_soil_locations.csv']['standard'], spec.STANDARD_LOCATION)
        self.assertEqual(flmd['test_soil_sensor_attr.csv']['data_dictionary_file_name'], 'test_soil_sensor_attr_dd.csv')


class TableColumnNames(unittest.TestCase):
    def test_table_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'pkg'
            pkg = ssi_source.build_package(ROWS, prefix='t', title='T', fetch=fetch, quality_codes=CODES, sites=SITES,
                                           column_naming='table')
            writers.write_package(pkg, out, log=lambda m: None)
            self.assertEqual(list(read(out / 't_SG-TEST1.csv')[0]),
                             ['datetime_measured', 't01_data_value', 't01_data_value_flag', 't02_data_value', 't02_data_value_flag'])
            self.assertEqual(validate.validate_package(out).errors, [])

    def test_feeder_site_file_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'pkg'
            pkg = ssi_source.build_package(ROWS, prefix='t', title='T', fetch=fetch, quality_codes=CODES, sites=SITES,
                                           file_naming='feeder-site')
            writers.write_package(pkg, out, log=lambda m: None)
            names = {p.name for p in out.iterdir()}
            self.assertIn('influxdb_SNOD1_Node26_SG-TEST1.csv', names)
            self.assertIn('influxdb_SNOD1_Node26_SG-TEST1_dd.csv', names)
            self.assertEqual(validate.validate_package(out).errors, [])


    def test_split_variables(self):
        rows = ROWS[:2] + [dict(ROWS[0], datastream_id='9', datastream_table='tb', variable='batteryVoltage',
                                variable_name='Battery Voltage', units='V', sensor_equipment_id='100',
                                sensor_name='unknown logger', sensor_model='unknown sensor', sensor_z_offset_m='0')]
        data = dict(DATA, tb=[(T0, 3.4, 'z')])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'pkg'
            pkg = ssi_source.build_package(rows, prefix='t', title='T', fetch=lambda t, s=None, e=None: iter(data[t]),
                                           quality_codes=CODES, sites=SITES, file_naming='feeder-site',
                                           split_variables=True)
            writers.write_package(pkg, out, log=lambda m: None)
            names = {p.name for p in out.iterdir() if not p.name.startswith('t_')}
            self.assertEqual(names, {'influxdb_SNOD1_Node26_SG-TEST1_soil_temperature.csv',
                                     'influxdb_SNOD1_Node26_SG-TEST1_soil_temperature_dd.csv',
                                     'influxdb_SNOD1_Node26_SG-TEST1_battery_voltage.csv',
                                     'influxdb_SNOD1_Node26_SG-TEST1_battery_voltage_dd.csv'})
            self.assertEqual(list(read(out / 'influxdb_SNOD1_Node26_SG-TEST1_battery_voltage.csv')[0]),
                             ['datetime_measured', 'battery_voltage_unknown_logger', 'battery_voltage_unknown_logger_flag'])


class ProblemFlags(unittest.TestCase):
    def test_apply_problem_flags(self):
        from essdive import checks
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'pkg'
            pkg = ssi_source.build_package(ROWS, prefix='t', title='T', fetch=fetch, quality_codes=CODES, sites=SITES)
            writers.write_package(pkg, out, log=lambda m: None)
            before = read(out / 't_SG-TEST1.csv')
            used = checks.apply_problem_flags(pkg, out, {'suspect_zero_packet': {('t_SG-TEST1.csv', '2022-10-03T01:00:00Z',
                                                                                  'soil_temperature_temp01')}})
            after = read(out / 't_SG-TEST1.csv')
            self.assertEqual(used, {'suspect_zero_packet': 1})
            self.assertEqual(after[0]['soil_temperature_temp01_flag'], 'qa_z; suspect_zero_packet')
            self.assertEqual([r['soil_temperature_temp01'] for r in after], [r['soil_temperature_temp01'] for r in before])
            self.assertEqual(validate.validate_package(out).errors, [])


class ValidatorCatchesErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'pkg'
        pkg = ssi_source.build_package(ROWS, prefix='bad', title='Bad', fetch=fetch, quality_codes=CODES, sites=SITES)
        writers.write_package(pkg, self.out, log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def _edit(self, name, fn):
        p = self.out / name
        rows = read(p)
        header = list(rows[0])
        fn(rows)
        with open(p, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=header, lineterminator='\n')
            w.writeheader()
            w.writerows(rows)

    def test_material_undefined_flag_and_bad_time(self):
        self._edit('bad_SG-TEST1_dd.csv', lambda rs: rs[1].update(material_measured='dirt'))
        self._edit('bad_SG-TEST1.csv', lambda rs: (rs[0].update(soil_temperature_temp01_flag='zz'),
                                                   rs[1].update(datetime_measured='2022-10-03 01:15:00')))
        rep = validate.validate_package(self.out)
        text = '\n'.join(rep.errors)
        self.assertIn("material_measured 'dirt'", text)
        self.assertIn('flag zz is not defined', text)
        self.assertIn('not ISO 8601', text)

    def test_missing_location(self):
        self._edit('bad_locations.csv', lambda rs: rs.pop())
        rep = validate.validate_package(self.out)
        self.assertTrue(any('not in the locations file' in e for e in rep.errors), rep.summary())


if __name__ == '__main__':
    unittest.main()
