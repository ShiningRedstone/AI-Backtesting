"""EdgeLab web application (Phase 3.5): a thin HTTP layer over ``edgelab.services``.

The browser only ever sends JSON data (strategy definitions, variation specs, ids). All
validation, canonicalization, compilation, lineage and backtesting stay in Python.
"""
