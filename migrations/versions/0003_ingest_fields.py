"""Ingest fields: the feed attributes that 0002 did not yet have a column for.

Found when the parser was written against the real 1 Oct 2026 extract, not
guessed in advance: each one is a published attribute a member or rep asks
about (is the policy corporate-only, does the fund sell to students, what
does the fund's preferred-provider list say), or a value the comparison
needs (limit groups with no annual cap).

Also: variants.raw now holds the rate fields only. The whole government
row lives in stg_products.raw for the current release, so the rate card does
not carry the product definition twice.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


UPGRADE = r"""
-- Agreement hospitals, re-keyed. 0002 keyed a hospital by its provider code,
-- but the feed gives two campuses one provider code (Calvary Lenah Valley and
-- Calvary St John's both carry 0085010F; three such pairs in the 1 Oct 2026
-- list). A site is its provider code and its title. Nothing had been loaded
-- into these tables when this changed, so they are rebuilt, not migrated.
DROP TABLE IF EXISTS hospital_agreement_funds, hospital_agreements, stg_agreement_hospitals CASCADE;

CREATE TABLE stg_agreement_hospitals (
    row_no        INTEGER PRIMARY KEY,           -- the CSV row, so a repeated provider code is not a key clash
    provider_code TEXT NOT NULL,
    raw           JSONB NOT NULL
);

CREATE TABLE hospital_agreements (
    agreement_id  BIGSERIAL PRIMARY KEY,
    provider_code TEXT NOT NULL,
    title         TEXT NOT NULL,
    address_line1 TEXT,
    address_line2 TEXT,
    town          TEXT,
    state         TEXT NOT NULL,
    postcode      TEXT,
    latitude      NUMERIC(9,6),
    longitude     NUMERIC(9,6),
    is_day        BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (provider_code, title)
);
CREATE INDEX ON hospital_agreements (state);

CREATE TABLE hospital_agreement_funds (
    agreement_id  BIGINT NOT NULL REFERENCES hospital_agreements(agreement_id) ON DELETE CASCADE,
    fund_code     TEXT NOT NULL REFERENCES funds(fund_code),
    PRIMARY KEY (agreement_id, fund_code)
);
CREATE INDEX ON hospital_agreement_funds (fund_code);

ALTER TABLE stg_agreement_hospitals     ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_agreements         ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_agreement_funds    ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON stg_agreement_hospitals, hospital_agreements, hospital_agreement_funds FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON stg_agreement_hospitals, hospital_agreements, hospital_agreement_funds TO service_role;
GRANT USAGE, SELECT ON SEQUENCE hospital_agreements_agreement_id_seq TO service_role;
REVOKE ALL ON SEQUENCE hospital_agreements_agreement_id_seq FROM anon, authenticated;

-- Staging for the content that many rate rows share, stored once by id:
-- a product's definition for one state (kind 'hospital' or 'general'), and a
-- limit group. A staged rate row refers to these by id. Truncated per ingest.
CREATE TABLE stg_definitions (
    def_id  TEXT PRIMARY KEY,
    kind    TEXT NOT NULL CHECK (kind IN ('hospital', 'general')),
    body    JSONB NOT NULL
);
CREATE TABLE stg_limit_groups (
    lg_id   TEXT PRIMARY KEY,
    body    JSONB NOT NULL
);
ALTER TABLE stg_definitions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE stg_limit_groups ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON stg_definitions, stg_limit_groups FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON stg_definitions, stg_limit_groups TO service_role;

-- A policy's identity is its fund, brand, product type, the government's
-- product group code, and its name. The name alone conflates distinct
-- products: 31 names in the 1 Oct 2026 extract carry two or three group
-- codes (AHM's "elite gold copay" is J138 and J139). The group code is the
-- government's own product-family identifier: the prefix of ProductCode and
-- the code OnlyAvailableWith refers to.
ALTER TABLE policies ADD COLUMN product_group_code TEXT;
CREATE INDEX ON policies (fund_code, product_type, current_name);

-- Extras definitions can differ by household, not only by state: 24 product
-- families price single-person benefits and waits apart from couple and
-- family ones (keyed by name, group and state, these were the only general
-- definitions that were not unique). The extras side tables therefore carry
-- a family_type as well as a state. 'ALL' is the default on both, and the
-- *_for() resolvers fall back from exact, to state-only, to family-only, to
-- ALL. Hospital definitions do not vary by household (0 conflicts) and keep
-- state alone.
ALTER TABLE extras_profiles ADD COLUMN family_type TEXT NOT NULL DEFAULT 'ALL'
    CHECK (family_type IN ('ALL', 'single', 'couple', 'family', 'single_parent', 'dependants_only'));
ALTER TABLE extras_services ADD COLUMN family_type TEXT NOT NULL DEFAULT 'ALL'
    CHECK (family_type IN ('ALL', 'single', 'couple', 'family', 'single_parent', 'dependants_only'));
ALTER TABLE extras_benefits ADD COLUMN family_type TEXT NOT NULL DEFAULT 'ALL'
    CHECK (family_type IN ('ALL', 'single', 'couple', 'family', 'single_parent', 'dependants_only'));
ALTER TABLE extras_profiles DROP CONSTRAINT extras_profiles_pkey,
    ADD PRIMARY KEY (policy_id, snapshot_id, state, family_type);
ALTER TABLE extras_services DROP CONSTRAINT extras_services_pkey,
    ADD PRIMARY KEY (policy_id, snapshot_id, state, family_type, service);
ALTER TABLE extras_benefits DROP CONSTRAINT extras_benefits_pkey,
    ADD PRIMARY KEY (policy_id, snapshot_id, state, family_type, service, item);

-- Comparisons carry the household too, for the same reason.
ALTER TABLE policy_comparisons ADD COLUMN family_type TEXT NOT NULL DEFAULT 'ALL'
    CHECK (family_type IN ('ALL', 'single', 'couple', 'family', 'single_parent', 'dependants_only'));
ALTER TABLE policy_comparisons DROP CONSTRAINT policy_comparisons_pkey,
    ADD PRIMARY KEY (snapshot_id, comp_policy_id, navy_policy_id, state, family_type);

-- The resolvers, with the household. The old three-argument versions are
-- replaced: nothing outside the database called them yet.
DROP FUNCTION extras_services_for(BIGINT, BIGINT, TEXT);
DROP FUNCTION extras_benefits_for(BIGINT, BIGINT, TEXT);

-- Per service (and per item), the most specific row wins: this state and
-- this household, then this state for any household, then any state for
-- this household, then the nationally common row. Per key, not per list, so
-- one state's override of one benefit does not hide the others.
CREATE FUNCTION extras_services_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT, p_family TEXT)
RETURNS SETOF extras_services LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (service) * FROM extras_services
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot
      AND state IN (p_state, 'ALL') AND family_type IN (p_family, 'ALL')
    ORDER BY service, (state = p_state) DESC, (family_type = p_family) DESC;
$$;

CREATE FUNCTION extras_benefits_for(p_policy BIGINT, p_snapshot BIGINT, p_state TEXT, p_family TEXT)
RETURNS SETOF extras_benefits LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (service, item) * FROM extras_benefits
    WHERE policy_id = p_policy AND snapshot_id = p_snapshot
      AND state IN (p_state, 'ALL') AND family_type IN (p_family, 'ALL')
    ORDER BY service, item, (state = p_state) DESC, (family_type = p_family) DESC;
$$;

-- Funds: the record's remaining published fields.
ALTER TABLE funds
    ADD COLUMN corporate_structure    TEXT,
    ADD COLUMN states                 TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN address_json           JSONB,
    ADD COLUMN preferred_providers_json JSONB,
    ADD COLUMN policy_options_json    JSONB,
    ADD COLUMN optional_coverage_json JSONB;   -- OSStudents, OSVisitors, CorporateProducts: who the fund will cover at all

-- Rate card: per-row availability and validity, and the product's
-- Medicare Levy Surcharge exemption (it is a per-product flag the member asks about).
ALTER TABLE variants
    ADD COLUMN is_corporate     BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN corporate_text   TEXT,
    ADD COLUMN mls_exempt       BOOLEAN,
    ADD COLUMN valid_from       DATE,
    ADD COLUMN valid_to         DATE;

COMMENT ON COLUMN variants.raw IS
    'The rate fields as the ingest read them, for backfill. The whole government row is in stg_products.raw for the release that was staged.';

-- Hospital: the limit on hospital days, when the product has one.
ALTER TABLE hospital_profiles
    ADD COLUMN limit_hospital_days SMALLINT;

-- Extras: the preferred-provider free text, and whether a limit group has no annual cap.
ALTER TABLE extras_profiles
    ADD COLUMN preferred_provider_text TEXT;

-- Limit groups, re-created with a state. The 1 Oct 2026 extract prices some
-- limits by state (113 household-and-title groups: NT and SA carry $500 per
-- person where other states carry $400), which 0002 assumed never happens.
-- Nothing had loaded into these tables, so they are rebuilt, not migrated.
DROP TABLE IF EXISTS extras_limit_group_services, extras_limit_groups CASCADE;

CREATE TABLE extras_limit_groups (
    id                   BIGSERIAL PRIMARY KEY,
    policy_id            BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id          BIGINT NOT NULL REFERENCES snapshots(id),
    state                TEXT NOT NULL DEFAULT 'ALL',
    family_type          TEXT NOT NULL,
    dependant_scope      TEXT NOT NULL DEFAULT '',
    title                TEXT NOT NULL,
    no_benefit_limits    BOOLEAN NOT NULL DEFAULT FALSE,
    annual_no_limit      BOOLEAN NOT NULL DEFAULT FALSE,
    limit_per_person     NUMERIC(8,2),
    limit_per_service    NUMERIC(8,2),
    limit_per_policy     NUMERIC(8,2),
    lifetime_limit       NUMERIC(8,2),
    service_count        SMALLINT,
    service_count_years  SMALLINT,
    service_count_type   TEXT CHECK (service_count_type IN ('Service', 'Appliance')),
    free_text_limit      TEXT,
    UNIQUE (policy_id, snapshot_id, state, family_type, dependant_scope, title)
);
CREATE TABLE extras_limit_group_services (
    group_id                  BIGINT NOT NULL REFERENCES extras_limit_groups(id) ON DELETE CASCADE,
    service                   TEXT NOT NULL,
    sub_limits_apply          BOOLEAN NOT NULL DEFAULT FALSE,
    individual_lifetime_limit NUMERIC(8,2),
    PRIMARY KEY (group_id, service)
);
CREATE INDEX ON extras_limit_group_services (service, group_id);
ALTER TABLE extras_limit_groups         ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras_limit_group_services ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON extras_limit_groups, extras_limit_group_services TO service_role;
GRANT USAGE, SELECT ON SEQUENCE extras_limit_groups_id_seq TO service_role;
REVOKE ALL ON SEQUENCE extras_limit_groups_id_seq FROM anon, authenticated;

-- Supabase grants every new public table, sequence and function to anon and
-- authenticated by default. The two staging tables above are revoked by name;
-- this is the same end-of-migration sweep 0001 and 0002 end with, so anything
-- created in this migration is covered by the same rule.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS extras_benefits_for(BIGINT, BIGINT, TEXT, TEXT), extras_services_for(BIGINT, BIGINT, TEXT, TEXT);
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
ALTER TABLE policy_comparisons DROP CONSTRAINT policy_comparisons_pkey,
    ADD PRIMARY KEY (snapshot_id, comp_policy_id, navy_policy_id, state);
ALTER TABLE policy_comparisons DROP COLUMN IF EXISTS family_type;
ALTER TABLE extras_benefits DROP CONSTRAINT extras_benefits_pkey,
    ADD PRIMARY KEY (policy_id, snapshot_id, state, service, item);
ALTER TABLE extras_services DROP CONSTRAINT extras_services_pkey,
    ADD PRIMARY KEY (policy_id, snapshot_id, state, service);
ALTER TABLE extras_profiles DROP CONSTRAINT extras_profiles_pkey,
    ADD PRIMARY KEY (policy_id, snapshot_id, state);
ALTER TABLE extras_benefits DROP COLUMN IF EXISTS family_type;
ALTER TABLE extras_services DROP COLUMN IF EXISTS family_type;
ALTER TABLE extras_profiles DROP COLUMN IF EXISTS family_type;
DROP INDEX IF EXISTS policies_fund_code_product_type_current_name_idx;
ALTER TABLE policies DROP COLUMN IF EXISTS product_group_code;
DROP TABLE IF EXISTS stg_limit_groups, stg_definitions;
DROP TABLE IF EXISTS hospital_agreement_funds, hospital_agreements, stg_agreement_hospitals CASCADE;
CREATE TABLE stg_agreement_hospitals (
    provider_code TEXT PRIMARY KEY,
    raw           JSONB NOT NULL
);
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
DROP TABLE IF EXISTS extras_limit_group_services, extras_limit_groups CASCADE;
CREATE TABLE extras_limit_groups (
    id                   BIGSERIAL PRIMARY KEY,
    policy_id            BIGINT NOT NULL REFERENCES policies(id),
    snapshot_id          BIGINT NOT NULL REFERENCES snapshots(id),
    family_type          TEXT NOT NULL,
    dependant_scope      TEXT NOT NULL DEFAULT '',
    title                TEXT NOT NULL,
    no_benefit_limits    BOOLEAN NOT NULL DEFAULT FALSE,
    limit_per_person     NUMERIC(8,2),
    limit_per_service    NUMERIC(8,2),
    limit_per_policy     NUMERIC(8,2),
    lifetime_limit       NUMERIC(8,2),
    service_count        SMALLINT,
    service_count_years  SMALLINT,
    service_count_type   TEXT CHECK (service_count_type IN ('Service', 'Appliance')),
    free_text_limit      TEXT,
    UNIQUE (policy_id, snapshot_id, family_type, dependant_scope, title)
);
CREATE TABLE extras_limit_group_services (
    group_id                  BIGINT NOT NULL REFERENCES extras_limit_groups(id) ON DELETE CASCADE,
    service                   TEXT NOT NULL,
    sub_limits_apply          BOOLEAN NOT NULL DEFAULT FALSE,
    individual_lifetime_limit NUMERIC(8,2),
    PRIMARY KEY (group_id, service)
);
CREATE INDEX ON extras_limit_group_services (service, group_id);
ALTER TABLE extras_profiles     DROP COLUMN IF EXISTS preferred_provider_text;
ALTER TABLE hospital_profiles   DROP COLUMN IF EXISTS limit_hospital_days;
COMMENT ON COLUMN variants.raw IS NULL;
ALTER TABLE variants
    DROP COLUMN IF EXISTS valid_to,
    DROP COLUMN IF EXISTS valid_from,
    DROP COLUMN IF EXISTS mls_exempt,
    DROP COLUMN IF EXISTS corporate_text,
    DROP COLUMN IF EXISTS is_corporate;
ALTER TABLE funds
    DROP COLUMN IF EXISTS optional_coverage_json,
    DROP COLUMN IF EXISTS policy_options_json,
    DROP COLUMN IF EXISTS preferred_providers_json,
    DROP COLUMN IF EXISTS address_json,
    DROP COLUMN IF EXISTS states,
    DROP COLUMN IF EXISTS corporate_structure;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
