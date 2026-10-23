# Data model: the government feed, and what it actually contains

The development plan's original schema sketch was written before any PHOL
file had been opened. This document records what a full structural scan of
the 1 Oct 2026 extract (`privatehealth-01-oct-2026.zip`, all 7 files) actually
contains, the corrections that scan forced, and the schema that follows from
it — `migrations/versions/0002_market_data.py`. `config/reference.py` holds
the government-fixed enums this schema depends on.

Method: streamed every file with `lxml.etree.iterparse` (never loaded whole —
the two Combined files are 528 MB and 658 MB), counted every element and
attribute value across the full file (not a sample), and cross-checked every
finding against `PHOLSchema-V3.1.xsd` and `PHOL-Lists-V3.1.xsd`'s own
enumerations and doc comments. Navy Health's own 280 Hospital, 280 Extras and
875 Combined rows were used to ground every structural claim in a real,
checkable example.

## The identity key is the opposite of what the plan assumed

The plan's resolver was designed around `ProductID` as the stable cross-month
key, with `ProductItemID` expected to change on reissue. The schema says the
reverse, in its own doc comments:

> **ProductItemID**: Assigned by the CMS for new products. **Common to all
> versions of a product.**
> **ProductID**: Assigned by the CMS for new products. **Unique to each
> version of a product.**

Confirmed operationally by `Product Changes 01-Oct-2026.csv`: a `Change` row
carries `OLD_ProductID` but never `OLD_ProductItemID`. A product being edited
keeps its `ProductItemID` and gets a new `ProductID`.

**Consequence:** the resolver (identity/, week 2) keys its exact-match tier on
`gov_product_item_id`, not `gov_product_id`. The plan's 4-tier cascade
(exact id → alias → fingerprint → unresolved) is otherwise sound — it just
had the wrong attribute as tier 1. Expect a materially higher exact-match rate
than the plan's conservative "about 97%" estimate, since this actually is the
government's stable key, not a field that merely tends to be stable.

Both IDs are unique **per row** within one file (confirmed: 31,864 distinct
`ProductID` and 31,864 distinct `ProductItemID` for 31,864 Hospital Open
rows) — i.e. per (fund, state, family composition, tier/excess), not per
marketed policy. Neither identifies "Prime Silver+ Hospital 750 across all
states and family types" as one thing; that grouping is `policies`, built by
the resolver from name + tier within a snapshot (see below), not read
directly off any one government field.

## `family_type` does not exist as a field — it is derived

There is no "Single / Couple / Family" element anywhere in the schema. Every
`<Product>` carries `WhoIsCovered`:

```xml
<WhoIsCovered OnlyOnePerson="false">
  <Coverage NumberOfAdults="2">
    <DependantCover Title="Child" Covered="true"/>
    <DependantCover Title="Student" Covered="true"/>
    ...
  </Coverage>
</WhoIsCovered>
```

`WhoIsCovered` has exactly one `Coverage` child (confirmed in the XSD:
`maxOccurs` is not set, so it defaults to 1) — each `<Product>` row is already
scoped to one specific household composition, not a menu of them.

A full scan of Hospital Open's 31,864 rows found 21 raw
`(OnlyOnePerson, NumberOfAdults, true-dependant-titles)` combinations, which
collapse to exactly 5 meaningful buckets:

| Raw shape | family_type | Rows (Hospital Open, all funds) |
| --- | --- | --- |
| `OnlyOnePerson=true` | `single` | 6,578 |
| `Adults=2`, no dependant covered | `couple` | 5,040 |
| `Adults=1`, ≥1 dependant covered | `single_parent` | 4,693 + 3,052 + ... |
| `Adults=2`, ≥1 dependant covered | `family` | 6,039 + 3,971 + ... |
| `Adults=0`, ≥1 dependant covered | `dependants_only` | 71 + 56 = 127 |

The 5th bucket — child-only cover, no adult on the policy — is real and the
plan's 4-value enum missed it. It is rare (127 of 31,864 rows, 0.4%) but
appears for multiple funds, not as a single fund's quirk.

Navy Health's own catalogue confirms this exactly: 8 hospital policy names ×
7 states × 5 family types = **280**, which is precisely Navy Health's row
count in Hospital Open. Same shape in Combined Open (25 hospital×extras
pairings × 7 states × 5 = 875).

`config/reference.py`'s `derive_family_type()` implements this derivation and
is the single place it happens; `tests/test_reference.py` pins every real
shape found.

### `family_type` alone is not enough: dependant scope is a price dimension

Across the whole Hospital Open file, 15,697 of 31,864 rows share their
(fund, name, state, family_type, excess) with at least one other row. In 98%
of those sets the rows differ in **which dependant categories the rate
covers**, and the premium differs with it:

```
NTF "Top Hospital 500 (Gold)", NSW, family
   Child + NonClassified + Student               $504.42
   Child + NonClassified + NonStudent + Student  $655.75
```

That is the same product, same household size, 30% apart, depending on
whether adult non-student dependants (to 31, under the 2021 age-of-dependants
rules) are on the policy. Collapsing both into `family` would quote the wrong
one. So the rate carries the exact covered set as a canonical key
(`variants.dependant_scope`, "Child+NonClassified+Student") alongside the
coarse `family_type`, with `covers_adult_dependants` as the axis a rep can
ask a member about. The rep UI has to capture it for family quotes.

### Brand is a policy dimension

The remaining rows in those duplicate sets were identical in price and
cover but carried different `FundsProductCode` prefixes and `Brands` codes.
A fund can sell under up to 5 consumer-facing names (`Fund.RelatedBrandNames`):
Teachers Health, UniHealth and Nurses & Midwives Health are one fund (NTF);
"Frank Health Insurance" is GMHBA's brand GMH02; "Hunter Health Insurance"
is CDH01. 9 brands exist in the feed. A member says the brand, so `policies`
carries `brand_code`, the `brands` table maps it to a name, and brand names
are policy aliases for the type-ahead.

### The rate key, proven complete

With brand, dependant scope and the full excess and co-payment structures
in the key, every one of the 31,864 open hospital rows is identified by
exactly one `(fund, brand, name, state, family_type, dependant_scope,
excess structure, co-payment structure)` — 0 collisions. (Co-payment was the
last dimension found: one AHM product, "elite gold copay", had two groups at
different premiums under the same name that differed only there.) Extras
leave 8 true duplicates: identical in every field including price, with
different `ProductItemID`s — LHS published the same product twice from
2026-05-15. Those stay as two rows, because they are two government records.

## Dependant age bands are a fund-level fact, not a product-level one

`Fund.FundDependants.DependantLimits` sets min/max age per dependant category,
once per fund, applying to every one of that fund's products:

```xml
<!-- Navy Health -->
<DependantLimit Title="Student" Supported="true" MinAge="22" MaxAge="31"/>
```

Navy Health covers student dependants to 31; the generic example in the
schema's own docs uses 18–24. This genuinely varies between funds and the
plan's schema had no field for it at all — it is the direct answer to "can my
dependant stay on this policy," which is exactly the kind of question a rep
fields. Stored as `fund_dependant_limits`, one row per (fund, category), so "which funds cover students past 25" is a query.

## Excess and co-payment are structures, not one number

`ExcessType` has 5 real values (`None`, `PerAdmissionOnly`, `LimitedPerYear`,
`LimitedPerPerson`, `LimitedPerPersonPerPolicy`), and what the dollar figure
*means* changes with the type — "$500 excess" is a different promise under
`LimitedPerPerson` than under `LimitedPerPersonPerPolicy`. 4 of the 5 types
occur in Hospital Open; `PerAdmissionOnly` is valid but wasn't used this
month, so it is still accepted, not rejected. Modelled as columns
(`excess_type`, `excess_per_admission`, `excess_per_person`,
`excess_per_policy`), not JSONB, because the comparison engine's `excess`
difference type reads them directly on the hot path.

Co-payments have the same shape problem (`CoPaymentType`: `None` / `Limited` /
`Unlimited`, each populating a different subset of `Shared` / `SharedMax` /
`Private` / `PrivateMax` / `DaySurgery` / `AnnualMax`).

## Hospital waiting periods are 4 fixed buckets, never per category

The plan assumed each of the 38 clinical categories carries its own wait. The
schema's own doc comment says otherwise:

> Three of the four are always required. PregnancyBirthNeonates is only
> required if the service is covered.

Confirmed exhaustively: across all 31,864 Hospital Open rows, exactly 4
`WaitingPeriod` titles ever appear — `SubAcute`, `PreExisting`,
`PregnancyBirth`, `Other` — and `PregnancyBirth` is present in only 11,110 of
them (the rest have `PregnancyBirth` set to `NotCovered` in `MedicalServices`,
so no wait applies). The 38 clinical categories carry a `Cover` level
(`Covered` / `Restricted` / `NotCovered`) each, but share these same 4 waits
between them — there is no 39th or per-category wait field anywhere in the
schema.

## Extras benefit limits are grouped across services, and some are by count

`BenefitLimit.ServicesCombined` lets several services share **one** cap. Real
example, Navy Health's own data:

```
DentalGeneral, DentalMajor, Endodontic  → one shared $1,700 annual limit
Physiotherapy, Chiropractic, ExercisePhysiology, Orthoptics,
  OccupationalTherapy, Osteopathy, SpeechTherapy  → one shared $950 annual limit
```

Across the whole GeneralHealth Open file, single-service limit groups
(71,836) are the majority but multi-service groups are common and large
(24,166 pairs, 9,023 triples, up to 23 services sharing one cap). A flat
per-service `annual_limit` column would state Navy Health's $1,700 dental cap
three times over — once per service — which reads as three times the real
limit. `extras_limit_groups` with `extras_limit_group_services` keeps the `services` list on
the group, not the service.

Two further shapes the plan's sketch didn't have a field for, both real and
non-trivial in volume:

- **Limits by count, not dollars** (`ServiceCountLimit`, types `Service` /
  `Appliance` — "2 hearing-aid appliances every 3 years"): 5,264 occurrences
  in GeneralHealth Open.
- **Free-text-only limits** (`FreeTextLimit`): when set, every numeric limit
  field on that group is to be ignored per the schema's own convention, and
  the group is unconditionally ranked "Low" for comparison. 1,481 occurrences.

Benefits are overwhelmingly dollar amounts in practice (420,388 of 420,388
sampled `Benefit` elements were dollars; the schema also allows a percentage,
never observed) and are usually state-independent even when the product's own
top-level `State` is `'ALL'` (218,368 of ~227,000 `BenefitsList` entries carry
`State="ALL"` internally too) — a minority of products use the documented
default-plus-override pattern to vary one service's benefit by state within a
single `'ALL'`-scoped row.

## Combined products are a third, independently published copy

Navy Health (and presumably most funds) publish three overlapping catalogues:
standalone Hospital, standalone Extras, and Combined — where a Combined
`<Product>` embeds **both** `HospitalCover` and `GeneralHealthCover` in one
self-contained row, not a reference to the separate standalone products.
Confirmed: scanning Combined Open's `<Product>` elements, 100% carry both
sub-blocks; 0% carry only one.

For Navy Health, a Combined row's premium is exactly the sum of its parts —
checked directly:

```
Core Silver+ Hospital, NSW, Single:        PremiumNoRebate = 192.37
Budget Extras, NSW, Single:                PremiumNoRebate =  35.30
Core Silver+ Hospital & Budget Extras:     PremiumNoRebate = 227.67   (= 192.37 + 35.30 exactly)
                                            PremiumHospitalComponent = 192.37
```

This is not relied on as a universal rule — no other fund was checked, and
nothing in the schema promises it. **All three files are ingested as the
government publishes them** (`variants.product_type` ∈ `hospital` / `extras` /
`combined`), rather than deriving Combined rows from the standalone ones.
Total row count across all 6 product files (Hospital + GeneralHealth + Combined,
Open and Closed) is ~132,000, landing almost exactly on the plan's own "about
130,000 variants" capacity estimate — ingesting the real triplicated data does
not blow the plan's sizing.

Navy Health's own catalogue isn't a clean 8×3 cross either: it publishes 24
regular pairings plus one extra ("Extras 65", bundled only with Saver
Bronze+), landing at 25 Combined products — funds choose which pairings to
package, not every hospital×extras combination.

## `HospitalTier` has 7 real values, with their own ranking

Beyond the 4 government reform tiers (Basic < Bronze < Silver < Gold), each
has a "Plus" variant — same regulatory floor, extra inclusions, not a tier
*between* the base and the next one up. All 7 occur in real data:

```
Bronze: 992    Gold: 7,154    SilverPlus: 13,083    BasicPlus: 3,292
BronzePlus: 5,173    Silver: 1,302    Basic: 868
```

The enumeration order in the XSD itself (`Gold, Silver, SilverPlus, Bronze,
...`) is not a ranking — `config/reference.py`'s `HOSPITAL_TIER_RANK` gives
the real order (Basic=1, BasicPlus=1.5, Bronze=2, BronzePlus=2.5, Silver=3,
SilverPlus=3.5, Gold=4) for the compare engine's `tier` difference type.

`Accommodation` has 6 real values, similarly finer-grained than a simple
private/public split (`PrivateOrPublic`, `PrivateSharedPublic`,
`PrivateSharedPublicShared`, `Public`, `PublicShared`, `PrivatePublicShared`).

## Fund-level complaints/retention/market-share data does not exist in this feed

`FundReport` (the plan's source for `complaints_per_10k`, `retention_pct`,
`market_share_pct`, `mgmt_expense_pct`) is schema-optional
(`minOccurs="0"`) and, checked directly, **occurs zero times across all 34
funds** in the 1 Oct 2026 extract:

```
$ grep -c "<FundReport" "Funds 01-Oct-2026.xml"
0
```

The columns are kept on `funds`, nullable, but need a separate source — APRA's
published "State of the Health Funds" statistics, loaded manually or by a
future job — before the pilot can show them. The `fund_*` difference type in
the plan's comparison engine should be treated as not-yet-available for the
pilot unless that source is wired up.

## Flat or side tables: what the feed's own variance decides

The feed is flat: one row per rate, each repeating the whole product
definition. The question for the schema was whether anything in that
definition actually varies between a product's rates. Tested directly —
every component fingerprinted per row, then grouped by (fund, name):

| Component | Constant within the product | Varies | By what |
| --- | --- | --- | --- |
| 38 clinical categories | **1,251 of 1,251** | 0 | — |
| 4 hospital waiting periods | 1,251 of 1,251 | 0 | — |
| Ambulance | 1,251 of 1,251 | 0 | — |
| Tier / accommodation / flags | 1,238 | 13 | state |
| Excess amounts | 666 | 585 | household (513), the per-policy cap doubling for two adults |
| 27 extras services covered | 507 of 508 | 1 | state |
| Extras benefit amounts | 473 | 35 | state (33) |
| Extras limit amounts | 157 | 351 | household (**351**), never state (0) |

Combined Open (1,139 products) and Hospital Closed (503) show the same
pattern. So the data has a two-level hierarchy the feed flattens: a
**product definition** (cover, waits, services, tier) that is constant
across every rate, and a **rate** (premium, excess amounts, limit amounts)
that varies by household. The first draft of this schema repeated the
definition on every rate row as JSONB — 35 copies of the 38 categories for
one Navy Health product. The data says to store it once.

**The rate card — `variants`.** One row per government row, holding only
what varies per row: premium, excess and co-payment structures, state,
household (`family_type` + `dependant_scope`), brand, the government ids,
and the raw row for backfill. Unique on `(snapshot_id, gov_product_item_id)`;
indexed on the card's lookup `(snapshot, policy, state, family_type,
dependant_scope, excess_option)`.

**The product definition — side tables keyed by policy.** `hospital_profiles`
(tier with its rank, accommodation, the 4 waits as columns, flags),
`hospital_cover` (38 rows per policy), `extras_profiles`, `extras_services`
(27 rows), `extras_benefits` (one per item), `extras_limit_groups` with
`extras_limit_group_services` (the shared caps). Written once per policy:
~48k `hospital_cover` rows per snapshot instead of 1.2M.

Each carries `state`, `'ALL'` in the common case and a specific state only
where the fund varies it; never mixed within a policy. Resolving a
`(policy, snapshot, state)` to the right rows is the one rule every query
must apply, so it is written once as the `*_for()` SQL functions in the
migration — a question-type SQL file calls `hospital_cover_for(...)`, it
never writes `state IN (...)` itself. Limit groups carry `family_type` and
`dependant_scope` instead, because that is the only thing they vary by.

What this buys the query layer over JSONB: "which policies cover joint
replacements", "cheapest Gold family policy in NSW", "every policy with a
dental cap over $1,000" are indexed lookups rather than scans of every row's
blob, and a multi-policy side-by-side is a join, not N blob parses.

### The comparison layer follows the same split

The plan precomputed one `comparisons` row per (pair, state, household) —
~450k rows, each carrying the full typed differences. With the definition
constant across rates, 90% of that work (the 38 categories, 27 services,
tier distance, the similarity score) is per policy *pair*, so
`policy_comparisons` computes it once per pair (~13k rows per snapshot). The
rate-level differences — premium, excess, limit amounts — are two indexed
`variants` reads and a subtraction, done live; precomputing them across every
excess-option pairing would be ~2M rows for arithmetic that takes under a
millisecond. `closest_matches` stays precomputed per (competitor policy,
state, household) so the card's first hit is still a single PK lookup.

## Checked against every question type the plan lists

| Question type | Real path | Why it's fast |
| --- | --- | --- |
| `head_to_head`, `best_fit` | `policy_comparisons` + `closest_matches`, then two `variants_for()` reads for the rate line | PK lookups; the rate arithmetic is live and trivial |
| `is_covered` | `hospital_cover_for(policy, snapshot, state)` | PK on `(policy, snapshot, state, category)` |
| `waiting_switch`, `accommodation` | `hospital_profile_for(...)` | PK, one row |
| `extras_item` | `extras_benefits_for(...)` joined to the item's limit group via `extras_limit_group_services` | PK + one indexed join |
| `extras_limit` | `extras_limit_groups` by `(policy, snapshot, family_type, dependant_scope)` | Unique index |
| `price`, `excess_options`, `ambulance` | `variants_for(...)` | The card's lookup index |
| `discount`, `fund_fact`, `eligibility` | `funds`, `fund_dependant_limits` | PK on `fund_code` |
| `what_changed` | `changes` | Indexed `(policy_id, snapshot_id)` — one policy across months, the query's actual shape |
| `closed_product` | `policies` | PK |

## Where this leaves the plan's remaining schema pieces

`snapshots`, `changes`, `overrides`, `closest_matches` are kept as the plan
designed them with the rate dimensions added where a change or a match is
per rate. `identity_queue` keys on `gov_product_item_id`. `hospital_agreements`
is normalised out of the CSV's 34-column wide format into a junction table,
so a new fund needs no migration. Staging tables (`stg_*`) hold each release
as published, so a rejected one stays inspectable.

## `discount` has no structured source for Navy Health's own case

Navy Health's 10% ADF/veteran/Active Reservist discount is real (it's in
`FundDescription`/`Restrictions.RestrictionDetails`) but there is **no
structured discount-percentage field anywhere in the schema** for it — the
only discount element the government feed defines is `AgeBasedDiscount`
(the unrelated 18–29 reform discount, already captured on `variants`).
Checked directly against the schema's enumerated elements; nothing like
`MilitaryDiscount` exists.

`discount`'s `reads` is set to `null` rather than a table, because there
isn't one. This has to be Navy-specific config (a fixed line the admin
screen or `config/` carries, in the same spirit as the plan's `overrides`
mechanism) rather than something the ingest job parses — the free text is
there for a human to read, not a figure the AI layer could safely bind a
claim to.

## Eligibility text was missing from the first pass

`eligibility` needs `Fund.Restrictions.RestrictionHint` /
`RestrictionDetails` — free text, not a number, so it is cited whole via a
Fact's `text` field rather than scanned for a figure to bind. The first pass
at this schema omitted both columns entirely; added to `funds` as
`restriction_hint` / `restriction_details`.
