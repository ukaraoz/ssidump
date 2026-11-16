"""Mappings from SSI / ODMX database terms to ESS-DIVE controlled vocabularies.

Extend these tables as more data types are converted. Anything not mapped is passed through and
reported by essdive.validate as a warning, so gaps are visible rather than silently guessed.
"""
import re

# SSI cv_units abbreviation -> Sensor Time Series - Full `unit` CV label.
UNIT_LABELS = {
    'degC; Cel': 'degree Celsius', 'Cel': 'degree Celsius',
    'unitless': 'unitless', '%': 'percent',
    'uS/cm': 'microsiemens per centimeter', 'mg/L': 'milligrams per liter',
    'm': 'meter', 'cm': 'centimeter', 'mm': 'millimeter', 'km': 'kilometer',
    'm^3/m^3': 'unitless',   # volumetric ratio; the CV has no m3/m3 entry
    '-': 'unitless',         # SSI 'dimensionless' (phenocam indices and channel means)
}

# SSI variable_term -> (CUAHSI/ODM2 measured_variable, material_measured CV).
# 'temperature' is an ODM2 variable-name term; check the other measured_variable values against
# http://vocabulary.odm2.org/variablename/ before publishing a package that uses them.
VARIABLES = {
    'soilTemperature': ('temperature', 'soil'),
    'snowTemperature': ('temperature', 'snow'),
    'airTemperature': ('temperature', 'air'),
    'surfaceTemperature': ('temperature', 'other'),
    'waterTemperature': ('temperature', 'liquid_aqueous'),
    'soilVolumetricWaterContent': ('volumetricWaterContent', 'soil'),
    'soilElectricalConductivity': ('electricalConductivity', 'soil'),
    'soilWaterPotential': ('waterPotential', 'soil'),
    'snowThickness': ('snowDepth', 'snow'),
    'airRelativeHumidity': ('relativeHumidity', 'air'),
    'batteryVoltage': ('voltage', 'equipment'),
    # no CUAHSI VariableName term for radio signal strength; kept as the SSI term (reported in the manifest)
    'rssi': ('rssi', 'equipment'),
    # phenocam image metrics (SSI sampled medium 'plant'); no CUAHSI VariableName terms, kept as SSI terms
    'GCC': ('GCC', 'vegetation'),
    'redChannelMean': ('redChannelMean', 'vegetation'),
    'greenChannelMean': ('greenChannelMean', 'vegetation'),
    'blueChannelMean': ('blueChannelMean', 'vegetation'),
}

# ODMX cv_quality_code qa_flag -> flag id used in the package.
def flag_id(qa_flag):
    return f'qa_{qa_flag}'


def unit_label(ssi_unit):
    return UNIT_LABELS.get(ssi_unit, ssi_unit)


def variable(ssi_variable):
    return VARIABLES.get(ssi_variable, (ssi_variable, 'unknown'))


def snake(term):
    """soilTemperature -> soil_temperature; acronyms stay whole: GCC -> gcc"""
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', term).lower()


def slug(text):
    """Letters, numbers, hyphens and underscores only (CSV reporting format names)."""
    return re.sub(r'[^A-Za-z0-9_-]+', '_', text).strip('_')
