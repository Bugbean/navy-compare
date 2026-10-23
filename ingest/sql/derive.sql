-- The derived comparison layer for :snapshot_id, rebuilt on every release.
--
-- policy_comparisons: one row per (Navy policy, competitor policy, state,
--   household), scored on cover. Hospital cover is compared by state: the
--   38 clinical categories (Covered 1, Restricted 0.5, NotCovered 0;
--   similarity = 1 - the mean gap). Extras are compared by state and
--   household: the 27 services covered or not. A combined pair averages its
--   two sides. A row is written only where a side has its own definition;
--   the rest resolve to ALL, as the resolvers expect.
--
-- closest_matches: for each competitor rate (state, household), the three
--   Navy policies with a rate in that state and household that score best,
--   ties broken by the smaller premium gap between the two cheapest rates.
--   A nationally priced competitor rate is matched state by state.
--
-- Scores are cover similarity only. Premium is shown beside them, never
-- folded into the score, so a rep can see both. How a rep would weigh cover
-- against price is not decided here; see docs/DATA_MODEL.md.

-- ═══════════════════════════════════════════════════════════════════════════
-- Which policies take part
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE navy_pol AS
SELECT p.id, p.product_type FROM policies p
WHERE p.is_navy AND p.id IN (SELECT policy_id FROM variants WHERE snapshot_id = :snapshot_id AND status = 'Open');

CREATE TEMP TABLE comp_pol AS
SELECT p.id, p.product_type FROM policies p
WHERE NOT p.is_navy AND p.id IN (SELECT policy_id FROM variants WHERE snapshot_id = :snapshot_id AND status = 'Open');

CREATE TEMP TABLE pairs AS
SELECT n.id AS navy_id, c.id AS comp_id, n.product_type
FROM navy_pol n JOIN comp_pol c ON c.product_type = n.product_type;

-- Where a policy has its own definition: a state (hospital), or a state and
-- household (extras). Each pair is compared at ALL, and again at every such
-- key either side has.
CREATE TEMP TABLE profile_keys AS
SELECT DISTINCT policy_id, state, 'ALL'::text AS family_type FROM hospital_profiles
 WHERE snapshot_id = :snapshot_id AND state <> 'ALL'
UNION
SELECT DISTINCT policy_id, state, family_type FROM extras_profiles
 WHERE snapshot_id = :snapshot_id AND NOT (state = 'ALL' AND family_type = 'ALL');

CREATE TEMP TABLE pair_states AS
SELECT pr.navy_id, pr.comp_id, 'ALL'::text AS state, 'ALL'::text AS family_type, pr.product_type FROM pairs pr
UNION
SELECT pr.navy_id, pr.comp_id, k.state, k.family_type, pr.product_type
FROM pairs pr JOIN profile_keys k ON k.policy_id = pr.navy_id
UNION
SELECT pr.navy_id, pr.comp_id, k.state, k.family_type, pr.product_type
FROM pairs pr JOIN profile_keys k ON k.policy_id = pr.comp_id;

-- ═══════════════════════════════════════════════════════════════════════════
-- Hospital pairs: cover, tier, accommodation and waits, by state
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE hospital_states AS
SELECT DISTINCT navy_id, comp_id, state FROM pair_states WHERE product_type IN ('hospital', 'combined');

CREATE TEMP TABLE cover_pair AS
SELECT hs.navy_id, hs.comp_id, hs.state,
       1 - (sum(abs(CASE n.cover WHEN 'Covered' THEN 1 WHEN 'Restricted' THEN 0.5 ELSE 0 END
                  - CASE c.cover WHEN 'Covered' THEN 1 WHEN 'Restricted' THEN 0.5 ELSE 0 END))
            / 38.0) AS score,
       jsonb_agg(jsonb_build_object('category', n.category, 'navy', n.cover, 'comp', c.cover))
           FILTER (WHERE n.cover IS DISTINCT FROM c.cover) AS cover_diffs
FROM hospital_states hs
JOIN LATERAL hospital_cover_for(hs.navy_id, :snapshot_id, hs.state) n ON true
JOIN LATERAL hospital_cover_for(hs.comp_id, :snapshot_id, hs.state) c ON c.category = n.category
GROUP BY hs.navy_id, hs.comp_id, hs.state;

CREATE TEMP TABLE hospital_pair AS
SELECT hs.navy_id, hs.comp_id, hs.state,
       jsonb_strip_nulls(jsonb_build_object(
           'tier', CASE WHEN n.hospital_tier IS DISTINCT FROM c.hospital_tier
                        THEN jsonb_build_object('navy', n.hospital_tier, 'comp', c.hospital_tier) END,
           'accommodation', CASE WHEN n.accommodation IS DISTINCT FROM c.accommodation
                                 THEN jsonb_build_object('navy', n.accommodation, 'comp', c.accommodation) END,
           'waits', CASE WHEN (n.wait_pre_existing_months, n.wait_other_months, n.wait_sub_acute_months, n.wait_pregnancy_months)
                             IS DISTINCT FROM (c.wait_pre_existing_months, c.wait_other_months, c.wait_sub_acute_months, c.wait_pregnancy_months)
                         THEN jsonb_build_object('navy', jsonb_build_object('pre_existing', n.wait_pre_existing_months,
                                                                            'other', n.wait_other_months,
                                                                            'sub_acute', n.wait_sub_acute_months,
                                                                            'pregnancy', n.wait_pregnancy_months),
                                                 'comp', jsonb_build_object('pre_existing', c.wait_pre_existing_months,
                                                                            'other', c.wait_other_months,
                                                                            'sub_acute', c.wait_sub_acute_months,
                                                                            'pregnancy', c.wait_pregnancy_months)) END
       )) AS differences
FROM hospital_states hs
JOIN LATERAL hospital_profile_for(hs.navy_id, :snapshot_id, hs.state) n ON true
JOIN LATERAL hospital_profile_for(hs.comp_id, :snapshot_id, hs.state) c ON true;

-- ═══════════════════════════════════════════════════════════════════════════
-- Extras pairs: services covered or not, by state and household
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TEMP TABLE extras_pair AS
SELECT ps.navy_id, ps.comp_id, ps.state, ps.family_type,
       1 - (count(*) FILTER (WHERE n.covered IS DISTINCT FROM c.covered)::numeric / 27.0) AS score,
       jsonb_agg(jsonb_build_object('service', n.service, 'navy', n.covered, 'comp', c.covered))
           FILTER (WHERE n.covered IS DISTINCT FROM c.covered) AS service_diffs
FROM pair_states ps
JOIN LATERAL extras_services_for(ps.navy_id, :snapshot_id, ps.state, ps.family_type) n ON true
JOIN LATERAL extras_services_for(ps.comp_id, :snapshot_id, ps.state, ps.family_type) c ON c.service = n.service
WHERE ps.product_type IN ('extras', 'combined')
GROUP BY ps.navy_id, ps.comp_id, ps.state, ps.family_type;

-- ═══════════════════════════════════════════════════════════════════════════
-- The pair rows
-- ═══════════════════════════════════════════════════════════════════════════
INSERT INTO policy_comparisons (snapshot_id, navy_policy_id, comp_policy_id, state, family_type, score, differences)
SELECT :snapshot_id, ps.navy_id, ps.comp_id, ps.state, ps.family_type,
       round(CASE WHEN cp.score IS NOT NULL AND ep.score IS NOT NULL THEN (cp.score + ep.score) / 2
                  WHEN cp.score IS NOT NULL THEN cp.score
                  ELSE ep.score END, 3),
       jsonb_strip_nulls(jsonb_build_object(
           'hospital', CASE WHEN cp.score IS NOT NULL THEN
                          jsonb_build_object('cover_diffs', COALESCE(cp.cover_diffs, '[]'::jsonb),
                                             'profile', COALESCE(hp.differences, '{}'::jsonb)) END,
           'extras', CASE WHEN ep.score IS NOT NULL THEN
                        jsonb_build_object('service_diffs', COALESCE(ep.service_diffs, '[]'::jsonb)) END))
FROM pair_states ps
LEFT JOIN cover_pair cp ON cp.navy_id = ps.navy_id AND cp.comp_id = ps.comp_id AND cp.state = ps.state
LEFT JOIN hospital_pair hp ON hp.navy_id = ps.navy_id AND hp.comp_id = ps.comp_id AND hp.state = ps.state
LEFT JOIN extras_pair ep ON ep.navy_id = ps.navy_id AND ep.comp_id = ps.comp_id
                        AND ep.state = ps.state AND ep.family_type = ps.family_type
WHERE cp.score IS NOT NULL OR ep.score IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════════════════
-- Closest matches, per competitor rate
-- ═══════════════════════════════════════════════════════════════════════════
-- A competitor rate priced nationally is matched in each state.
CREATE TEMP TABLE comp_rate AS
SELECT v.policy_id AS comp_id, st.state, v.family_type, v.dependant_scope, min(v.premium_month) AS premium
FROM variants v
JOIN comp_pol c ON c.id = v.policy_id
CROSS JOIN LATERAL (SELECT unnest(CASE WHEN v.state = 'ALL'
                                       THEN ARRAY['NSW', 'VIC', 'QLD', 'SA', 'WA', 'TAS', 'NT', 'ACT']
                                       ELSE ARRAY[v.state] END) AS state) st
WHERE v.snapshot_id = :snapshot_id AND v.status = 'Open'
GROUP BY 1, 2, 3, 4;

CREATE TEMP TABLE navy_rate AS
SELECT v.policy_id AS navy_id, v.state, v.family_type, v.dependant_scope, min(v.premium_month) AS premium
FROM variants v
JOIN navy_pol n ON n.id = v.policy_id
WHERE v.snapshot_id = :snapshot_id AND v.status = 'Open'
GROUP BY 1, 2, 3, 4;

-- The cover score for the pair at this state and household: the most specific
-- comparison row, by the same fallback the resolvers use.
CREATE TEMP TABLE match_candidates AS
SELECT DISTINCT ON (cr.comp_id, cr.state, cr.family_type, cr.dependant_scope, pr.navy_id)
       cr.comp_id, cr.state, cr.family_type, cr.dependant_scope, pr.navy_id,
       sc.score,
       abs(nr.premium - cr.premium) AS premium_gap
FROM comp_rate cr
JOIN pairs pr ON pr.comp_id = cr.comp_id
JOIN navy_rate nr ON nr.navy_id = pr.navy_id AND nr.family_type = cr.family_type
                 AND nr.dependant_scope = cr.dependant_scope AND nr.state IN (cr.state, 'ALL')
JOIN LATERAL (SELECT pc.score FROM policy_comparisons pc
               WHERE pc.snapshot_id = :snapshot_id AND pc.navy_policy_id = pr.navy_id
                 AND pc.comp_policy_id = cr.comp_id
                 AND pc.state IN (cr.state, 'ALL') AND pc.family_type IN (cr.family_type, 'ALL')
               ORDER BY (pc.state = cr.state) DESC, (pc.family_type = cr.family_type) DESC
               LIMIT 1) sc ON true
ORDER BY cr.comp_id, cr.state, cr.family_type, cr.dependant_scope, pr.navy_id, abs(nr.premium - cr.premium);

INSERT INTO closest_matches (snapshot_id, comp_policy_id, state, family_type, dependant_scope, rank, navy_policy_id, score)
SELECT :snapshot_id, comp_id, state, family_type, dependant_scope, rn, navy_id, score
FROM (SELECT m.*, row_number() OVER (PARTITION BY comp_id, state, family_type, dependant_scope
                                     ORDER BY score DESC, premium_gap, navy_id) AS rn
      FROM match_candidates m WHERE score IS NOT NULL) ranked
WHERE rn <= 3;
