"""Realtime layer for the Hawkes cascade-onset alarm (CascadeSignal live pivot).

Wraps the existing offline algorithm core (`models/hawkes.py`,
`models/labels.py`) with live Ethereum mainnet ingestion, an incremental bar
clock, model persistence, and a read-only status/UI service.

Nothing here modifies the offline pipeline (`labels/`, `models/`, `eval/`,
`state/`, `graph/`) -- this package only adds new, additive code around it.
"""
