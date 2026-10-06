"""Market simulator (ADR-106): a deep, read-only analysis of NQ and ES on the DISCOVERY period (never the holdout) and
live-knowledge 15-minute forecasts scored against baselines. Nothing here is a backtest run, a try or a holdout look.

Modules: ``data`` (bars of every timeframe), ``news`` (economic calendar via the JBlanked API, timezone proven from
fixed-time events), ``patterns`` (ICT / SMC concepts and their outcomes), ``trend`` / ``nqes`` / ``shocks`` (statistics),
``edges`` (find on the first 70 %, confirm on the last 30 %), ``features`` / ``models`` / ``forecast`` (walk-forward
predictions), ``newdays`` (NQ + ES days after the data ends), ``analysis`` (the job that ties it together).
"""
