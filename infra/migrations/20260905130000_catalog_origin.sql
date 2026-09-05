ALTER TABLE catalog_entry ADD COLUMN IF NOT EXISTS data_origin text NOT NULL DEFAULT 'unknown';
ALTER TABLE catalog_entry ADD COLUMN IF NOT EXISTS rate_unit_amount integer
    CHECK (rate_unit_amount > 0);

COMMENT ON COLUMN catalog_entry.data_origin IS 'Source of catalogue rates, units, eligibility and sale status. synthetic_demo is generated data, not insurer evidence; unknown is unverified.';
COMMENT ON COLUMN catalog_entry.rate_unit_amount IS 'Amount priced by unit_premium, in the catalogue unit. NULL means no validated numeric pricing basis; never infer it from unit_label.';

CREATE OR REPLACE VIEW sale_catalog AS
SELECT ce.* FROM catalog_entry ce JOIN product p USING (product_id)
WHERE p.document_kind = 'contract';
