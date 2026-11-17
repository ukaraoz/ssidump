"""ESS-DIVE reporting format specifications used by this package.

Column orders and the data-dictionary definitions that must be used verbatim are read from the
official templates vendored in essdive/templates/ (see its README). Controlled vocabularies are
transcribed from the formats' controlled_vocabulary.md files.

Sensor Time Series - Full v1.0.0:
  https://github.com/ess-dive-workspace/essdive-sensor-time-series-full/tree/release-v1.0.0
Location Metadata v2.0.0:
  https://github.com/ess-dive-workspace/essdive-location-metadata/tree/release-v2.0.0
"""
import csv
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / 'templates'
STS_DIR = TEMPLATES / 'sensor_time_series_full_v1.0.0'
LOC_DIR = TEMPLATES / 'location_metadata_v2.0.0'


def _header(path):
    with open(path, newline='', encoding='utf-8') as f:
        return next(csv.reader(f))


# ---------------------------------------------------------------- standards (FLMD `standard` values)

STANDARD_SENSOR_FULL = 'ESS-DIVE Sensor Time Series - Full Reporting Format v1'
STANDARD_LOCATION = 'ESS-DIVE Location v2'
LOCATION_KEYWORD = 'ESS-DIVE Location Metadata Reporting Format'   # for the dataset's Keywords

# ---------------------------------------------------------------- column orders (from the templates)

DATA_COLUMNS = _header(STS_DIR / 'template_sensor_full_data.csv')     # incl. {measurement_column_name}
ATTR_COLUMNS = _header(STS_DIR / 'template_sensor_attr.csv')
DD_COLUMNS = _header(STS_DIR / 'template_sensor_full_dd.csv')
FLMD_COLUMNS = _header(STS_DIR / 'template_sensor_full_flmd.csv')
LOCATION_COLUMNS = _header(LOC_DIR / 'template_locations.csv')

# Data dictionary rows the template defines; their `definition` must be used as-is.
with open(STS_DIR / 'template_sensor_full_dd.csv', newline='', encoding='utf-8') as _f:
    DD_TEMPLATE_ROWS = {r['column_or_row_name']: r for r in csv.DictReader(_f)}

FLAG_TEMPLATE_KEY = '{measurement_column_name}_flag'

# ---------------------------------------------------------------- required terms

ATTR_REQUIRED = ['attr_id', 'attr_type', 'attr_description']
DD_REQUIRED = ['column_or_row_name', 'unit', 'definition', 'measured_variable', 'material_measured',
               'method_id', 'location_id']
FLMD_REQUIRED = ['file_name', 'file_description', 'standard', 'data_dictionary_file_name']
LOCATION_REQUIRED = ['location_id', 'location_description', 'location_shape', 'location_confidence']

# ---------------------------------------------------------------- controlled vocabularies

ATTR_TYPES = {'method_id', 'flag_id', 'treatment_id', 'sensor_id'}

# unit CV: label -> UCUM code. The `unit` column takes the label.
UNITS = {
    'unitless': 'unitless', 'degree Celsius': 'Cel', 'microsiemens per centimeter': 'uS/cm',
    'milligrams per liter': 'mg/L', 'percent saturation': '%{saturation}', 'pH': '[pH]',
    'moles per kilogram': 'mol/kg', 'moles per liter': 'mol/L', 'percent': '%', 'per mille': '/10*2',
    'count': '{count}', 'milliliter': 'mL', 'liter': 'L', 'millimeter': 'mm', 'centimeter': 'cm',
    'meter': 'm', 'kilometer': 'km', 'milligram': 'mg', 'gram': 'g', 'kilogram': 'kg',
    'feet (international)': '[ft_i]',
}

MATERIALS = {'air', 'gas', 'liquid_aqueous', 'liquid_organic', 'ice', 'snow', 'soil', 'sediment', 'mineral',
             'rock', 'regolith', 'particulate', 'tissue', 'organism', 'vegetation', 'habitat', 'equipment',
             'other', 'not_applicable', 'unknown'}

DATA_TYPES = {'text', 'numeric', 'date', 'datetime'}

REPRESENTATION_TEMPORAL = {'year', 'month', 'day', '2-hour', 'hour', '30-minute', '15-minute', '5-minute',
                           'minute', 'second', 'hertz', 'other'}

STATISTICS = {'mean', 'minimum', 'median', 'maximum', 'total', 'standard deviation', 'standard error',
              'measurement uncertainty', 'R2', 'RMSE', 'p-value', 'CV', 'covariance'}

# measured_variable uses the CUAHSI / ODM2 variable-name vocabulary (http://vocabulary.odm2.org/variablename/),
# which is too large to embed; the validator only checks that the field is filled.

LOCATION_SHAPES = {'point', 'curve', 'surface'}
LOCATION_CONFIDENCE = {'high', 'low', 'unknown'}
ELEVATION_DATUMS = {'EGM96', 'MSL', 'NAVD88', 'NGVD29', 'WGS84', 'unknown', 'other'}
VERTICAL_POSITION_REFERENCES = {'ground_surface', 'well_casing_top', 'water_bed', 'water_surface',
                                'mean_sea_level', 'unknown', 'other'}

# ---------------------------------------------------------------- CSV conventions (CSV Reporting Format)

MISSING_NUMERIC = '-9999'
MISSING_TEXT = 'N/A'
NOT_APPLICABLE = 'N/A'

# FLMD values the Sensor Time Series - Full format asks for when these optional terms are used.
FLMD_DATA_ORIENTATION = 'horizontal'
FLMD_HEADER_ROWS = '1'
FLMD_NAME_POSITION = '1'
