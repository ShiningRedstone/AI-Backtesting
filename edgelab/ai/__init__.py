"""AI-assisted strategy discovery (Phase 9), on top of the Mode B proposal path.

    DiscoveryRequest -> blind context (no results) -> AIProvider.generate_proposals()
        -> versioned proposal envelopes -> strict gate (schema, DSL, features, causality,
           parameter domains, request constraints, canonical compile, identity)
        -> human review (accept / reject) -> save as an ordinary library strategy with provenance
        -> the EXISTING research tools (backtest, variations, compare, OOS, walk-forward, controls, prop)

The AI proposes hypotheses as DSL data. It never sees results, never ranks, never declares a
strategy good, and nothing here loops results back into generation.
"""
