-- The clauses a reply cited, kept with the reply. The live socket already sends them to
-- the customer; the console's transcript could not show them afterwards, because nothing
-- wrote them down. Clause ids only: the transcript resolves them against the member's own
-- book at read time, the way the socket does, so a bare id never becomes a link on its own.
ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS citations text[] NOT NULL DEFAULT '{}';

-- Replies written before the column existed carry their citations in the text, as the
-- [art.5] markers the answer prompt asks for. Read them back so the transcript shows them.
UPDATE conversation_message
SET citations = ARRAY(SELECT DISTINCT m[1] FROM regexp_matches(text, '\[((?:art\.[0-9A-Za-z.]+)|waiting)\]', 'g') AS m)
WHERE speaker = 'agent' AND citations = '{}' AND text ~ '\[(art\.[0-9A-Za-z.]+|waiting)\]';
