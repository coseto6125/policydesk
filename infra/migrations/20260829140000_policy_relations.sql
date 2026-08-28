-- A rider hangs off a main policy, and the database says so.
--
-- `main_policy_ref` was a text policy number with nothing on the other end of it, so a
-- rider pointing at a contract that does not exist was a row the schema permitted. It
-- was planted deliberately as a fault case; the decision now is that the relational
-- model should make it impossible rather than the generator make it common.
--
-- A self-referencing FK does that: a rider cannot be written before its main policy,
-- and deleting a main policy takes its riders with it, which is what surrendering a
-- main contract does to the riders attached to it.

ALTER TABLE policy ADD COLUMN IF NOT EXISTS main_policy_id bigint
    REFERENCES policy(policy_id) ON DELETE CASCADE;
COMMENT ON COLUMN policy.main_policy_id IS 'The main contract this rider attaches to. NULL for a main policy. Enforced by the FK, so an orphan rider is not a state this table can hold.';

CREATE INDEX IF NOT EXISTS policy_riders ON policy (main_policy_id) WHERE main_policy_id IS NOT NULL;

-- Attach any rider whose named main policy is genuinely in the same member's book.
UPDATE policy r SET main_policy_id = m.policy_id
FROM policy m
WHERE r.main_policy_ref IS NOT NULL
  AND r.main_policy_id IS NULL
  AND m.member_id = r.member_id
  AND m.policy_number = r.main_policy_ref;

-- The rest named a contract nobody holds. They describe cover that does not exist, so
-- they are removed rather than converted into main policies they never were.
DELETE FROM policy WHERE main_policy_ref IS NOT NULL AND main_policy_id IS NULL;

ALTER TABLE policy DROP COLUMN IF EXISTS main_policy_ref;
