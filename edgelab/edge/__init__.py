"""Edge lab (ADR-104): does a signal carry information BEFORE a strategy is built on it?

* ``check``: a frozen set of hypotheses about NQ in the first 90 minutes after the New York open (9:30-11:00), measured on
  the discovery period only, with strict statistics (day-level shuffle test, Bonferroni over the whole set, costs,
  per-year consistency, ES cross-check).
* ``anatomy``: what the trades of an existing My strategy report really did (gross R before costs, costs, how far trades
  went for / against before they ended).

Nothing here runs a strategy, records a run or a trial, or reads the holdout.
"""
