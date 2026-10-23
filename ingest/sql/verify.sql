-- What a loaded snapshot holds, against the figures docs/DATA_MODEL.md
-- proved from the 1 Oct 2026 extract. Printed by the loader after every
-- release; a figure that moves is a finding to read, not a test to delete.
-- Run by hand with:  psql "$DATABASE_URL" -v snapshot_id=N -f ingest/sql/verify.sql

\echo '── snapshot'
SELECT id, release_date, status, promoted_at FROM snapshots WHERE id = :snapshot_id;

\echo '── rate card by product type and status'
SELECT product_type, status, count(*) AS rows FROM variants
WHERE snapshot_id = :snapshot_id GROUP BY 1, 2 ORDER BY 1, 2;

\echo '── Navy Health: expect 280 hospital, 280 extras, 875 combined open rows (DATA_MODEL)'
SELECT product_type, count(*) AS open_rows FROM variants
WHERE snapshot_id = :snapshot_id AND fund_code = 'NHB' AND status = 'Open'
GROUP BY 1 ORDER BY 1;

\echo '── open hospital rows, and named products: expect 31,864 rows and 1,251 products'
SELECT count(*) AS open_hospital_rows,
       count(DISTINCT (fund_code, COALESCE(brand_code, ''), name)) AS named_products
FROM variants WHERE snapshot_id = :snapshot_id AND product_type = 'hospital' AND status = 'Open';

\echo '── rate-key collisions in open hospital rows: expect 0'
SELECT count(*) AS colliding_rate_keys FROM (
    SELECT policy_id, state, family_type, dependant_scope, excess_type, excess_per_admission,
           excess_per_person, excess_per_policy, copayment_type, copayment_shared, copayment_private
    FROM variants WHERE snapshot_id = :snapshot_id AND product_type = 'hospital' AND status = 'Open'
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 HAVING count(*) > 1) x;

\echo '── household buckets, open hospital rows: expect five family types'
SELECT family_type, count(*) AS rows FROM variants
WHERE snapshot_id = :snapshot_id AND product_type = 'hospital' AND status = 'Open'
GROUP BY 1 ORDER BY 2 DESC;

\echo '── states: ALL is a real value'
SELECT state, count(*) AS rows FROM variants WHERE snapshot_id = :snapshot_id AND status = 'Open'
GROUP BY 1 ORDER BY 1;

\echo '── side tables (definitions are stored once per policy and state)'
SELECT 'hospital_profiles' AS table_name, count(*) AS rows FROM hospital_profiles WHERE snapshot_id = :snapshot_id
UNION ALL SELECT 'hospital_cover', count(*) FROM hospital_cover WHERE snapshot_id = :snapshot_id
UNION ALL SELECT 'hospital_cover states other than ALL', count(*) FROM hospital_cover WHERE snapshot_id = :snapshot_id AND state <> 'ALL'
UNION ALL SELECT 'extras_profiles', count(*) FROM extras_profiles WHERE snapshot_id = :snapshot_id
UNION ALL SELECT 'extras_services', count(*) FROM extras_services WHERE snapshot_id = :snapshot_id
UNION ALL SELECT 'extras_benefits', count(*) FROM extras_benefits WHERE snapshot_id = :snapshot_id
UNION ALL SELECT 'extras_limit_groups', count(*) FROM extras_limit_groups WHERE snapshot_id = :snapshot_id
UNION ALL SELECT 'extras_limit_group_services', count(*) FROM extras_limit_group_services
          WHERE group_id IN (SELECT id FROM extras_limit_groups WHERE snapshot_id = :snapshot_id)
UNION ALL SELECT 'hospital_agreements', count(*) FROM hospital_agreements
UNION ALL SELECT 'hospital_agreement_funds', count(*) FROM hospital_agreement_funds;

\echo '── policies: expect the ones the feed carries, closed where it no longer lists them'
SELECT product_type, is_navy, status, count(*) AS policies FROM policies
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

\echo '── policies with no rate card in this snapshot (should be none for live policies)'
SELECT count(*) AS live_policies_without_rates FROM policies p
WHERE p.status <> 'closed'
  AND NOT EXISTS (SELECT 1 FROM variants v WHERE v.policy_id = p.id AND v.snapshot_id = :snapshot_id);

\echo '── what changed against the previous snapshot'
SELECT kind, count(*) AS changes FROM changes WHERE snapshot_id = :snapshot_id GROUP BY 1 ORDER BY 1;

\echo '── comparison layer'
SELECT count(*) AS policy_comparisons FROM policy_comparisons WHERE snapshot_id = :snapshot_id;
SELECT count(*) AS closest_match_rows FROM closest_matches WHERE snapshot_id = :snapshot_id;

\echo '── grants to the public API roles in public: expect none'
SELECT count(*) AS leaks FROM (
    SELECT table_name FROM information_schema.role_table_grants
     WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated')
    UNION ALL
    SELECT routine_name FROM information_schema.role_routine_grants
     WHERE routine_schema = 'public' AND grantee IN ('anon', 'authenticated', 'PUBLIC')) x;
