-- The three things a service desk is asked about that this schema could not hold.
--
-- Found by asking what a customer would say next. 我下次繳費是什麼時候, 我這期繳了嗎,
-- 受益人是誰, 我的理賠辦到哪了 — four ordinary questions at an insurance counter, and the
-- desk had no row to read for any of them. `billing_summary` computed an annual total from
-- the rate card, which is what a policy costs, never what anyone paid.
--
-- Each table earns its place by a question, and nothing here is speculative structure:
-- a column exists because a scenario reads it.

-- 繳費 --------------------------------------------------------------------------------
--
-- Two facts, and they are different. `premium_mode` and `paid_through` are the state of
-- the contract — what it costs and how far it is paid — and belong on the policy. A
-- payment is an event with a date and an amount, and belongs in its own table, because a
-- customer asking 我這期繳了嗎 is asking about an event, and one asking 我下次什麼時候繳
-- is asking about the state.

ALTER TABLE policy ADD COLUMN IF NOT EXISTS premium_mode text NOT NULL DEFAULT 'annual'
    CHECK (premium_mode IN ('annual', 'semiannual', 'quarterly', 'monthly'));
COMMENT ON COLUMN policy.premium_mode IS 'How often a premium falls due. Decides the grace period the customer is actually in and the amount of one instalment.';

ALTER TABLE policy ADD COLUMN IF NOT EXISTS paid_through date;
COMMENT ON COLUMN policy.paid_through IS 'The date cover is paid up to. NULL means never paid, which for an in-force policy is a data fault rather than a state.';

CREATE TABLE IF NOT EXISTS premium_payment (
    payment_id   bigserial PRIMARY KEY,
    policy_id    bigint NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
    due_at       date NOT NULL,
    paid_at      date,
    amount       numeric(12, 2) NOT NULL,
    method       text NOT NULL DEFAULT 'transfer'
                 CHECK (method IN ('transfer', 'credit_card', 'counter', 'convenience_store')),
    created_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE premium_payment IS 'One instalment. `paid_at` NULL means it fell due and has not been paid, which is the row 寬限期 and 停效 are computed from.';
CREATE INDEX IF NOT EXISTS premium_payment_by_policy ON premium_payment (policy_id, due_at DESC);
CREATE INDEX IF NOT EXISTS premium_payment_unpaid ON premium_payment (policy_id) WHERE paid_at IS NULL;

-- 受益人 ------------------------------------------------------------------------------
--
-- `member.beneficiary_relation` is one code on the customer, and a beneficiary is not a
-- property of a person — it is a designation on a contract, there can be several, and they
-- have shares. 保險法 §110 to §113 are all about that designation, and the scenario built
-- on them was reading a single字 off the wrong table.

CREATE TABLE IF NOT EXISTS policy_beneficiary (
    beneficiary_id bigserial PRIMARY KEY,
    policy_id      bigint NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
    display_name   text NOT NULL,
    relation       text NOT NULL,
    share          integer NOT NULL DEFAULT 100 CHECK (share > 0 AND share <= 100),
    designated_at  date NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE policy_beneficiary IS 'Who receives what, per contract. No row means the policy names nobody, which is 保險法 §113 and makes the benefit part of the estate — a state the schema must be able to hold, so this table has no NOT NULL relationship to policy.';
CREATE INDEX IF NOT EXISTS policy_beneficiary_by_policy ON policy_beneficiary (policy_id);

-- 理賠 --------------------------------------------------------------------------------
--
-- The claim scenario helps assemble documents and then had nowhere to put them. 我的理賠
-- 辦到哪了 is the most-asked question after a claim is filed, and the desk could not answer
-- it at all.
--
-- `outcome` is deliberately nullable and deliberately not decided here. This desk does not
-- adjudicate; it reports what the assessor recorded. A claim with no outcome is one still
-- being assessed, which is the honest answer to most enquiries.

CREATE TABLE IF NOT EXISTS claim (
    claim_id     bigserial PRIMARY KEY,
    policy_id    bigint NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('hospital', 'surgery', 'accident', 'disability', 'death', 'specific_illness')),
    event_at     date NOT NULL,
    filed_at     date NOT NULL,
    stage        text NOT NULL DEFAULT 'received'
                 CHECK (stage IN ('received', 'documents_pending', 'assessing', 'decided')),
    outcome      text CHECK (outcome IN ('paid', 'partial', 'declined')),
    decided_at   date,
    paid_amount  numeric(12, 2),
    note         text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE claim IS 'A claim as the assessor recorded it. The desk reads this and never writes an outcome — 核保理賠人員 decide, and a counter that could set `outcome` would be a counter that could promise one.';
COMMENT ON COLUMN claim.outcome IS 'NULL while the claim is being assessed, which is most of them. Never inferred from `stage`.';
CREATE INDEX IF NOT EXISTS claim_by_policy ON claim (policy_id, filed_at DESC);
