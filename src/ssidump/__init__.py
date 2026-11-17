"""Tools for the SSI odmx_lbnlsfa PostgreSQL dump: run a local server, export and select
datastreams, and build ESS-DIVE reporting-format packages.

Commands (after `pip install -e .`): ssidump, ssiexport, ssiexportall, ssiselect, ssi2essdive.
Without installing: PYTHONPATH=src python3 -m ssidump.<module> ...
"""
__version__ = '1.0.0'
