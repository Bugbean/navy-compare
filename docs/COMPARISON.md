# The call screen and how "better" is decided

The call screen (`/call`) is where a rep compares Navy Health's policies with a competitor's while the
member is on the phone. This page is for anyone reviewing what it says: Navy Health's sales lead, legal,
or a developer changing it.

## What the rep does

The screen is one bar. The rep types what the member says, and the screen shows what it understood
as a sentence of chips, with the answer below it.

1. **Types the competitor's policy the way the member says it** ("hcf bronze plus 750", "defence gold",
   "frank"). The company is enough to start. Suggestions appear as each key is pressed, with the fund,
   brand, tier and type on each line so look-alike names can be told apart.
2. **Answers one question if the name means several policies.** The screen asks the single question
   that best splits them ("Which excess?", "Corporate cover or standard?", "Which kind of cover?"),
   worked out from the data. The rep clicks an answer or presses its number.
3. **Sees the answer.** Navy Health is always the base. A competitor on its own is compared with its
   closest Navy Health policy, marked "best match", and the rep can switch it from the chip. The card
   leads with a one-line verdict, then the direct answer to anything asked, the premiums, a talking
   point, where each side is better, and one-tap follow-ups (a cheaper Navy Health option, a member
   need the card has something to say about, another competitor, the full table).
4. **Keeps talking in the same bar**: "she's worried about her knees", "couple in vic", "add bupa
   silver plus", "anything cheaper?". Each changes the comparison on screen rather than starting again.
   Esc twice starts a new call.

The talking point is written from the card's facts only and streamed as it is written. While it is
streaming it is shown greyed as a draft. It is then replaced by the version the verifier checked, or by
the plain facts if the check failed (`engine/script.py`, `ai/compose.py`). A rep is never shown an
unchecked sentence as if it were checked.

## How typing is understood

Most of what a rep types is recognised in the browser in a few milliseconds (`app/static/call.js`).
The page downloads one compressed index when it opens (`/call/index.json`). The index holds every live
policy and fund, and the words for states, households, member needs and benefits. Each word typed gets
a role:

- **A company**, by code, name, brand or the words reps use ("defence", "teachers", "qantas"). These
  come from `config/aliases.yaml`.
- **A state or household**, by code, name or capital ("vic", "melbourne"), or "single mum", "couple".
- **A member need or a benefit** ("knees", "teeth", "ivf", "physio"). A need turns on its chip and brings
  its benefits to the top of the card as a direct answer.
- **Part of a policy name.** Each word must start a word of a policy's name or its company's, and a typo
  of one letter is forgiven in longer words.
- **Filler** ("she's", "about", "the"), which is ignored.

Some everyday words are also common in policy names ("family", "excess", "premium", "teeth"). They count
towards a policy name when they fit one, and otherwise mean what they usually mean. So "our premium
gold" finds Premium Gold Hospital rather than asking about price.

Words the screen could not place are shown as "Not used". Only those words are sent to the model
(`engine/plan.py`, prompt `ai/prompts/planner_v3.md`), along with the comparison on screen, so a
follow-up is read in context. The model never names a figure and never picks a policy for the rep. It
cannot override a chip the rep has confirmed. A name it finds that means several policies becomes a
question like any other.

Figures and policies are cached on the server for a few minutes per snapshot, so a repeated card comes
back in milliseconds. The index is rebuilt only when a new snapshot goes live.

## Changing the words it understands

- `config/aliases.yaml` holds the words reps use for each company, keyed by fund code.
- `config/language.py` holds the words for states, households, member needs and filler.
- `config/features.py` holds the everyday words for each benefit.

`tests/test_language.py` fails if any word maps to a state, household, chip or benefit the system
doesn't have.

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
- `tests/test_catalog.py` holds the type-ahead and how words are set aside.
- `tests/test_language.py` holds the vocabulary checks.
- `tests/test_suggest.py` holds the verdict line, the follow-ups and the closest Navy Health policies.
- `tests/test_call.py` holds the request reader, the index download, follow-ups read in context, the
  routes' checks, and the whole path on a loaded snapshot.

A browser-level session was run against staging with a headless DOM. It typed, answered questions,
added needs, households and competitors, removed a policy, and waited for the talking point. It is not
in the repository because it needs a running server.
