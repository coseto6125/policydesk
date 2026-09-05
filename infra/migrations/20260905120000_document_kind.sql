ALTER TABLE product ADD COLUMN IF NOT EXISTS document_kind text NOT NULL DEFAULT 'unknown'
    CHECK (document_kind IN ('contract', 'brochure', 'unknown'));

COMMENT ON COLUMN product.document_kind IS 'Published source role. Brochure summaries are not complete contracts; unknown sources are not promoted implicitly.';

CREATE OR REPLACE VIEW contract_clause AS
SELECT c.* FROM clause c JOIN product p USING (product_id)
WHERE p.document_kind = 'contract';

CREATE OR REPLACE VIEW sale_catalog AS
SELECT ce.* FROM catalog_entry ce JOIN product p USING (product_id)
WHERE p.document_kind = 'contract';
