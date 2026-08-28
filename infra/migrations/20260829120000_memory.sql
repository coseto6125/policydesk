-- Conversation memory: what survives the window.
--
-- A customer does not walk the flow in order. They ask about a claim, then about a
-- premium, then decide to apply, then come back two days later and ask the first
-- question again. A transcript window handles the local coherence and nothing else:
-- once the budget they stated scrolls out, it is gone.
--
-- So two stores, on different clocks. `member_fact` holds what stays true about a
-- person across every case they ever open. `case.summary` holds where one application
-- has got to. Both are written by an offline sweep rather than on the reply path, so
-- neither costs the customer a second of latency.
--
-- Modelled on enoract's user_facts/summary pair, re-keyed: enoract keys facts by
-- conversation, which splits one person's profile across channels. Here a fact about a
-- customer belongs to the customer.

CREATE TABLE IF NOT EXISTS member_fact (
    member_id         bigint NOT NULL REFERENCES member(member_id) ON DELETE CASCADE,
    key               text NOT NULL,
    value             text NOT NULL,
    category          text NOT NULL CHECK (category IN ('need','cons','hist','pref')),
    source_message_id bigint REFERENCES conversation_message(message_id) ON DELETE SET NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (member_id, key)
);
COMMENT ON TABLE member_fact IS 'Durable facts about one customer, extracted from their own words. Keyed by member because a person''s budget and constraints outlive the case they first mentioned them in.';
COMMENT ON COLUMN member_fact.category IS 'need 保障需求 · cons 硬限制（預算上限、不願加費） · hist 已發生的事（曾理賠、曾被拒保） · pref 偏好（繳別、聯絡方式）.';
COMMENT ON COLUMN member_fact.source_message_id IS 'The message the fact was read out of. A fact with no evidence pointer is a guess, and this column is what makes the difference checkable.';

CREATE INDEX IF NOT EXISTS member_fact_by_member ON member_fact (member_id, updated_at DESC);

ALTER TABLE "case" ADD COLUMN IF NOT EXISTS summary text NOT NULL DEFAULT '';
ALTER TABLE "case" ADD COLUMN IF NOT EXISTS facts_extracted_at timestamptz;
COMMENT ON COLUMN "case".summary IS 'Where this application stands, in prose, folded forward each sweep rather than accumulated. Bounded, so it cannot grow into a second transcript.';
COMMENT ON COLUMN "case".facts_extracted_at IS 'Watermark. The sweep claims a case by advancing this, so two workers never extract the same messages twice.';

-- The sweep is itself a model call, and a model call this system does not record is a
-- model call it cannot audit.
ALTER TABLE llm_usage DROP CONSTRAINT IF EXISTS llm_usage_phase_check;
ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_phase_check
    CHECK (phase IN ('route','scenario_tools','answer','validate','repair','embedding','facts'));
