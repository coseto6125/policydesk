-- The law the contract sits under, as its own corpus.
--
-- `clause` is FK'd to `product` and every read of it is `WHERE product_id = ANY(...)`,
-- because a clause belongs to a contract somebody bought. A statute belongs to nobody
-- and binds everybody, so putting it in `clause` would mean a product_id that names
-- nothing and a scope filter that has to be defeated on every statute read.
--
-- Granularity is 條 / 項 / 款, all three stored, because all three are how a citation is
-- written. 保險法 §64 II is the sentence that decides whether a policy can be rescinded;
-- returning the whole article when the customer's question turns on one 項 makes the
-- reader find the sentence themselves, which is the work the desk exists to do.

CREATE TABLE IF NOT EXISTS statute (
    statute_id  text PRIMARY KEY,
    name        text NOT NULL,
    authority   text NOT NULL DEFAULT '',
    amended_at  date,
    source_url  text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE statute IS 'One row per Act. statute_id is the scope_id the retriever filters on.';
COMMENT ON COLUMN statute.amended_at IS 'Latest amendment date as published. A statute the desk quotes from an older revision is a wrong answer that reads like a right one.';

CREATE TABLE IF NOT EXISTS statute_article (
    statute_id   text NOT NULL REFERENCES statute(statute_id) ON DELETE CASCADE,
    doc_id       text NOT NULL,
    article      integer NOT NULL,
    branch       integer NOT NULL DEFAULT 0,
    paragraph    integer,
    subparagraph integer,
    chapter      text NOT NULL DEFAULT '',
    heading      text NOT NULL DEFAULT '',
    verbatim     text NOT NULL,
    PRIMARY KEY (statute_id, doc_id)
);
COMMENT ON COLUMN statute_article.doc_id IS 'The citation as a lawyer would type it: art.64 / art.64.1 / art.64.1.2 / art.8-1 for 第八條之一.';
COMMENT ON COLUMN statute_article.branch IS '之一 / 之二. Zero for a plain article. Kept separate from `article` so §8 and §8-1 sort and filter as the distinct provisions they are.';
COMMENT ON COLUMN statute_article.paragraph IS '項. NULL on the whole-article row, which is stored alongside its paragraphs so a citation to the article as a whole still resolves.';
COMMENT ON COLUMN statute_article.chapter IS '章節標題. 保險法 prints no per-article headings, so the 章 is what the term collector and any heading boost have to work with.';

CREATE INDEX IF NOT EXISTS statute_article_by_number ON statute_article (statute_id, article, branch, paragraph, subparagraph);
