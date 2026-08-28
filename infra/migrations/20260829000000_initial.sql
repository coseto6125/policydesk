-- policydesk initial schema.
--
-- Three groups of tables, and the split is the design.
--
--   Corpus    — what the insurer published. Derived from PDFs, rebuildable, read-only
--               once written. product, clause, benefit, required_document.
--   Catalog   — what the contracts do not say. Issue-age bands, premium rates, rider
--               compatibility: measured absent from all 660 published contracts, which
--               reference 保險費率表 without containing it. This group is mock data and
--               is labelled as such in the column comments, so nobody later mistakes it
--               for something scraped.
--   Live      — members, their policies, cases, documents, authorisations, the audit
--               trail, and the LLM trace.
--
-- Two absences are deliberate and load-bearing. There is no commission column: when
-- asked how the recommendation avoids steering to the highest-paying product, the
-- answer is that the figure is not in the database. And no table stores a "qualifies"
-- or "should_be_rejected" flag: a refusal is computed at query time from dates,
-- occupation class and clause text, so a caseworker can read the reason off the same
-- fields the system used.

-- Chinese contract text has no spaces, so the default full-text tokeniser treats a
-- whole clause as one token. Trigram indexing is what makes a substring search over
-- 條款 work at all.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------- corpus

CREATE TABLE IF NOT EXISTS product (
    product_id   text PRIMARY KEY,
    doc_sha      text NOT NULL,
    insurer      text NOT NULL,
    name         text NOT NULL,
    line         text NOT NULL CHECK (line IN ('life','health','accident','annuity','investment','other')),
    attachment   text NOT NULL CHECK (attachment IN ('main','rider')),
    approval     text,
    pages        integer NOT NULL DEFAULT 0,
    source_url   text,
    fetched_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN product.approval IS '核准文號. Identifies the contract version; only 89 of 660 documents carry one the parser recognises.';
COMMENT ON COLUMN product.line IS '人身保險 class per 保險法 §13. "other" holds brochures and term sheets that carry no articles.';

CREATE TABLE IF NOT EXISTS clause (
    product_id   text NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    clause_id    text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('definition','grant','exclusion','carve_back','waiting','limit','endorsement','procedure')),
    heading      text NOT NULL,
    verbatim     text NOT NULL,
    page         integer NOT NULL,
    overrides    text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (product_id, clause_id)
);
COMMENT ON COLUMN clause.overrides IS 'Clause ids this one defeats. A carve-back lists the exclusion it reopens; an endorsement lists the printed clause it amends.';
COMMENT ON COLUMN clause.verbatim IS 'Copied from the source document, never generated. The evidence layer rebuilds citations from this column.';

CREATE INDEX IF NOT EXISTS clause_by_kind ON clause (kind);
CREATE INDEX IF NOT EXISTS clause_text_trgm ON clause USING gin (verbatim gin_trgm_ops);

CREATE TABLE IF NOT EXISTS benefit (
    product_id   text NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    name         text NOT NULL,
    trigger      text NOT NULL DEFAULT '',
    formula      text NOT NULL DEFAULT '',
    notes        text NOT NULL DEFAULT '',
    page         integer NOT NULL,
    PRIMARY KEY (product_id, name)
);
COMMENT ON COLUMN benefit.formula IS 'As printed: "手術給付倍數 ╳ 住院醫療保險金日額". Evaluated by the calculator tool, never by a model.';

CREATE TABLE IF NOT EXISTS required_document (
    product_id   text NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    benefit      text NOT NULL,
    document     text NOT NULL,
    condition    text NOT NULL DEFAULT '',
    page         integer NOT NULL,
    PRIMARY KEY (product_id, benefit, document)
);
COMMENT ON COLUMN required_document.condition IS 'The footnote hung off a benefit — "須列明手術或處置名稱及部位". This is what claimants actually get wrong, so it is checked separately from whether the document exists.';

-- ---------------------------------------------------------------- catalog (mock)

CREATE TABLE IF NOT EXISTS catalog_entry (
    product_id       text PRIMARY KEY REFERENCES product(product_id) ON DELETE CASCADE,
    issue_age_min    integer NOT NULL,
    issue_age_max    integer NOT NULL,
    max_occupation   integer NOT NULL CHECK (max_occupation BETWEEN 1 AND 6),
    unit_premium     numeric(10,2) NOT NULL,
    unit_label       text NOT NULL,
    requires_main    boolean NOT NULL DEFAULT false,
    on_sale          boolean NOT NULL DEFAULT true
);
COMMENT ON TABLE catalog_entry IS 'MOCK DATA. Issue-age bands, rates and rider compatibility are published in 保險費率表, which is not part of the public contract corpus — measured: all 660 contracts reference it, none contain it.';
COMMENT ON COLUMN catalog_entry.max_occupation IS 'Highest 職業等級 this product accepts. Class 7 (拒保) is never acceptable, so it is absent from the range.';
COMMENT ON COLUMN catalog_entry.unit_premium IS 'Annual premium per unit_label unit, before age and occupation loading.';

-- ---------------------------------------------------------------- live

CREATE TABLE IF NOT EXISTS member (
    member_id        bigserial PRIMARY KEY,
    display_name     text NOT NULL UNIQUE,
    national_id      text NOT NULL,
    sex              text NOT NULL CHECK (sex IN ('male','female')),
    birth_date       date NOT NULL,
    occupation       text NOT NULL,
    occupation_class integer NOT NULL CHECK (occupation_class BETWEEN 1 AND 7),
    address_city     text NOT NULL,
    address_district text NOT NULL,
    address_rest     text NOT NULL,
    phone            text NOT NULL,
    email            text NOT NULL,
    marital_status   text NOT NULL,
    income_band      text NOT NULL,
    medical_history  text[] NOT NULL DEFAULT '{}',
    beneficiary_relation text NOT NULL,
    profile_frozen_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN member.display_name IS 'The whole login. A second claim on a live name evicts the first.';
COMMENT ON COLUMN member.occupation_class IS '1-6 are insurable with loading; 7 is 拒保 and is stored, not hidden, so the refusal is computed from a readable fact.';
COMMENT ON COLUMN member.profile_frozen_at IS 'Age and sex are editable until the first message, then fixed. Null means still editable.';

CREATE TABLE IF NOT EXISTS policy (
    policy_id        bigserial PRIMARY KEY,
    member_id        bigint NOT NULL REFERENCES member(member_id) ON DELETE CASCADE,
    product_id       text NOT NULL REFERENCES product(product_id),
    policy_number    text NOT NULL UNIQUE,
    sum_insured      integer NOT NULL,
    effective_at     date NOT NULL,
    lapsed_at        date,
    main_policy_ref  text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN policy.sum_insured IS '保險金額 — the contractual ceiling. Remaining benefit is computed, never stored.';
COMMENT ON COLUMN policy.effective_at IS 'Against this a waiting period is a date comparison, so "not yet covered" is derived rather than flagged.';
COMMENT ON COLUMN policy.main_policy_ref IS 'For a rider, the main contract it hangs off. A ref that resolves to nothing is a data-integrity fault, and is reported as one rather than treated as cover.';

CREATE INDEX IF NOT EXISTS policy_by_member ON policy (member_id);

CREATE TABLE IF NOT EXISTS "case" (
    case_id          bigserial PRIMARY KEY,
    member_id        bigint NOT NULL REFERENCES member(member_id) ON DELETE CASCADE,
    kind             text NOT NULL CHECK (kind IN ('enrolment','claim','service')),
    stage            text NOT NULL CHECK (stage IN ('inquiry','proposed','issued','signed','verified','review','approved','rejected')),
    case_version     integer NOT NULL DEFAULT 1,
    adviser_name     text,
    adviser_licence  text,
    decided_by       text,
    decision_reason  text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE "case" IS 'One case, two renderings. The customer pane and the back office read this row; neither writes it directly — a single case command module does.';
COMMENT ON COLUMN "case".case_version IS 'Bumped on every accepted command. Both panes render the version they hold, so a stale pane is visible rather than silently wrong.';
COMMENT ON COLUMN "case".adviser_licence IS '登錄字號 of the adviser answering for the recommendation. 推介 is 招攬, and 招攬 requires a registered individual.';

CREATE TABLE IF NOT EXISTS case_document (
    document_id      bigserial PRIMARY KEY,
    case_id          bigint NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    kind             text NOT NULL,
    title            text NOT NULL,
    sha              text,
    signed_at        timestamptz,
    uploaded_name    text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN case_document.kind IS '要保書 / 商品說明書 / 健康告知書 / 個資告知同意書 / 受益人指定書 / 契約撤銷權告知 and the rest of the real signing set.';
COMMENT ON COLUMN case_document.sha IS 'Hash of the exact bytes issued. A signature binds to this, so amending a document after signing invalidates the signature rather than silently replacing it.';

CREATE TABLE IF NOT EXISTS authorization_grant (
    grant_id         bigserial PRIMARY KEY,
    case_id          bigint NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    stage            text NOT NULL,
    scope            text NOT NULL,
    signed_at        timestamptz NOT NULL DEFAULT now(),
    document_sha     text NOT NULL,
    provider         text NOT NULL DEFAULT 'mock',
    request_id       text
);
COMMENT ON TABLE authorization_grant IS 'One digital signature authorising one stage. Only a digital signature carries the statutory presumption of a personal signature (電子簽章法); a click-through does not, so a click never lands here.';
COMMENT ON COLUMN authorization_grant.scope IS 'What the agent may do under this grant. Anything outside it needs a new grant.';
COMMENT ON COLUMN authorization_grant.provider IS 'mock or the real identity provider. The rest of the system does not branch on this; only the audit view shows it.';

CREATE TABLE IF NOT EXISTS identity_check (
    check_id         bigserial PRIMARY KEY,
    case_id          bigint NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    national_id      text NOT NULL,
    verified         boolean NOT NULL,
    reason           text NOT NULL DEFAULT '',
    provider         text NOT NULL DEFAULT 'mock',
    latency_ms       integer,
    request_id       text,
    checked_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE identity_check IS 'Every attempt, including failures and non-responses. A verification path whose refusals leave no record cannot be audited, and a mock that never refuses is a stub.';

CREATE TABLE IF NOT EXISTS conversation_message (
    message_id       bigserial PRIMARY KEY,
    case_id          bigint NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    speaker          text NOT NULL CHECK (speaker IN ('customer','agent','system')),
    text             text NOT NULL,
    turn_id          text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS message_by_case ON conversation_message (case_id, message_id);

CREATE TABLE IF NOT EXISTS audit_event (
    event_id         bigserial PRIMARY KEY,
    case_id          bigint REFERENCES "case"(case_id) ON DELETE CASCADE,
    actor            text NOT NULL,
    action           text NOT NULL,
    detail           jsonb NOT NULL DEFAULT '{}',
    case_version     integer,
    grant_id         bigint REFERENCES authorization_grant(grant_id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE audit_event IS 'The trail the FSC 2026-05 agentic-AI guidance requires. Answers "who, under which authorisation, moved this case, and when".';
COMMENT ON COLUMN audit_event.actor IS 'A person, the agent, or a named external provider. "the system" is not an actor.';

CREATE INDEX IF NOT EXISTS audit_by_case ON audit_event (case_id, event_id);

-- ---------------------------------------------------------------- llm trace

CREATE TABLE IF NOT EXISTS llm_usage (
    id               bigserial PRIMARY KEY,
    case_id          bigint REFERENCES "case"(case_id) ON DELETE SET NULL,
    turn_id          text,
    phase            text NOT NULL CHECK (phase IN ('route','scenario_tools','answer','validate','repair','embedding')),
    scenario         text,
    tool_names       text[] NOT NULL DEFAULT '{}',
    provider         text NOT NULL DEFAULT '',
    model            text NOT NULL DEFAULT '',
    prompt_tokens    integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    cached_tokens    integer NOT NULL DEFAULT 0,
    total_tokens     integer NOT NULL DEFAULT 0,
    cost_usd         numeric(12,6),
    latency_ms       integer,
    request          jsonb,
    response         jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE llm_usage IS 'One row per model call. Rolled up by turn_id for the trace view and by case_id for the session view — the Langfuse shape, self-hosted, mirroring enoract.';
COMMENT ON COLUMN llm_usage.phase IS 'Where in the turn the call sat. "validate" is this project''s addition: a prompt-based validator is itself a traced call, which is how a non-deterministic check still produces an auditable record.';

CREATE INDEX IF NOT EXISTS llm_usage_by_turn ON llm_usage (turn_id);
CREATE INDEX IF NOT EXISTS llm_usage_by_case ON llm_usage (case_id, created_at);
