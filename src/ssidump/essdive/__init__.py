"""Tools for writing ESS-DIVE reporting-format packages (Python 3 standard library only).

    essdive.spec        reporting-format rules: column orders, verbatim definitions, vocabularies
    essdive.model       source-independent description of a package (locations, attributes, data files)
    essdive.vocab       mappings from SSI / ODMX terms to the controlled vocabularies
    essdive.writers     write a Package as CSV files (data, data dictionaries, attributes, locations, FLMD)
    essdive.validate    check a written package against the formats (python3 -m ssidump.essdive.validate DIR)
    essdive.ssi_source  build a Package from SSI datastreams (catalog rows from ssiselect)

Implemented: ESS-DIVE Sensor Time Series - Full v1.0.0 with Location Metadata v2.0.0.
"""
__version__ = '0.1.0'
