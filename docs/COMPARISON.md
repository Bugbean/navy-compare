# The call screen and how "better" is decided

The call screen (`/call`) is where a rep compares Navy Health's policies with a competitor's while the
member is on the phone. This page is for anyone reviewing what it says: Navy Health's sales lead, legal,
or a developer changing it.

## What the rep does

1. **Says what they want, in their own words** ("our Premium Gold 350 against HCF bronze for dental and
   joint replacements"). One model call reads the request into policy words, benefits, and the state and
   household if given (`engine/plan.py`, prompt `ai/prompts/planner_v2.md`). The model never names a
   figure and never picks a policy. The policies it could mean come back as buttons. A word that names
   exactly one policy is ticked for the rep, and everything else is a choice.
2. **Or types in the pickers.** As the rep types, companies are suggested first ("HCF", "Frank" for
   GMHBA's brand) and then policies, with the fund, brand, tier and type on each line so look-alike names
   can be told apart. Picking a company keeps the list to its policies. Every word typed must start a
   word of the policy, its fund or its brand (`engine/catalog.py`).
3. **Chooses benefits** by everyday words ("crown", "physio", "hips", "glasses") or a quick group
   (`config/features.py`), or leaves them empty to compare everything.
4. **Turns on member needs** (joints or back, pregnancy, dental and so on). These only change the order:
   matching benefits move to the top.
5. **Compares.** Navy Health's policies always come first. Each competitor gets a card against each
   chosen Navy Health policy, and a side-by-side table marks the best value on each benefit. A competitor
   chosen on its own shows the closest Navy Health policies as buttons to add.
6. **Writes a talking point** from a card if they want one: two to four sentences from that card's
   facts, checked by the verifier before they are shown (`engine/script.py`, `ai/compose.py`).

## Where the figures come from

Every figure is read from the live snapshot of the government data through the same resolvers the rest
of the system uses, so the state and household rules are applied in one place (`engine/benefits.py`,
one query for all the chosen policies). Premiums are before the government rebate. They are compared at
the excess closest to the first Navy Health policy's excess. The card says which excess each premium is
at, and the rep can switch it.

## How "better" is decided

`engine/differences.py` compares the two values of each benefit:

| Benefit | Better is |
| --- | --- |
| Premium | lower |
| Hospital tier | higher (Basic < Bronze < Silver < Gold, a Plus tier half a step up) |
| Clinical category | covered > restricted > not covered |
| Waiting period | shorter |
| Co-payment | none, then lower |
| Extras service | covered |
| Extras benefit for an item | more paid, in dollars against dollars or percent against percent |
| Extras annual limit | higher, and no limit is best; per person is only compared with per person |
| Ambulance | emergency and non-emergency > emergency only > none |
| Known gap cover | yes |

Some benefits are never called better or worse:

- **The excess is a trade-off.** A lower excess comes with a higher premium. The card lists it under
  "Different, not better or worse".
- **Values that can't be ranked are only shown**: a limit per person against one per policy, two kinds of
  accommodation, a state ambulance scheme, or a free-text limit.
- **A waiting period for something one policy doesn't cover is left out**, because the clinical category
  row already says it.

## How much a difference matters, and how to change it

`config/materiality.yaml` scores each difference from 0 to 3. A $40 premium gap scores 3. A $1 gap is
noise and is not shown as a difference. Covered against not covered scores 3. The card is ordered by this
score, then by the member's chips, then by the type order at the bottom of the file.

Navy Health may edit this file: the thresholds, the chips, and which benefits each chip is about. The
app checks it when it starts. A tag naming a benefit that doesn't exist, or a missing rule, stops the
app with a message, rather than quietly ranking the card wrongly. Editing it changes only the order and
which small differences count as noise. It never changes a figure.

## Tests

- `tests/test_differences.py` holds each "better" rule and the materiality bands.
- `tests/test_catalog.py` holds the type-ahead and the benefit words.
- `tests/test_call.py` holds the request reader, the routes' checks, and the whole path on a loaded snapshot.

A browser-level session (typing in the pickers, choosing, comparing, the talking point) was run against
staging with a headless DOM. It is not in the repository because it needs a running server.
