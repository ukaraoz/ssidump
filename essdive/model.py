"""Source-independent description of an ESS-DIVE Sensor Time Series - Full package.

A source adapter (e.g. essdive.ssi_source) fills these objects; essdive.writers turns them into
the CSV files and essdive.validate checks the result.

    Package
    ├── locations   [Location]           -> <prefix>_locations.csv        (Location Metadata v2)
    ├── attributes  [Attribute]          -> <prefix>_sensor_attr.csv     (methods, sensors, flags, treatments)
    └── data_files  [DataFile]           -> <name>.csv + <name>_dd.csv
            └── columns [MeasurementColumn]   one time series each, with an optional _flag column
"""
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, List, Optional, Tuple


@dataclass
class Location:
    """A row of the locations file (Location Metadata v2)."""
    location_id: str
    location_description: str
    location_shape: str = 'point'
    location_confidence: str = 'unknown'
    location_name: str = ''
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    elevation: Optional[float] = None
    elevation_datum: str = ''
    vertical_position: Optional[float] = None
    vertical_position_interval: str = ''
    vertical_position_reference: str = ''
    location_alias: str = ''
    parent_location_id: str = ''
    measurement_method: str = ''
    location_information_source: str = ''
    notes: str = ''

    def as_row(self):
        return {k: ('' if v is None else v) for k, v in self.__dict__.items()}


@dataclass
class Attribute:
    """A row of the methods and attributes file."""
    attr_id: str
    attr_type: str                     # method_id | sensor_id | flag_id | treatment_id
    attr_description: str
    analysis_detection_limit: str = ''
    analysis_precision: str = ''
    instrument_precision: str = ''
    lower_bound: str = ''
    upper_bound: str = ''
    sensor_model: str = ''
    sensor_serial_number: str = ''

    def as_row(self):
        return dict(self.__dict__)


@dataclass
class MeasurementColumn:
    """One measurement column of a data file and its data dictionary row."""
    name: str                          # {measurement_column_name}
    definition: str
    unit: str                          # unit CV label, e.g. 'degree Celsius'
    measured_variable: str             # CUAHSI / ODM2 variable name, e.g. 'temperature'
    material_measured: str             # material CV, e.g. 'soil'
    method_id: str
    location_id: str
    sensor_id: str = ''
    treatment_id: str = 'N/A'
    long_name: str = ''
    data_type: str = 'numeric'
    missing_value_code: str = '-9999'
    unit_basis: str = ''
    representation_temporal: str = ''
    statistic_temporal: str = ''
    statistic_temporal_number: str = ''
    notes: str = ''
    with_flag: bool = True             # write a {name}_flag column
    source: str = ''                   # source identifier for the adapter (e.g. datastream table); not written

    @property
    def flag_name(self):
        return f'{self.name}_flag'


# A data file's rows: (datetime_measured, {column name: (value or None, flag codes)}) in time order.
Row = Tuple[str, dict]


@dataclass
class DataFile:
    name: str                          # file stem, e.g. 'snod1_soil_temperature_SG-DTP26'
    description: str
    columns: List[MeasurementColumn]
    rows: Callable[[], Iterator[Row]]  # called once when the file is written
    notes: str = ''                    # FLMD notes

    @property
    def file_name(self):
        return f'{self.name}.csv'

    @property
    def dd_file_name(self):
        return f'{self.name}_dd.csv'


@dataclass
class Package:
    prefix: str                        # stem shared by the package-level files
    title: str
    locations: List[Location] = field(default_factory=list)
    attributes: List[Attribute] = field(default_factory=list)
    data_files: List[DataFile] = field(default_factory=list)
    flmd_notes: dict = field(default_factory=dict)      # file_name -> notes

    @property
    def attr_file_name(self):
        return f'{self.prefix}_sensor_attr.csv'

    @property
    def attr_dd_file_name(self):
        return f'{self.prefix}_sensor_attr_dd.csv'

    @property
    def locations_file_name(self):
        return f'{self.prefix}_locations.csv'

    @property
    def flmd_file_name(self):
        return f'{self.prefix}_flmd.csv'
