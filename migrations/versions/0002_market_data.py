"""Market data: the government feed, un-flattened into product definitions
and a rate card, plus the derived comparison layer.

Grounded in a full structural scan of the 1 Oct 2026 PHOL extract — every
element of all 7 files counted, every enumeration checked against
PHOLSchema-V3.1.xsd, every structural claim tested against Navy Health's own
rows. docs/DATA_MODEL.md has the findings and the numbers behind each
decision; config/reference.py has the government-fixed enums and the
derivations (family_type, dependant_scope) this schema depends on.

The shape of the data, and therefore of the schema:

  The feed is flat: one <Product> row per RATE (state x household
  composition x excess option), and every row repeats the whole product
  definition — all 38 clinical categories, the waiting periods, every extras
  limit group. Tested across 1,251 named hospital products: the clinical
  categories, waiting periods and ambulance cover are constant across every
  rate of a product in 1,251 of 1,251 cases. What varies per rate is the
  premium, the excess amounts (the per-policy cap doubles for two adults) and
  the extras limit amounts (a per-person/per-policy field flip by household).

  So: a lean rate card (`variants`: one row per government row, only what
  varies) and product-definition side tables keyed by policy (`hospital_cover`,
  `hospital_profiles`, `extras_services`, `extras_benefits`,
  `extras_limit_groups`), written once per policy, not 35 times. The
  comparison engine reads the side tables — ~100k rows in total, loaded once
  per rebuild — and the NLM query layer gets plain indexed lookups and
  "which policies cover X" queries that a JSONB blob could not answer
  without a scan.

  The rate key is proven complete on the real data: (fund, brand, name) x
  state x family_type x dependant_scope x excess structure x co-payment
  structure identifies exactly one row across all 31,864 open hospital rows.
  Two of those dimensions did not exist in the plan's sketch and each would
  have been a wrong-price bug: dependant_scope (a family rate covering adult
  non-student dependants costs ~30% more than one that does not, under the
  same name) and brand (Teachers Health, UniHealth and Nurses & Midwives
  Health are one fund, NTF, selling the same product under three names).

  'ALL' is a real state value (a nationally priced product has one row, not
  seven) and is kept, not exploded: resolving (policy, snapshot, state) to
  the right rows is the one rule the whole query layer has to apply, so it
  is written once, as the *_for() functions at the end of this migration,
  rather than once per question-type SQL file.

Other corrections to the plan's sketch, each with its evidence in
docs/DATA_MODEL.md: the resolver's stable key is ProductItemID, not
ProductID (the plan had it backwards); hospital waiting periods are 4 fixed
titles, never per category; extras limits are grouped across services and
some are by count, not dollars; HospitalTier has 7 values with their own
ranking; FundReport (complaints, retention, market share) occurs zero times
in this feed and needs a separate APRA source.

RLS is on with no anon/authenticated grants, matching migration 0001.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


UPGRADE = r"""
-- ═══════════════════════════════════════════════════════════════════════════
-- Staging: the government files as published, one row per record. COPY'd in,
-- quality-gated, then promoted. Real tables, not temp: a rejected release
-- stays inspectable. Truncated at the start of every ingest.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE stg_products (
    gov_product_item_id  TEXT NOT NULL,
    gov_product_id       TEXT NOT NULL,
    gov_product_code     TEXT NOT NULL,
    fund_code            TEXT NOT NULL,
    product_type         TEXT NOT NULL,          -- Hospital | GeneralHealth | Combined, as the file says
    status               TEXT NOT NULL,          -- Open | Closed
    name                 TEXT NOT NULL,
    state                TEXT NOT NULL,
    premium_month        NUMERIC(8,2) NOT NULL,
    source_file          TEXT NOT NULL,
    raw                  JSONB NOT NULL,         -- the whole parsed <Product>
    PRIMARY KEY (gov_product_item_id)
);
CREATE INDEX ON stg_products (fund_code, name);

CREATE TABLE stg_funds (
    fund_code     TEXT PRIMARY KEY,
    fund_item_id  TEXT NOT NULL,
    raw           JSONB NOT NULL
);

CREATE TABLE stg_agreement_hospitals (
    provider_code TEXT PRIMARY KEY,
    raw           JSONB NOT NULL                 -- the CSV row, fund columns included
);

-- ═══════════════════════════════════════════════════════════════════════════
-- Snapshots and funds
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE snapshots (
    id            BIGSERIAL PRIMARY KEY,
    release_date  DATE NOT NULL UNIQUE,
    source_url    TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'downloading'
                  CHECK (status IN ('downloading', 'staged', 'rejected', 'live', 'superseded')),
    record_counts JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at   TIMESTAMPTZ
);

-- fund_item_id is the stable cross-month key (FundItemID: "common to all
-- versions of a Fund document"); fund_code is what every other table
-- references and what members and reps actually say.
CREATE TABLE funds (
    fund_code       TEXT PRIMARY KEY,
    fund_item_id    TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    fund_type       TEXT NOT NULL CHECK (fund_type IN ('Open', 'Restricted')),
    description     TEXT,
    phone           TEXT,
    email           TEXT,
    website         TEXT,
    not_for_profit  BOOLEAN,
    -- Who may join (Fund.Restrictions): free text, the `eligibility`
    -- question's only source. Cited whole, never scanned for a figure.
    restriction_hint     TEXT,
    restriction_details  TEXT,
    -- Fund-level ambulance cover: the resolved value is on each variant;
    -- this keeps the per-state wording for display.
    ambulance_json  JSONB,
    -- NOT in this feed: FundReport occurs zero times across all 34 funds.
    -- Needs APRA's "State of the Health Funds" statistics, loaded separately.
    complaints_per_10k   NUMERIC(8,2),
    retention_pct        NUMERIC(5,2),
    market_share_pct     NUMERIC(5,2),
    mgmt_expense_pct     NUMERIC(5,2),
    first_seen    DATE NOT NULL,
    last_seen     DATE NOT NULL,
    is_navy       BOOLEAN GENERATED ALWAYS AS (fund_code = 'NHB') STORED
);

-- A consumer-facing name a fund sells under. 9 across the feed: Frank
-- (GMHBA), Hunter Health Insurance (CDH), UniHealth and Nurses & Midwives
-- Health (Teachers Health)... A member says the brand, not the fund.
CREATE TABLE brands (
    brand_code  TEXT PRIMARY KEY,                -- e.g. GMH02
    fund_code   TEXT NOT NULL REFERENCES funds(fund_code),
    name        TEXT NOT NULL,                   -- e.g. Frank Health Insurance
    phone       TEXT,
    email       TEXT,
    website     TEXT
);
CREATE INDEX ON brands (fund_code);

-- Age bands per dependant category (config.reference.DEPENDANT_TITLES), a
-- fund-level fact. Navy Health covers Student dependants to 31; another fund
-- may stop at 24. One row per (fund, category): the direct answer to "can my
-- dependant stay on this policy".
CREATE TABLE fund_dependant_limits (
    fund_code       TEXT NOT NULL REFERENCES funds(fund_code),
    dependant_title TEXT NOT NULL,
    supported       BOOLEAN NOT NULL,
    min_age         SMALLINT,
    max_age         SMALLINT,
    PRIMARY KEY (fund_code, dependant_title)
);

-- ═══════════════════════════════════════════════════════════════════════════
-- Policies: the marketed product. One row for "Prime Silver+ Hospital 750",
-- however many states, households and excess options it is priced for.
-- Keyed by the resolver (week 2) on (fund, brand, name, product_type); the
-- id persists across months, carried forward by whichever of its rate rows
-- exact-match on gov_product_item_id.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE policies (
    id            BIGSERIAL PRIMARY KEY,
    fund_code     TEXT NOT NULL REFERENCES funds(fund_code),
    brand_code    TEXT REFERENCES brands(brand_code),  -- null: sold under the fund's own name
    product_type  TEXT NOT NULL CHECK (product_type IN ('hospital', 'extras', 'combined')),
    current_name  TEXT NOT NULL,
    -- Availability. 28% of open hospital rows are corporate ("Corporate
    -- Cover only"): real for a member who holds one through an employer,
    -- never something a non-eligible member can be offered.
    is_corporate    BOOLEAN NOT NULL DEFAULT FALSE,
    corporate_text  TEXT,
    status        TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('provisional', 'confirmed', 'closed')),
    first_seen    DATE NOT NULL,
    last_seen     DATE NOT NULL,
    is_navy       BOOLEAN GENERATED ALWAYS AS (fund_code = 'NHB') STORED
);
CREATE INDEX ON policies (fund_code, product_type);
CREATE INDEX ON policies (is_navy, product_type) WHERE status != 'closed';

-- What a rep types. Government names, renames the resolver recognised, brand
-- names ("Frank"), and what reps actually say ("medibank top hosp" is
-- promoted to the type-ahead at 3 hits). Trigram-indexed for typo tolerance.
CREATE TABLE policy_aliases (
    policy_id     BIGINT NOT NULL REFERENCES policies(id),
    alias         TEXT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('gov', 'brand', 'rename', 'rep')),
    hits          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (policy_id, alias)
);
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX policy_aliases_trgm ON policy_aliases USING gin (alias gin_trgm_ops);

-- ═══════════════════════════════════════════════════════════════════════════
-- The rate card: one row per government <Product> row, holding only what
-- varies per row. ~132,000 rows per snapshot across the 6 product files.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE variants (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_id     BIGINT NOT NULL REFERENCES snapshots(id),
    policy_id       BIGINT REFERENCES policies(id),       -- null until resolved
    fund_code       TEXT NOT NULL REFERENCES funds(fund_code),
    brand_code      TEXT REFERENCES brands(brand_code),
    product_type    TEXT NOT NULL CHECK (product_type IN ('hospital', 'extras', 'combined')),

    -- Government identity. gov_product_item_id is the STABLE key (resolver
    -- tier 1); gov_product_id changes on every edit (audit only);
    -- gov_product_code is the human code, audit only, never parsed as truth.
    gov_product_item_id    TEXT NOT NULL,
    gov_product_id         TEXT NOT NULL,
    gov_product_code       TEXT NOT NULL,
    gov_product_group_code TEXT,                  -- fund-internal; what OnlyAvailableWith refers to
    gov_funds_product_code TEXT,                  -- the fund's own code, e.g. "FSH7-NSW"
    name            TEXT NOT NULL,                -- this month's exact text; policies.current_name may lag a rename
    status          TEXT NOT NULL CHECK (status IN ('Open', 'Closed')),

    -- The rate dimensions. Together with policy_id these identify exactly
    -- one row (tested: 0 collisions across 31,864 open hospital rows).
    state           TEXT NOT NULL,                -- config.reference.STATES; 'ALL' = nationally priced
    family_type     TEXT NOT NULL CHECK (family_type IN ('single', 'couple', 'family', 'single_parent', 'dependants_only')),
    -- Which dependant categories the rate covers, as the canonical "+"-joined
    -- sorted string ("Child+NonClassified+Student"; '' for none). The
    -- dimension the plan's enum lost: under one name, 'family' is priced
    -- separately with and without adult non-student dependants.
    dependant_scope          TEXT NOT NULL DEFAULT '',
    dependant_titles         TEXT[] NOT NULL DEFAULT '{}',
    covers_adult_dependants  BOOLEAN NOT NULL DEFAULT FALSE,  -- NonStudent or ConditionalNonStudent in scope
    covers_disability_dependants BOOLEAN NOT NULL DEFAULT FALSE,
    who_covered_json JSONB NOT NULL,              -- raw WhoIsCovered, audit

    premium_month              NUMERIC(8,2) NOT NULL,   -- PremiumNoRebate
    premium_hospital_component NUMERIC(8,2),           -- the hospital share of a combined premium

    -- Excess: the figure's meaning depends on the type. Varies by household
    -- (the per-policy cap doubles for two adults), so it is a rate attribute.
    excess_type          TEXT CHECK (excess_type IN ('None', 'PerAdmissionOnly', 'LimitedPerYear', 'LimitedPerPerson', 'LimitedPerPersonPerPolicy')),
    excess_per_admission NUMERIC(8,2),
    excess_per_person    NUMERIC(8,2),
    excess_per_policy    NUMERIC(8,2),
    -- The "which excess option" a rep picks: the per-person figure, else
    -- per-admission, else 0 for 'None'. Constant across a product's
    -- households even when per_policy doubles. Derived at ingest.
    excess_option        NUMERIC(8,2) NOT NULL DEFAULT 0,
    excess_waivers_json  JSONB,

    copayment_type        TEXT CHECK (copayment_type IN ('None', 'Limited', 'Unlimited')),
    copayment_shared      NUMERIC(8,2),
    copayment_shared_max  NUMERIC(8,2),
    copayment_private     NUMERIC(8,2),
    copayment_private_max NUMERIC(8,2),
    copayment_day_surgery NUMERIC(8,2),
    copayment_annual_max  NUMERIC(8,2),

    -- Resolved per row (ProductAmbulance.UseFund=true means "read the fund's").
    ambulance_cover     TEXT CHECK (ambulance_cover IN ('None', 'EmergencyOnly', 'EmergencyAndNonEmergency', 'StateGovt')),
    ambulance_use_fund  BOOLEAN NOT NULL DEFAULT FALSE,

    -- Purchase constraint for the rarer non-'combined' bundling
    -- (OnlyAvailableWith): {"kind": "Products"|"AnyHospital"|"AnyGeneralHealth",
    -- "group_prefixes": [...]}. Display-only at the pilot's scope.
    bundle_requirement_json JSONB,

    raw           JSONB NOT NULL,                 -- the whole parsed row, for backfill without re-parsing the XML
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (snapshot_id, gov_product_item_id)
);
-- The card's rate lookup, and "cheapest X in NSW for a family".
CREATE INDEX ON variants (snapshot_id, policy_id, state, family_type, dependant_scope, excess_option);
CREATE INDEX ON variants (snapshot_id, fund_code, product_type, state, family_type);
CREATE INDEX ON variants (snapshot_id, product_type, state, family_type, premium_month) WHERE status = 'Open';
CREATE INDEX ON variants (gov_product_item_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- Product definition: what is constant across a policy's rates, keyed by
-- policy. `state` is 'ALL' in the common case (constant across states) and a
-- specific state only where the fund varies it — 13 of 1,251 hospital
-- products vary tier/accommodation by state; 33 of 508 extras products vary
-- a benefit amount by state. Never mixed: a policy's rows for one table are
-- either all 'ALL' or all per-state. The *_for() functions below resolve it.
-- ═══════════════════════════════════════════════════════════════════════════

-- Tier, accommodation, the 4 waiting periods, and the hospital-side flags.
CREATE TABLE hospital_profiles (
    policy_id      BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id    BIGINT NOT NULL REFERENCES snapshots(id),
    state          TEXT NOT NULL DEFAULT 'ALL',
    hospital_tier  TEXT NOT NULL CHECK (hospital_tier IN ('Basic', 'BasicPlus', 'Bronze', 'BronzePlus', 'Silver', 'SilverPlus', 'Gold')),
    tier_rank      NUMERIC(2,1) NOT NULL,         -- config.reference.HOSPITAL_TIER_RANK, so SQL can ORDER BY it
    accommodation  TEXT NOT NULL CHECK (accommodation IN ('PrivateOrPublic', 'PrivateSharedPublic', 'PrivateSharedPublicShared', 'Public', 'PublicShared', 'PrivatePublicShared')),
    -- Exactly 4 waits exist in the feed (confirmed across all 31,864 rows);
    -- PregnancyBirth is null when that category is NotCovered.
    wait_pre_existing_months  SMALLINT NOT NULL,
    wait_other_months         SMALLINT NOT NULL,
    wait_sub_acute_months     SMALLINT NOT NULL,
    wait_pregnancy_months     SMALLINT,
    age_discount_available      BOOLEAN NOT NULL,
    age_discount_for_transferee BOOLEAN NOT NULL,
    known_gap_cover             BOOLEAN,
    hospital_percent            NUMERIC(5,2),     -- null = 100%; the rare lower-MBS-benefit case
    other_features_text         TEXT,             -- OtherProductFeatures, display only
    PRIMARY KEY (policy_id, snapshot_id, state)
);
CREATE INDEX ON hospital_profiles (snapshot_id, hospital_tier);

-- The 38 clinical categories: 38 rows per (policy, state). Constant across
-- every rate of a product in 1,251 of 1,251 open hospital products, so this
-- is ~48k rows per snapshot, not the 1.2M a per-rate layout would be.
CREATE TABLE hospital_cover (
    policy_id    BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id  BIGINT NOT NULL REFERENCES snapshots(id),
    state        TEXT NOT NULL DEFAULT 'ALL',
    category     TEXT NOT NULL,                   -- config.reference.CLINICAL_CATEGORIES
    cover        TEXT NOT NULL CHECK (cover IN ('Covered', 'Restricted', 'NotCovered')),
    PRIMARY KEY (policy_id, snapshot_id, state, category)
);
-- "Which policies cover joint replacements": the query a JSONB blob cannot
-- answer without scanning every row.
CREATE INDEX ON hospital_cover (snapshot_id, category, cover, policy_id);

CREATE TABLE extras_profiles (
    policy_id      BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id    BIGINT NOT NULL REFERENCES snapshots(id),
    state          TEXT NOT NULL DEFAULT 'ALL',
    -- "Pick N of M covered services" products (3.6% of extras): the member
    -- does not get every covered service. Flagged, not modelled further yet.
    optional_services     BOOLEAN NOT NULL DEFAULT FALSE,
    max_services_covered  SMALLINT,
    preferred_provider    TEXT,                   -- resolved per state: Covered | NotCovered | NotSpecified
    other_services_text   TEXT,                   -- OtherServices: unlisted extras, display only
    PRIMARY KEY (policy_id, snapshot_id, state)
);

-- The 27 extras services: covered or not, with the service's own wait.
CREATE TABLE extras_services (
    policy_id      BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id    BIGINT NOT NULL REFERENCES snapshots(id),
    state          TEXT NOT NULL DEFAULT 'ALL',
    service        TEXT NOT NULL,                 -- config.reference.EXTRAS_SERVICES
    covered        BOOLEAN NOT NULL,
    wait_months    SMALLINT,
    has_special_features BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (policy_id, snapshot_id, state, service)
);
CREATE INDEX ON extras_services (snapshot_id, service, policy_id) WHERE covered;

-- Example benefit per item (config.reference.EXTRAS_ITEMS): "$42 for a
-- physio initial consult". All dollars in practice (420,388 of 420,388
-- observed); the schema allows a percentage, kept as a column for the day
-- a fund uses it. The one table where state variation is common enough to
-- matter: 33 of 508 open extras products.
CREATE TABLE extras_benefits (
    policy_id      BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id    BIGINT NOT NULL REFERENCES snapshots(id),
    state          TEXT NOT NULL DEFAULT 'ALL',
    service        TEXT NOT NULL,
    item           TEXT NOT NULL,                 -- e.g. DentalMajor615FullCrownVeneered
    benefit_amount NUMERIC(8,2),
    benefit_pct    NUMERIC(5,2),
    PRIMARY KEY (policy_id, snapshot_id, state, service, item)
);

-- Benefit limits, GROUPED: one row is one cap, shared by every service in
-- extras_limit_group_services. Navy Health's own General + Major Dental +
-- Endodontic share one $1,700 cap — a per-service column would state it
-- three times over. Keyed by household too: the amounts flip between
-- per-person and per-policy fields by household (351 of 508 products),
-- never by state (0 of 508).
CREATE TABLE extras_limit_groups (
    id               BIGSERIAL PRIMARY KEY,
    policy_id        BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id      BIGINT NOT NULL REFERENCES snapshots(id),
    family_type      TEXT NOT NULL,
    dependant_scope  TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL,               -- the group's lead service, as the PHIS labels it
    no_benefit_limits    BOOLEAN NOT NULL DEFAULT FALSE,   -- an explicit "unlimited"
    limit_per_person     NUMERIC(8,2),
    limit_per_service    NUMERIC(8,2),
    limit_per_policy     NUMERIC(8,2),
    lifetime_limit       NUMERIC(8,2),
    -- "2 appliances every 3 years": a cap by count, not dollars (5,264 cases).
    service_count        SMALLINT,
    service_count_years  SMALLINT,
    service_count_type   TEXT CHECK (service_count_type IN ('Service', 'Appliance')),
    -- When set, every other field in this row is to be ignored and the
    -- group ranks "Low" — the government schema's own convention (1,481 cases).
    free_text_limit      TEXT,
    UNIQUE (policy_id, snapshot_id, family_type, dependant_scope, title)
);
CREATE TABLE extras_limit_group_services (
    group_id   BIGINT NOT NULL REFERENCES extras_limit_groups(id) ON DELETE CASCADE,
    service    TEXT NOT NULL,
    sub_limits_apply       BOOLEAN NOT NULL DEFAULT FALSE,
    individual_lifetime_limit NUMERIC(8,2),
    PRIMARY KEY (group_id, service)
);
CREATE INDEX ON extras_limit_group_services (service, group_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- Identity, change log, overrides
-- ═══════════════════════════════════════════════════════════════════════════
-- A rate row whose gov_product_item_id matched nothing last month and whose
-- (fund, brand, name) matched zero or several existing policies.
CREATE TABLE identity_queue (
    id                  BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT NOT NULL REFERENCES snapshots(id),
    variant_id          BIGINT NOT NULL REFERENCES variants(id),
    candidates          JSONB NOT NULL,           -- [{policy_id, score, reason}]
    resolved_policy_id  BIGINT REFERENCES policies(id),
    resolved_by         UUID,
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX ON identity_queue (snapshot_id) WHERE resolved_policy_id IS NULL;

-- The monthly diff. A cover or service change is per policy; a premium,
-- excess or limit change is per rate, so the rate dimensions are nullable.
CREATE TABLE changes (
    id               BIGSERIAL PRIMARY KEY,
    snapshot_id      BIGINT NOT NULL REFERENCES snapshots(id),
    policy_id        BIGINT REFERENCES policies(id),
    state            TEXT,
    family_type      TEXT,
    dependant_scope  TEXT,
    field            TEXT NOT NULL,
    old_value        TEXT,
    new_value        TEXT,
    kind             TEXT NOT NULL CHECK (kind IN ('new', 'closed', 'premium', 'cover', 'benefit', 'limit', 'rename', 'excess'))
);
CREATE INDEX ON changes (policy_id, snapshot_id);   -- "what changed since <date>" for one policy, across months

CREATE TABLE overrides (
    id                    BIGSERIAL PRIMARY KEY,
    policy_id             BIGINT NOT NULL REFERENCES policies(id),
    field                 TEXT NOT NULL,
    value                 TEXT NOT NULL,
    effective             DATE NOT NULL,
    notified_by           TEXT NOT NULL,
    evidence              TEXT,
    created_by            UUID NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_snapshot_id BIGINT REFERENCES snapshots(id),
    status                TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'confirmed', 'unconfirmed', 'withdrawn'))
);
CREATE INDEX ON overrides (policy_id) WHERE status = 'active';

-- ═══════════════════════════════════════════════════════════════════════════
-- Derived comparison layer, rebuilt per snapshot.
--
-- The plan precomputed one row per (pair, state, household). With the
-- product/rate split, 90% of the work — the 38 categories, 27 services,
-- tier distance, the similarity score — is per policy PAIR and constant
-- across its ~35 rates. It is computed once per pair here (~13k rows), not
-- ~450k times. The rate-level differences (premium, excess, limits) are two
-- indexed `variants` reads and a subtraction, done live. The card's first
-- hit stays a single PK lookup: closest_matches.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE policy_comparisons (
    snapshot_id     BIGINT NOT NULL REFERENCES snapshots(id),
    navy_policy_id  BIGINT NOT NULL REFERENCES policies(id),
    comp_policy_id  BIGINT NOT NULL REFERENCES policies(id),
    state           TEXT NOT NULL DEFAULT 'ALL',  -- per state only where either side's definition varies by state
    score           NUMERIC(6,3) NOT NULL,        -- cover similarity, for closest match
    differences     JSONB NOT NULL,               -- typed, ranked by materiality.yaml; see engine/compare.py (week 2)
    PRIMARY KEY (snapshot_id, comp_policy_id, navy_policy_id, state)
);

CREATE TABLE closest_matches (
    snapshot_id      BIGINT NOT NULL REFERENCES snapshots(id),
    comp_policy_id   BIGINT NOT NULL REFERENCES policies(id),
    state            TEXT NOT NULL,
    family_type      TEXT NOT NULL,
    dependant_scope  TEXT NOT NULL DEFAULT '',
    rank             SMALLINT NOT NULL,
    navy_policy_id   BIGINT NOT NULL REFERENCES policies(id),
    score            NUMERIC(6,3) NOT NULL,       -- cover similarity + premium proximity for this rate
    PRIMARY KEY (snapshot_id, comp_policy_id, state, family_type, dependant_scope, rank)
);

-- ═══════════════════════════════════════════════════════════════════════════
-- Agreement hospitals: the CSV's 34-columns-per-fund wide format normalised.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE hospital_agreements (
    provider_code TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    address_line1 TEXT,
    address_line2 TEXT,
    town          TEXT,
    state         TEXT NOT NULL,
    postcode      TEXT,
    latitude      NUMERIC(9,6),
    longitude     NUMERIC(9,6),
    is_day        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ON hospital_agreements (state);

CREATE TABLE hospital_agreement_funds (
    provider_code TEXT NOT NULL REFERENCES hospital_agreements(provider_code),
    fund_code     TEXT NOT NULL REFERENCES funds(fund_code),
    PRIMARY KEY (provider_code, fund_code)
);
CREATE INDEX ON hospital_agreement_funds (fund_code);

-- ═══════════════════════════════════════════════════════════════════════════
-- Resolving 'ALL': the one rule the query layer must get right, written
-- once. Each returns the rows for a specific state if the policy has them,
-- else its 'ALL' rows. A question-type SQL file calls these; it never
-- writes `state IN (...)` itself.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE FUNCTION hospital_profile_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT)
RETURNS SETOF hospital_profiles LANGUAGE sql STABLE AS $$
    SELECT * FROM hospital_profiles
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot
      AND state = CASE WHEN EXISTS (SELECT 1 FROM hospital_profiles
                                    WHERE policy_id = p_policy AND snapshot_id = p_snapshot AND state = p_state)
                       THEN p_state ELSE 'ALL' END;
$$;

CREATE FUNCTION hospital_cover_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT)
RETURNS SETOF hospital_cover LANGUAGE sql STABLE AS $$
    SELECT * FROM hospital_cover
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot
      AND state = CASE WHEN EXISTS (SELECT 1 FROM hospital_cover
                                    WHERE policy_id = p_policy AND snapshot_id = p_snapshot AND state = p_state)
                       THEN p_state ELSE 'ALL' END;
$$;

CREATE FUNCTION extras_services_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT)
RETURNS SETOF extras_services LANGUAGE sql STABLE AS $$
    SELECT * FROM extras_services
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot
      AND state = CASE WHEN EXISTS (SELECT 1 FROM extras_services
                                    WHERE policy_id = p_policy AND snapshot_id = p_snapshot AND state = p_state)
                       THEN p_state ELSE 'ALL' END;
$$;

CREATE FUNCTION extras_benefits_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT)
RETURNS SETOF extras_benefits LANGUAGE sql STABLE AS $$
    SELECT * FROM extras_benefits
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot
      AND state = CASE WHEN EXISTS (SELECT 1 FROM extras_benefits
                                    WHERE policy_id = p_policy AND snapshot_id = p_snapshot AND state = p_state)
                       THEN p_state ELSE 'ALL' END;
$$;

-- The rate rows for a state: a nationally priced product's 'ALL' row counts.
CREATE FUNCTION variants_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT)
RETURNS SETOF variants LANGUAGE sql STABLE AS $$
    SELECT * FROM variants
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot AND state IN (p_state, 'ALL');
$$;

-- ═══════════════════════════════════════════════════════════════════════════
-- Access: service role only, matching migration 0001's blanket policy.
-- ═══════════════════════════════════════════════════════════════════════════
ALTER TABLE stg_products                ENABLE ROW LEVEL SECURITY;
ALTER TABLE stg_funds                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE stg_agreement_hospitals     ENABLE ROW LEVEL SECURITY;
ALTER TABLE snapshots                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE funds                       ENABLE ROW LEVEL SECURITY;
ALTER TABLE brands                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE fund_dependant_limits       ENABLE ROW LEVEL SECURITY;
ALTER TABLE policies                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_aliases              ENABLE ROW LEVEL SECURITY;
ALTER TABLE variants                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_profiles           ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_cover              ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras_profiles             ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras_services             ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras_benefits             ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras_limit_groups         ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras_limit_group_services ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_queue              ENABLE ROW LEVEL SECURITY;
ALTER TABLE changes                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE overrides                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_comparisons          ENABLE ROW LEVEL SECURITY;
ALTER TABLE closest_matches             ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_agreements         ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_agreement_funds    ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON stg_products, stg_funds, stg_agreement_hospitals, snapshots, funds, brands,
              fund_dependant_limits, policies, policy_aliases, variants, hospital_profiles,
              hospital_cover, extras_profiles, extras_services, extras_benefits,
              extras_limit_groups, extras_limit_group_services, identity_queue, changes,
              overrides, policy_comparisons, closest_matches, hospital_agreements,
              hospital_agreement_funds
    FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON stg_products, stg_funds, stg_agreement_hospitals, snapshots, funds, brands,
       fund_dependant_limits, policies, policy_aliases, variants, hospital_profiles,
       hospital_cover, extras_profiles, extras_services, extras_benefits,
       extras_limit_groups, extras_limit_group_services, identity_queue, changes,
       overrides, policy_comparisons, closest_matches, hospital_agreements,
       hospital_agreement_funds
    TO service_role;
GRANT USAGE, SELECT ON SEQUENCE
    snapshots_id_seq, policies_id_seq, variants_id_seq, extras_limit_groups_id_seq,
    identity_queue_id_seq, changes_id_seq, overrides_id_seq
    TO service_role;
REVOKE ALL ON FUNCTION hospital_profile_for(BIGINT, BIGINT, TEXT), hospital_cover_for(BIGINT, BIGINT, TEXT),
              extras_services_for(BIGINT, BIGINT, TEXT), extras_benefits_for(BIGINT, BIGINT, TEXT),
              variants_for(BIGINT, BIGINT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION hospital_profile_for(BIGINT, BIGINT, TEXT), hospital_cover_for(BIGINT, BIGINT, TEXT),
              extras_services_for(BIGINT, BIGINT, TEXT), extras_benefits_for(BIGINT, BIGINT, TEXT),
              variants_for(BIGINT, BIGINT, TEXT)
    TO service_role;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS variants_for(BIGINT, BIGINT, TEXT), extras_benefits_for(BIGINT, BIGINT, TEXT),
    extras_services_for(BIGINT, BIGINT, TEXT), hospital_cover_for(BIGINT, BIGINT, TEXT),
    hospital_profile_for(BIGINT, BIGINT, TEXT);
DROP TABLE IF EXISTS hospital_agreement_funds, hospital_agreements CASCADE;
DROP TABLE IF EXISTS closest_matches, policy_comparisons, overrides, changes, identity_queue CASCADE;
DROP TABLE IF EXISTS extras_limit_group_services, extras_limit_groups, extras_benefits, extras_services,
    extras_profiles, hospital_cover, hospital_profiles CASCADE;
DROP TABLE IF EXISTS variants CASCADE;
DROP TABLE IF EXISTS policy_aliases, policies CASCADE;
DROP TABLE IF EXISTS fund_dependant_limits, brands, funds CASCADE;
DROP TABLE IF EXISTS snapshots CASCADE;
DROP TABLE IF EXISTS stg_agreement_hospitals, stg_funds, stg_products CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
