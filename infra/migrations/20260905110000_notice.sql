-- When the 催告 for an unpaid instalment reached the customer. 保險法 §116 counts its thirty
-- days from the day after this date, so with it on record the desk computes the deadline
-- deterministically; without it, the desk says what the period runs from and asks. NULL
-- is "no arrival on record", which is not the same as "no notice sent".
ALTER TABLE premium_payment ADD COLUMN IF NOT EXISTS notice_arrived_at date;
