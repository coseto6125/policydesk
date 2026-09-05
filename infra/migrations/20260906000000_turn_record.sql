-- What a reply stood on, and what stopped it, kept with the reply. `citations` already
-- records the clauses a reply cited; a withheld reply has none, and its reason lived only
-- in a log line. An auditor asking "which turns were withheld this week, and what had the
-- model been shown" had no table to ask.
ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS faults text[] NOT NULL DEFAULT '{}';
ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS evidence jsonb;

COMMENT ON COLUMN conversation_message.faults IS 'Why an agent reply was withheld: answer_format, answer_leak, unoffered_calculator, unoffered_dates, date:<date>, source:<key>, promise:<phrase>, a statute the corpus does not hold. Empty for a reply that passed every check and for every customer row.';
COMMENT ON COLUMN conversation_message.evidence IS 'Agent rows only. scenario the router chose and the names of the parameters it filled (never their values: one of them is the national id); offered = the clause keys the tools returned with each retrieval score (null for a row no channel ranked); coverage = whether the evidence budget cut rows; computations and dates = the expressions the model wrote and what the tools made of them. Scores are recorded here and never shown to the model.';

-- `repair` was a phase the pipeline diagram drew and no code ever wrote. A constraint
-- naming a value nothing produces is a promise the trace does not keep. Nothing wrote the
-- value, so a row carrying it is not this migration's to reinterpret: stop and say so.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM llm_usage WHERE phase = 'repair') THEN
        RAISE EXCEPTION 'llm_usage holds phase = repair rows; no code ever wrote them, so decide what they are before this migration drops the phase';
    END IF;
END $$;
-- NOT VALID then VALIDATE: the validation scan takes a lock that does not block the
-- insert every model call makes, where a plain ADD CONSTRAINT would hold them until the
-- scan finished.
ALTER TABLE llm_usage DROP CONSTRAINT IF EXISTS llm_usage_phase_check;
ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_phase_check
    CHECK (phase IN ('route','scenario_tools','answer','validate','embedding','facts')) NOT VALID;
ALTER TABLE llm_usage VALIDATE CONSTRAINT llm_usage_phase_check;
