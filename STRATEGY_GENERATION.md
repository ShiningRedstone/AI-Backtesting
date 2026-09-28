# Strategy generation, families and lineage

This covers how strategy *instances* come to exist: written by you (or edited or duplicated),
generated as controlled variations (**Mode A**), or proposed by an AI (**Mode B**). Every path
ends in the same place: a DSL definition that passed the validator and the compiler, with a
lineage record. Nothing here evaluates performance. That is the numerical engine's job
(single runs now, batch research in Phase 4).

## 1. Families and instances

| Concept | What it is | Where |
|---|---|---|
| **Family** | a market hypothesis ("NY opening-range breakout"): `family: {id, name, hypothesis, category}` | carried by every instance; `StrategyFamily` |
| **Instance** | one concrete canonical definition; identity = `logic_hash`, `strategy_id = STR_<12 hex>` | `StrategyLibrary` |
| **Base strategy** | the hypothesis plus its declared parameter **domains** (what the hypothesis allows) | the DSL file |
| **Variation spec** | which declared parameters to explore, over which values, and how to combine them | separate YAML file |

The split between base strategy and variation spec is deliberate. The base strategy says what
may vary and within which bounds. The variation spec only chooses *inside* those bounds, so it
cannot quietly widen a hypothesis into a data-mining exercise.

Families are counted by `id`. Instances with identical logic are the same instance, whatever
their names.

## 2. Mode A: controlled variations

```yaml
# strategies/fixtures/orb_variations.yaml
variation_spec_version: 1
name: orb_structural_grid
mode: grid                    # grid | one_at_a_time | random_sample
max_variants: 100             # hard cap, checked BEFORE generation
include_base: false           # optional
dimensions:
  - {parameter: or_window, values: [OR_0930_0945, OR_0930_1000, OR_0930_1030], category: opening_range}
  - {parameter: rr, range: {min: 1.5, max: 3.0, step: 0.5}, category: target}
  - {parameter: use_volume_filter, values: [false, true], category: confirmation}
  - {parameter: min_rvol, values: [1.2, 1.5], category: confirmation}
```

| Mode | Combinations |
|---|---|
| `grid` | cartesian product of all dimensions |
| `one_at_a_time` | each dimension varied alone, everything else at the base value (sensitivity) |
| `random_sample` | `sample_size` combinations of the grid, drawn with a **required** integer `seed` (mixed-radix decoding, so the grid is never enumerated) |

**What can vary.** Anything the base strategy parameterizes, which covers:
- feature parameters and thresholds;
- stops, targets, time and hold limits;
- the opening-range or session windows (`choice` parameters);
- the higher timeframe (`timeframe` parameters);
- optional confirmations and filters (`enabled: $flag`).

The strategy timeframe itself can be a parameter too. Its children then need datasets of that timeframe.

**Rules, all enforced:**
- A dimension must name a parameter the base strategy **declares**. Every value must pass the
  parameter's declared domain (type, `min`/`max`, grid step, `choices`).
- `values` and `range` are mutually exclusive. A range needs `step > 0` that divides `max − min`.
  Values must be unique, and a parameter appears in at most one dimension.
- The combination count must be `<= max_variants`. Otherwise generation **fails** with the
  count; it is never truncated silently. The absolute ceiling is 10,000,000 grid points.
- Every child is validated **and compiled**. If any child is invalid (for example, a combination
  that enables a contradictory filter), the whole batch is rejected, with the failing
  combinations listed.
- Children are de-duplicated by `logic_hash`. For example, varying `min_rvol` while
  `use_volume_filter` is off yields identical logic. Duplicates and combinations identical to the base are
  **reported**, not silently dropped: for the example above, 48 combinations give 35 variants,
  11 duplicates and 2 identical to the base.

**Reproducibility.**
- The batch id is `VB_` + a hash of (base logic, base definition, canonical spec, generator version).
- The batch record stores the canonical base definition, the canonical spec and its hash, the DSL,
  compiler and generator versions, the config hash, the counts, the duplicates and the ordered child
  ids.
- Regenerating from the record reproduces the same children (tested). Timestamps are recorded
  but are not part of any id.

## 3. Lineage

Each instance carries one or more `LineageRecord`s:

| Field | Meaning |
|---|---|
| `strategy_id`, `logic_hash`, `definition_hash`, `family_id` | identity |
| `generation_method` | `user`, `manual_edit`, `duplicate`, `mode_a_variation`, `mode_b_proposal` |
| `parent_strategy_id` | the base (Mode A) or the edited strategy (manual edit) |
| `changes` | `[{parameter, old, new, category}]`: exactly what differs from the parent |
| `generation_parameters` | spec name, mode, full override set (Mode A); source and proposal index (Mode B) |
| `generation_batch_id`, `generation_timestamp` | batch and time |
| `versions` | DSL, compiler, generator, feature spec ids, config hash |

When the same logic is reached from two parents, both records are kept.

`duplicate_strategy` returns an unsaved **draft**: an unchanged copy has the same logic and so
is the same instance. Saving an edited draft through `edit_strategy` creates a new instance with a
`manual_edit` record that points at its parent.

## 4. Strategy library

The library is a plain directory, `data/strategy_library/` (git-ignored, easy to inspect, diff and back up):

```
instances/<STRATEGY_ID>.json   {strategy_id, logic_hash, definition_hash, name, family_id,
                                definition (canonical), lineage: [records]}
batches/<BATCH_ID>.json        Mode A batch records
```

Writes are atomic (temp file plus rename). Saving an existing instance is idempotent. The API is
`save`, `load`, `list(family)`, `families()`, `children(id)`, `ancestry(id)`, `save_batch` and
`load_batch`.

Stored definitions pin feature versions and re-validate on load. A stored id (`STR_…`) is
accepted anywhere a definition is: validate, compile, backtest, variations.

## 5. Mode B: AI proposal interface

Only the **interface** exists. No LLM is called anywhere in the code base.

```
ProposalRequest(n_families, instructions).capability_menu(sessions)
    -> machine-readable menu: operators, bar fields, order/stop/target/sizing types, features
       (params, outputs, requirements), sessions, timeframes, the unsupported list, and the rules
StrategyProposer.propose(request, menu) -> proposal batch (data)     # protocol; StaticProposer for files/tests
ingest_proposals(batch) -> IngestReport                              # the gate
```

Proposal batch schema (`strategies/fixtures/proposals_example.yaml`):

```yaml
proposal_batch_version: 1
request: {n_families: 20, notes: "..."}
source: {kind: ai | human, model: "...", prompt_sha256: "...", notes: "..."}
proposals:
  - family: {id, name, hypothesis, category}
    rationale: "why this is testable / the market mechanism"
    strategy: <DSL document>          # family injected if absent; must match if present
    variations: <variation spec>      # optional; validated against the strategy, not generated
```

**The gate:**
- **Unknown keys are rejected** at the batch, source, proposal and family levels. The schema
  has no field for performance, so a field such as `expected_win_rate` is refused.
- **Claim language is rejected** in the family name and hypothesis, the rationale and the
  strategy description. Examples: *profitable*, *guaranteed*, *win rate*, *high probability*,
  *Sharpe*, *profit factor*, *proven*, *best strategy*, *strong edge*, "*N% win/return*".
  Neutral words such as "edge of the range" pass.
- Every strategy goes through the **same** validator and compiler. Errors come back with DSL paths
  (unknown feature with suggestions, unsupported trailing stop, ...).
- Identical logic is rejected as not a distinct family. The same *structure* with different numbers
  is accepted with a warning that it is a variation, not a new family.
- A shortfall between families requested and families accepted is reported.
- Accepted instances get a `mode_b_proposal` lineage record with the source and batch id
  (`PB_` + hash of the batch).

The example batch results in 3 accepted and 5 rejected:
- rejected for claim language, a performance field, an unknown feature, a trailing stop, and a duplicate;
- one accepted proposal is flagged as a variation of another.

## 6. Commands and service calls

```bash
python -m edgelab.cli strategy variations strategies/fixtures/opening_range_breakout.yaml \
                                          strategies/fixtures/orb_variations.yaml [--no-save]
python -m edgelab.cli strategy proposals  strategies/fixtures/proposals_example.yaml [--no-save]
python -m edgelab.cli strategy menu [--n-families 20]
python -m edgelab.cli strategy list [--family ny_opening_range_breakout]
python -m edgelab.cli strategy show STR_...
python -m edgelab.cli strategy lineage STR_...
```

Service layer (JSON contracts for the future UI): `generate_variations`, `ingest_proposals`,
`proposal_menu`, `save_strategy`, `edit_strategy`, `duplicate_strategy`, `load_strategy`,
`list_strategies`, `strategy_families`, `strategy_lineage`.

## 7. Not implemented (deliberately)

| Item | Where it belongs |
|---|---|
| Running variations or proposals in bulk, ranking, selection | Phase 4 research engine |
| Any AI model call | a later AI layer behind `StrategyProposer` |
| Variation over structure that is not parameterized (adding or removing arbitrary conditions) | parameterize it in the base strategy (`enabled: $flag`) |
| Judging whether proposed families are "genuinely different" beyond logic and structure hashes | human review |
