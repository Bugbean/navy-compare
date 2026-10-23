-- Promote a staged release into the live tables.
--
-- Run by jobs/ingest.py as one transaction (psql -1), so any gate that fails
-- leaves the previous live snapshot exactly as it was. Inputs, passed as psql
-- variables by the runner:
--   :snapshot_id          the snapshots row this release is loaded under (status 'staged')
--   :'release_date'       its release date
--   :prev_snapshot_id     the live snapshot it replaces, or 0 for a first load
--   :expected_products    the total of the six product files' own Count attributes
--   :expected_funds       the Funds file's own Count
--
-- Order matters and is the point of this file: funds and brands first (every
-- rate names a brand), then identity (which policy each rate belongs to),
-- then the rate card, then the side tables, then the diff against the
-- previous snapshot, and only then do the policies take their new names and
-- statuses. The diff needs the old values, so it reads them before they change.
--
-- Gates use pg_temp.gate(), which raises with the message when the condition
-- is false. Nothing here guesses: a release that does not fit fails loudly.

-- A full release is one long transaction. Hosted Postgres sets a statement
-- timeout on the pooled role; this session is the loader, not an API request.
SET statement_timeout = 0;

CREATE FUNCTION pg_temp.gate(ok boolean, msg text) RETURNS void
LANGUAGE plpgsql AS $f$
BEGIN
    IF NOT ok THEN
        RAISE EXCEPTION 'ingest gate failed: %', msg;
    END IF;
END
$f$;

-- ═══════════════════════════════════════════════════════════════════════════
-- Gates on the staged data
-- ═══════════════════════════════════════════════════════════════════════════
SELECT pg_temp.gate(EXISTS (SELECT 1 FROM snapshots WHERE id = :snapshot_id AND status = 'staged'),
                    'snapshot is not in status staged');
SELECT pg_temp.gate((SELECT count(*) FROM stg_products) = :expected_products,
                    'stg_products does not hold the number of rows the files say (' || :expected_products || ')');
SELECT pg_temp.gate((SELECT count(*) FROM stg_funds) = :expected_funds,
                    'stg_funds does not hold the number of rows the Funds file says (' || :expected_funds || ')');
SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM stg_products
                                WHERE state NOT IN ('NSW', 'VIC', 'QLD', 'SA', 'WA', 'TAS', 'NT', 'ACT', 'ALL')),
                    'a product has a State outside config.reference.STATES');
SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM stg_products p
                                WHERE NOT EXISTS (SELECT 1 FROM stg_funds f WHERE f.fund_code = p.fund_code)),
                    'a product names a fund that is not in the Funds file');

-- ═══════════════════════════════════════════════════════════════════════════
-- Funds, their brands, age bands, and agreement hospitals
-- ═══════════════════════════════════════════════════════════════════════════
INSERT INTO funds (fund_code, fund_item_id, name, fund_type, description, phone, email, website,
                   restriction_hint, restriction_details, ambulance_json, corporate_structure, states,
                   address_json, preferred_providers_json, policy_options_json, optional_coverage_json,
                   first_seen, last_seen)
SELECT raw->>'fund_code', raw->>'fund_item_id', raw->>'name', raw->>'fund_type', raw->>'description',
       raw->>'phone', raw->>'email', raw->>'website', raw->>'restriction_hint', raw->>'restriction_details',
       raw->'ambulance', raw->>'corporate_structure',
       ARRAY(SELECT jsonb_array_elements_text(raw->'states')),
       raw->'address', raw->'preferred_providers', raw->'policy_options', raw->'optional_coverage',
       :'release_date', :'release_date'
FROM stg_funds
ON CONFLICT (fund_code) DO UPDATE SET
    fund_item_id = EXCLUDED.fund_item_id, name = EXCLUDED.name, fund_type = EXCLUDED.fund_type,
    description = EXCLUDED.description, phone = EXCLUDED.phone, email = EXCLUDED.email,
    website = EXCLUDED.website, restriction_hint = EXCLUDED.restriction_hint,
    restriction_details = EXCLUDED.restriction_details, ambulance_json = EXCLUDED.ambulance_json,
    corporate_structure = EXCLUDED.corporate_structure, states = EXCLUDED.states,
    address_json = EXCLUDED.address_json, preferred_providers_json = EXCLUDED.preferred_providers_json,
    policy_options_json = EXCLUDED.policy_options_json, optional_coverage_json = EXCLUDED.optional_coverage_json,
    last_seen = EXCLUDED.last_seen;

INSERT INTO brands (brand_code, fund_code, name)
SELECT b->>'brand_code', f.raw->>'fund_code', b->>'name'
FROM stg_funds f CROSS JOIN LATERAL jsonb_array_elements(f.raw->'brands') b
ON CONFLICT (brand_code) DO UPDATE SET fund_code = EXCLUDED.fund_code, name = EXCLUDED.name;

DELETE FROM fund_dependant_limits WHERE fund_code IN (SELECT fund_code FROM stg_funds);
INSERT INTO fund_dependant_limits (fund_code, dependant_title, supported, min_age, max_age)
SELECT f.raw->>'fund_code', d->>'title', (d->>'supported')::boolean,
       (d->>'min_age')::smallint, (d->>'max_age')::smallint
FROM stg_funds f CROSS JOIN LATERAL jsonb_array_elements(f.raw->'dependant_limits') d;

SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM stg_products p
                                WHERE p.raw->'rate'->>'brand_code' IS NOT NULL
                                  AND NOT EXISTS (SELECT 1 FROM brands b WHERE b.brand_code = p.raw->'rate'->>'brand_code')),
                    'a product names a brand code that no fund lists');

DELETE FROM hospital_agreements;  -- its funds go with it (ON DELETE CASCADE): the list is replaced whole
INSERT INTO hospital_agreements (provider_code, title, address_line1, address_line2, town, state, postcode,
                                 latitude, longitude, is_day)
SELECT provider_code, raw->>'title', raw->>'address_line1', raw->>'address_line2', raw->>'town',
       raw->>'state', raw->>'postcode', (raw->>'latitude')::numeric, (raw->>'longitude')::numeric,
       (raw->>'is_day')::boolean
FROM stg_agreement_hospitals;

INSERT INTO hospital_agreement_funds (agreement_id, fund_code)
SELECT a.agreement_id, f.value
FROM hospital_agreements a
JOIN stg_agreement_hospitals s ON s.provider_code = a.provider_code AND s.raw->>'title' = a.title
CROSS JOIN LATERAL jsonb_array_elements_text(s.raw->'funds') f(value);

-- ═══════════════════════════════════════════════════════════════════════════
-- One row per government product: the rate fields, with its policy to be
-- decided below. The product's own key for policy identity is its fund,
-- brand, product type and name.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE rate_rows AS
SELECT p.gov_product_item_id AS item,
       p.gov_product_id, p.gov_product_code, p.fund_code,
       NULLIF(p.raw->'rate'->>'brand_code', '') AS brand_code,
       CASE p.product_type WHEN 'Hospital' THEN 'hospital' WHEN 'GeneralHealth' THEN 'extras'
                           WHEN 'Combined' THEN 'combined' END AS product_type,
       p.status, p.name, p.state, p.premium_month::numeric AS premium, p.raw->'rate' AS rate,
       COALESCE(p.raw->'rate'->>'gov_product_group_code', '') AS group_code
FROM stg_products p;
CREATE INDEX ON rate_rows (item);

-- Which rows of the previous snapshot each current row replaces, by the
-- government's own stable ProductItemID.
CREATE TEMP TABLE carried AS
SELECT r.item, v.policy_id
FROM rate_rows r
JOIN variants v ON v.gov_product_item_id = r.item AND v.snapshot_id = :prev_snapshot_id;

-- ═══════════════════════════════════════════════════════════════════════════
-- Identity. A product group (fund, brand, type, government product group and
-- name) takes the policy its rows carried from last month. A group with no
-- carried rows takes an existing policy with the same key, and only otherwise
-- a new policy. Two groups claiming one policy, or one group claiming two,
-- stop the release: that is a rename or a split a person has to look at.
--
-- The product group is part of the key because the name alone is not one
-- product: 31 names here carry two or three group codes.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE product_groups AS
SELECT fund_code, COALESCE(brand_code, '') AS brand, product_type, group_code, name, count(*) AS n
FROM rate_rows GROUP BY 1, 2, 3, 4, 5;

CREATE TEMP TABLE group_candidates AS
SELECT g.fund_code, g.brand, g.product_type, g.group_code, g.name, c.policy_id, count(*) AS rows_carried
FROM product_groups g
JOIN rate_rows r ON r.fund_code = g.fund_code AND COALESCE(r.brand_code, '') = g.brand
                AND r.product_type = g.product_type AND r.group_code = g.group_code AND r.name = g.name
JOIN carried c ON c.item = r.item
GROUP BY 1, 2, 3, 4, 5, 6;

SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM group_candidates
                                GROUP BY fund_code, brand, product_type, group_code, name HAVING count(*) > 1),
                    'a product group carried rows from two different policies last month');
SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM (SELECT DISTINCT policy_id, fund_code, brand, product_type, group_code, name
                                               FROM group_candidates) x
                                GROUP BY policy_id HAVING count(*) > 1),
                    'two product groups claim the same policy (a rename or a split to review)');

CREATE TEMP TABLE group_policy AS
SELECT g.fund_code, g.brand, g.product_type, g.group_code, g.name,
       COALESCE(
           (SELECT c.policy_id FROM group_candidates c
             WHERE c.fund_code = g.fund_code AND c.brand = g.brand AND c.product_type = g.product_type
               AND c.group_code = g.group_code AND c.name = g.name),
           (SELECT p.id FROM policies p
             WHERE p.fund_code = g.fund_code AND COALESCE(p.brand_code, '') = g.brand
               AND p.product_type = g.product_type AND COALESCE(p.product_group_code, '') = g.group_code
               AND p.current_name = g.name
             ORDER BY p.id LIMIT 1)
       ) AS policy_id,
       false AS created
FROM product_groups g;

SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM group_policy WHERE policy_id IS NOT NULL
                                GROUP BY policy_id HAVING count(*) > 1),
                    'two product groups resolve to the same existing policy');

INSERT INTO policies (fund_code, brand_code, product_type, product_group_code, current_name, status, first_seen, last_seen)
SELECT fund_code, NULLIF(brand, ''), product_type, NULLIF(group_code, ''), name, 'confirmed',
       :'release_date', :'release_date'
FROM group_policy WHERE policy_id IS NULL;

UPDATE group_policy gp SET policy_id = p.id, created = true
FROM policies p
WHERE gp.policy_id IS NULL
  AND p.fund_code = gp.fund_code AND COALESCE(p.brand_code, '') = gp.brand
  AND p.product_type = gp.product_type AND COALESCE(p.product_group_code, '') = gp.group_code
  AND p.current_name = gp.name AND p.first_seen = :'release_date';

CREATE TEMP TABLE row_policy AS
SELECT r.item, gp.policy_id
FROM rate_rows r
JOIN group_policy gp ON gp.fund_code = r.fund_code AND gp.brand = COALESCE(r.brand_code, '')
                    AND gp.product_type = r.product_type AND gp.group_code = r.group_code AND gp.name = r.name;
CREATE UNIQUE INDEX ON row_policy (item);

-- The policy's own facts this month: corporate-only if every rate is, and
-- confirmed if any rate is open.
CREATE TEMP TABLE policy_facts AS
SELECT rp.policy_id,
       bool_or(r.status = 'Open') AS any_open,
       bool_and(COALESCE((r.rate->>'is_corporate')::boolean, false)) AS is_corporate,
       max(r.rate->>'corporate_text') AS corporate_text
FROM row_policy rp JOIN rate_rows r ON r.item = rp.item
GROUP BY rp.policy_id;

-- ═══════════════════════════════════════════════════════════════════════════
-- The rate card
-- ═══════════════════════════════════════════════════════════════════════════
INSERT INTO variants (
    snapshot_id, policy_id, fund_code, brand_code, product_type,
    gov_product_item_id, gov_product_id, gov_product_code, gov_product_group_code, gov_funds_product_code,
    name, status, state, family_type, dependant_scope, dependant_titles,
    covers_adult_dependants, covers_disability_dependants, who_covered_json,
    premium_month, premium_hospital_component,
    excess_type, excess_per_admission, excess_per_person, excess_per_policy, excess_option, excess_waivers_json,
    copayment_type, copayment_shared, copayment_shared_max, copayment_private, copayment_private_max,
    copayment_day_surgery, copayment_annual_max,
    ambulance_cover, ambulance_use_fund, bundle_requirement_json,
    raw, is_corporate, corporate_text, mls_exempt, valid_from, valid_to)
SELECT :snapshot_id, rp.policy_id, r.fund_code, r.brand_code, r.product_type,
       r.item, r.gov_product_id, r.gov_product_code, r.rate->>'gov_product_group_code', r.rate->>'gov_funds_product_code',
       r.name, r.status, r.state, r.rate->>'family_type', r.rate->>'dependant_scope',
       ARRAY(SELECT jsonb_array_elements_text(r.rate->'dependant_titles')),
       (r.rate->>'covers_adult_dependants')::boolean, (r.rate->>'covers_disability_dependants')::boolean,
       r.rate->'who_covered_json',
       r.premium, (r.rate->>'premium_hospital_component')::numeric,
       r.rate->'excess'->>'type', (r.rate->'excess'->>'per_admission')::numeric,
       (r.rate->'excess'->>'per_person')::numeric, (r.rate->'excess'->>'per_policy')::numeric,
       (r.rate->>'excess_option')::numeric,
       CASE WHEN jsonb_array_length(r.rate->'excess'->'waivers') > 0 THEN r.rate->'excess'->'waivers' END,
       r.rate->'copayment'->>'type', (r.rate->'copayment'->>'shared')::numeric,
       (r.rate->'copayment'->>'shared_max')::numeric, (r.rate->'copayment'->>'private')::numeric,
       (r.rate->'copayment'->>'private_max')::numeric, (r.rate->'copayment'->>'day_surgery')::numeric,
       (r.rate->'copayment'->>'annual_max')::numeric,
       r.rate->>'ambulance_cover', (r.rate->>'ambulance_use_fund')::boolean,
       r.rate->'only_available_with',
       r.rate, COALESCE((r.rate->>'is_corporate')::boolean, false), r.rate->>'corporate_text',
       (r.rate->>'mls_exempt')::boolean, (r.rate->>'valid_from')::date, (r.rate->>'valid_to')::date
FROM rate_rows r JOIN row_policy rp ON rp.item = r.item;

SELECT pg_temp.gate((SELECT count(*) FROM variants WHERE snapshot_id = :snapshot_id) = :expected_products,
                    'variants rows do not match the products staged');

-- ═══════════════════════════════════════════════════════════════════════════
-- Product definitions, keyed by policy and state, and for extras by household.
--
-- A definition is the part of a product that is the same across its rates
-- (cover, waits, tier, services, benefits). Where a policy's rows agree, the
-- definition is written once under ALL; a state, or for extras a household,
-- that differs gets its own rows, and a state that agrees with the rest gets
-- none. The resolvers fall back to ALL, so no row is written twice.
--
-- Display text (the features paragraph, the preferred-provider note, the
-- special-features flag) is not part of a definition. Text is the most common
-- value among the rows it applies to, and a special-features flag is set if
-- any of those rows sets it, so one row's typo does not change what a policy is.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE def_rows AS
SELECT rp.policy_id, d.value->>'state' AS state, r.rate->>'family_type' AS family,
       d.value->>'hospital' AS hospital_id, d.value->>'general' AS general_id,
       d.value->'texts'->>'other_features' AS other_features,
       d.value->'texts'->>'other_services' AS other_services,
       d.value->'texts'->>'preferred_free' AS preferred_free,
       d.value->'texts'->'special' AS special
FROM row_policy rp
JOIN rate_rows r ON r.item = rp.item
JOIN stg_products p ON p.gov_product_item_id = rp.item
CROSS JOIN LATERAL jsonb_array_elements(p.raw->'definitions') d(value);

-- Hospital side ----------------------------------------------------------------
CREATE TEMP TABLE hosp_defs AS
SELECT DISTINCT dr.policy_id, dr.state, dr.hospital_id AS fp, sd.body AS hospital
FROM def_rows dr JOIN stg_definitions sd ON sd.def_id = dr.hospital_id AND sd.kind = 'hospital'
WHERE dr.hospital_id IS NOT NULL;

SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM hosp_defs GROUP BY policy_id, state HAVING count(*) > 1),
                    'a policy has two different hospital definitions for one state');

CREATE TEMP TABLE hosp_final AS
SELECT DISTINCT ON (h.policy_id, CASE WHEN pc.n = 1 THEN 'ALL' ELSE h.state END)
       h.policy_id, CASE WHEN pc.n = 1 THEN 'ALL' ELSE h.state END AS state, h.hospital
FROM hosp_defs h
JOIN (SELECT policy_id, count(DISTINCT fp) AS n FROM hosp_defs GROUP BY policy_id) pc USING (policy_id);

-- Every definition row, once for each scope it can apply to: its own state
-- and household, then ALL for either or both. Each lookup below is then a
-- single aggregate joined on the scope key, not a scan per output row.
CREATE TEMP TABLE scoped AS
SELECT dr.*, dr.state AS state_key, dr.family AS family_key FROM def_rows dr
UNION ALL
SELECT dr.*, 'ALL', dr.family FROM def_rows dr WHERE dr.state <> 'ALL'
UNION ALL
SELECT dr.*, dr.state, 'ALL' FROM def_rows dr WHERE dr.family <> 'ALL'
UNION ALL
SELECT dr.*, 'ALL', 'ALL' FROM def_rows dr WHERE dr.state <> 'ALL' AND dr.family <> 'ALL';
CREATE INDEX ON scoped (policy_id, state_key, family_key);

CREATE TEMP TABLE hosp_text AS
SELECT policy_id, state_key AS state,
       mode() WITHIN GROUP (ORDER BY other_features) AS other_features
FROM scoped
WHERE family_key = 'ALL' AND hospital_id IS NOT NULL AND other_features IS NOT NULL
GROUP BY policy_id, state_key;

INSERT INTO hospital_profiles (
    policy_id, snapshot_id, state, hospital_tier, tier_rank, accommodation,
    wait_pre_existing_months, wait_other_months, wait_sub_acute_months, wait_pregnancy_months,
    age_discount_available, age_discount_for_transferee, known_gap_cover, hospital_percent,
    other_features_text, limit_hospital_days)
SELECT h.policy_id, :snapshot_id, h.state, h.hospital->>'tier', (h.hospital->>'tier_rank')::numeric,
       h.hospital->>'accommodation',
       (h.hospital->'waits'->>'PreExisting')::smallint, (h.hospital->'waits'->>'Other')::smallint,
       (h.hospital->'waits'->>'SubAcute')::smallint, (h.hospital->'waits'->>'PregnancyBirth')::smallint,
       (h.hospital->>'age_discount_available')::boolean, (h.hospital->>'age_discount_for_transferee')::boolean,
       (h.hospital->>'known_gap_cover')::boolean, (h.hospital->>'hospital_percent')::numeric,
       t.other_features, (h.hospital->>'limit_hospital_days')::smallint
FROM hosp_final h JOIN hosp_text t ON t.policy_id = h.policy_id AND t.state = h.state;

INSERT INTO hospital_cover (policy_id, snapshot_id, state, category, cover)
SELECT h.policy_id, :snapshot_id, h.state, c.key, c.value
FROM hosp_final h CROSS JOIN LATERAL jsonb_each_text(h.hospital->'cover') c;

-- Extras side: by state, then by household ---------------------------------------
CREATE TEMP TABLE gen_defs AS
SELECT DISTINCT dr.policy_id, dr.state, dr.family, dr.general_id AS fp, sd.body AS general
FROM def_rows dr JOIN stg_definitions sd ON sd.def_id = dr.general_id AND sd.kind = 'general'
WHERE dr.general_id IS NOT NULL;

SELECT pg_temp.gate(NOT EXISTS (SELECT 1 FROM gen_defs GROUP BY policy_id, state, family HAVING count(*) > 1),
                    'a policy has two different extras definitions for one state and household');

-- A state where every household agrees is written once for all households...
CREATE TEMP TABLE gen_by_family AS
SELECT DISTINCT ON (g.policy_id, g.state, CASE WHEN pf.n = 1 THEN 'ALL' ELSE g.family END)
       g.policy_id, g.state, CASE WHEN pf.n = 1 THEN 'ALL' ELSE g.family END AS family, g.fp, g.general
FROM gen_defs g
JOIN (SELECT policy_id, state, count(DISTINCT fp) AS n FROM gen_defs GROUP BY policy_id, state) pf
  USING (policy_id, state);

-- ...and a policy where every state agrees is written once for all states.
CREATE TEMP TABLE gen_final AS
SELECT DISTINCT ON (g.policy_id,
                    CASE WHEN ps.n = 1 THEN 'ALL' ELSE g.state END,
                    CASE WHEN ps.n = 1 THEN 'ALL' ELSE g.family END)
       g.policy_id,
       CASE WHEN ps.n = 1 THEN 'ALL' ELSE g.state END AS state,
       CASE WHEN ps.n = 1 THEN 'ALL' ELSE g.family END AS family,
       g.general
FROM gen_by_family g
JOIN (SELECT policy_id, count(DISTINCT fp) AS n FROM gen_by_family GROUP BY policy_id) ps USING (policy_id);

-- The display text for each extras row: the most common among the rows it covers.
CREATE TEMP TABLE special_any AS
SELECT DISTINCT policy_id, state_key, family_key, sv.value AS service
FROM scoped CROSS JOIN LATERAL jsonb_array_elements_text(special) sv(value)
WHERE special IS NOT NULL AND general_id IS NOT NULL;
CREATE INDEX ON special_any (policy_id, state_key, family_key, service);

CREATE TEMP TABLE gen_text AS
SELECT g.policy_id, g.state, g.family,
       t.other_services, t.preferred_free
FROM gen_final g
LEFT JOIN (SELECT policy_id, state_key, family_key,
                  mode() WITHIN GROUP (ORDER BY other_services) AS other_services,
                  mode() WITHIN GROUP (ORDER BY preferred_free) AS preferred_free
           FROM scoped
           WHERE general_id IS NOT NULL
           GROUP BY policy_id, state_key, family_key) t
  ON t.policy_id = g.policy_id AND t.state_key = g.state AND t.family_key = g.family;

INSERT INTO extras_profiles (policy_id, snapshot_id, state, family_type, optional_services, max_services_covered,
                             preferred_provider, preferred_provider_text, other_services_text)
SELECT g.policy_id, :snapshot_id, g.state, g.family, (g.general->>'optional_services')::boolean,
       (g.general->>'max_services_covered')::smallint,
       g.general->'preferred_provider'->>'covered', t.preferred_free, t.other_services
FROM gen_final g
JOIN gen_text t ON t.policy_id = g.policy_id AND t.state = g.state AND t.family = g.family;

INSERT INTO extras_services (policy_id, snapshot_id, state, family_type, service, covered, wait_months,
                             has_special_features)
SELECT g.policy_id, :snapshot_id, g.state, g.family, s.key, (s.value->>'covered')::boolean,
       (s.value->>'wait_months')::smallint,
       EXISTS (SELECT 1 FROM special_any sa
                WHERE sa.policy_id = g.policy_id AND sa.state_key = g.state
                  AND sa.family_key = g.family AND sa.service = s.key)
FROM gen_final g CROSS JOIN LATERAL jsonb_each(g.general->'services') s;

INSERT INTO extras_benefits (policy_id, snapshot_id, state, family_type, service, item, benefit_amount, benefit_pct)
SELECT g.policy_id, :snapshot_id, g.state, g.family, b->>'service', b->>'item',
       (b->>'amount')::numeric, (b->>'pct')::numeric
FROM gen_final g CROSS JOIN LATERAL jsonb_array_elements(g.general->'benefits') b;

-- ═══════════════════════════════════════════════════════════════════════════
-- Limit groups: household-scoped, shared across states. A group that two
-- rates of one household describe differently is a finding, not a merge.
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE lg_rows AS
SELECT rp.policy_id, r.state, r.rate->>'family_type' AS family_type, r.rate->>'dependant_scope' AS dependant_scope,
       lg.body AS lg, lg.lg_id
FROM row_policy rp
JOIN rate_rows r ON r.item = rp.item
JOIN stg_products p ON p.gov_product_item_id = rp.item
CROSS JOIN LATERAL jsonb_array_elements_text(p.raw->'limit_groups') ref(value)
JOIN stg_limit_groups lg ON lg.lg_id = ref.value;

SELECT pg_temp.gate(NOT EXISTS (
           SELECT 1 FROM lg_rows GROUP BY policy_id, state, family_type, dependant_scope, lg->>'title'
           HAVING count(DISTINCT lg_id) > 1),
       'a household in one state has two different limit groups under one title');

-- A limit that is the same in every state is written once for ALL; otherwise
-- each state keeps its own, as the resolver expects.
CREATE TEMP TABLE lg_base AS
SELECT DISTINCT policy_id, state, family_type, dependant_scope, lg->>'title' AS title, lg_id, lg
FROM lg_rows;

CREATE TEMP TABLE lg_one AS
SELECT DISTINCT ON (b.policy_id, CASE WHEN n.c = 1 THEN 'ALL' ELSE b.state END,
                    b.family_type, b.dependant_scope, b.title)
       b.policy_id, CASE WHEN n.c = 1 THEN 'ALL' ELSE b.state END AS state,
       b.family_type, b.dependant_scope, b.title, b.lg
FROM lg_base b
JOIN (SELECT policy_id, family_type, dependant_scope, title, count(DISTINCT lg_id) AS c
        FROM lg_base GROUP BY 1, 2, 3, 4) n
  USING (policy_id, family_type, dependant_scope, title);

INSERT INTO extras_limit_groups (policy_id, snapshot_id, state, family_type, dependant_scope, title,
                                 no_benefit_limits, limit_per_person, limit_per_service, limit_per_policy,
                                 lifetime_limit, service_count, service_count_years, service_count_type,
                                 free_text_limit, annual_no_limit)
SELECT policy_id, :snapshot_id, state, family_type, dependant_scope, title,
       (lg->>'no_benefit_limits')::boolean, (lg->>'limit_per_person')::numeric,
       (lg->>'limit_per_service')::numeric, (lg->>'limit_per_policy')::numeric,
       (lg->>'lifetime_limit')::numeric, (lg->>'service_count')::smallint,
       (lg->>'service_count_years')::smallint, lg->>'service_count_type',
       lg->>'free_text_limit', (lg->>'annual_no_limit')::boolean
FROM lg_one;

INSERT INTO extras_limit_group_services (group_id, service, sub_limits_apply, individual_lifetime_limit)
SELECT g.id, s->>'service', (s->>'sub_limits_apply')::boolean, (s->>'ind_lifetime_limit')::numeric
FROM extras_limit_groups g
JOIN lg_one l ON l.policy_id = g.policy_id AND l.state = g.state AND l.family_type = g.family_type
             AND l.dependant_scope = g.dependant_scope AND l.title = g.title
CROSS JOIN LATERAL jsonb_array_elements(l.lg->'services') s
WHERE g.snapshot_id = :snapshot_id;

-- ═══════════════════════════════════════════════════════════════════════════
-- What changed since the previous live snapshot. Read before the policies
-- take their new names and statuses below, because the rename and closure
-- checks compare against the old values. Empty on a first load except for
-- the new policies.
-- ═══════════════════════════════════════════════════════════════════════════
INSERT INTO changes (snapshot_id, policy_id, field, old_value, new_value, kind)
SELECT :snapshot_id, gp.policy_id, 'current_name', p.current_name, gp.name, 'rename'
FROM group_policy gp JOIN policies p ON p.id = gp.policy_id
WHERE gp.policy_id IS NOT NULL AND NOT gp.created AND p.current_name <> gp.name;

INSERT INTO changes (snapshot_id, policy_id, field, new_value, kind)
SELECT :snapshot_id, policy_id, 'policy', name, 'new' FROM group_policy WHERE created;

INSERT INTO changes (snapshot_id, policy_id, field, old_value, kind)
SELECT :snapshot_id, p.id, 'policy', p.current_name, 'closed'
FROM policies p
WHERE p.id IN (SELECT policy_id FROM variants WHERE snapshot_id = :prev_snapshot_id)
  AND p.id NOT IN (SELECT policy_id FROM row_policy);

INSERT INTO changes (snapshot_id, policy_id, state, family_type, dependant_scope, field, old_value, new_value, kind)
SELECT :snapshot_id, c.policy_id, c.state, c.family_type, c.dependant_scope,
       'premium_month', p.premium_month::text, c.premium_month::text, 'premium'
FROM variants c
JOIN variants p ON p.gov_product_item_id = c.gov_product_item_id AND p.snapshot_id = :prev_snapshot_id
WHERE c.snapshot_id = :snapshot_id AND p.premium_month <> c.premium_month;

INSERT INTO changes (snapshot_id, policy_id, state, family_type, dependant_scope, field, old_value, new_value, kind)
SELECT :snapshot_id, c.policy_id, c.state, c.family_type, c.dependant_scope,
       'excess', p.excess_type || ' ' || p.excess_option, c.excess_type || ' ' || c.excess_option, 'excess'
FROM variants c
JOIN variants p ON p.gov_product_item_id = c.gov_product_item_id AND p.snapshot_id = :prev_snapshot_id
WHERE c.snapshot_id = :snapshot_id
  AND (p.excess_type IS DISTINCT FROM c.excess_type OR p.excess_option IS DISTINCT FROM c.excess_option);

INSERT INTO changes (snapshot_id, policy_id, state, family_type, dependant_scope, field, old_value, new_value, kind)
SELECT :snapshot_id, p.policy_id, p.state, p.family_type, p.dependant_scope, 'rate', 'present', NULL, 'closed'
FROM variants p
WHERE p.snapshot_id = :prev_snapshot_id
  AND p.gov_product_item_id NOT IN (SELECT item FROM rate_rows);

INSERT INTO changes (snapshot_id, policy_id, state, family_type, dependant_scope, field, old_value, new_value, kind)
SELECT :snapshot_id, c.policy_id, c.state, c.family_type, c.dependant_scope, 'rate', NULL, 'present', 'new'
FROM variants c
WHERE c.snapshot_id = :snapshot_id
  AND :prev_snapshot_id <> 0
  AND c.gov_product_item_id NOT IN (SELECT gov_product_item_id FROM variants WHERE snapshot_id = :prev_snapshot_id);

INSERT INTO changes (snapshot_id, policy_id, state, field, old_value, new_value, kind)
SELECT :snapshot_id, COALESCE(c.policy_id, p.policy_id), COALESCE(c.state, p.state), COALESCE(c.category, p.category),
       p.cover, c.cover, 'cover'
FROM (SELECT * FROM hospital_cover WHERE snapshot_id = :snapshot_id) c
FULL JOIN (SELECT * FROM hospital_cover WHERE snapshot_id = :prev_snapshot_id) p
       ON p.policy_id = c.policy_id AND p.state = c.state AND p.category = c.category
WHERE :prev_snapshot_id <> 0 AND c.cover IS DISTINCT FROM p.cover;

INSERT INTO changes (snapshot_id, policy_id, state, field, old_value, new_value, kind)
SELECT :snapshot_id, COALESCE(c.policy_id, p.policy_id), COALESCE(c.state, p.state),
       COALESCE(c.service, p.service) || '/' || COALESCE(c.item, p.item),
       p.benefit_amount::text, c.benefit_amount::text, 'benefit'
FROM (SELECT * FROM extras_benefits WHERE snapshot_id = :snapshot_id) c
FULL JOIN (SELECT * FROM extras_benefits WHERE snapshot_id = :prev_snapshot_id) p
       ON p.policy_id = c.policy_id AND p.state = c.state AND p.family_type = c.family_type
      AND p.service = c.service AND p.item = c.item
WHERE :prev_snapshot_id <> 0
  AND (c.benefit_amount IS DISTINCT FROM p.benefit_amount OR c.benefit_pct IS DISTINCT FROM p.benefit_pct);

INSERT INTO changes (snapshot_id, policy_id, family_type, dependant_scope, field, old_value, new_value, kind)
SELECT :snapshot_id, COALESCE(c.policy_id, p.policy_id), COALESCE(c.family_type, p.family_type),
       COALESCE(c.dependant_scope, p.dependant_scope), COALESCE(c.title, p.title),
       p.limit_per_person::text || '/' || p.limit_per_policy::text, c.limit_per_person::text || '/' || c.limit_per_policy::text,
       'limit'
FROM (SELECT * FROM extras_limit_groups WHERE snapshot_id = :snapshot_id) c
FULL JOIN (SELECT * FROM extras_limit_groups WHERE snapshot_id = :prev_snapshot_id) p
       ON p.policy_id = c.policy_id AND p.state = c.state AND p.family_type = c.family_type
      AND p.dependant_scope = c.dependant_scope AND p.title = c.title
WHERE :prev_snapshot_id <> 0
  AND (c.limit_per_person, c.limit_per_service, c.limit_per_policy, c.lifetime_limit, c.service_count, c.free_text_limit)
      IS DISTINCT FROM
      (p.limit_per_person, p.limit_per_service, p.limit_per_policy, p.lifetime_limit, p.service_count, p.free_text_limit);

-- ═══════════════════════════════════════════════════════════════════════════
-- The policies take this month's facts; policies that left the feed close.
-- ═══════════════════════════════════════════════════════════════════════════
UPDATE policies p
SET current_name = gp.name,
    status = CASE WHEN f.any_open THEN 'confirmed' ELSE 'closed' END,
    is_corporate = f.is_corporate,
    corporate_text = f.corporate_text,
    last_seen = :'release_date'
FROM policy_facts f
JOIN group_policy gp ON gp.policy_id = f.policy_id
WHERE p.id = f.policy_id;

UPDATE policies SET status = 'closed'
WHERE id IN (SELECT policy_id FROM variants WHERE snapshot_id = :prev_snapshot_id)
  AND id NOT IN (SELECT policy_id FROM row_policy);

UPDATE snapshots SET status = 'superseded' WHERE status = 'live' AND id <> :snapshot_id;
UPDATE snapshots SET status = 'live', promoted_at = now() WHERE id = :snapshot_id;
