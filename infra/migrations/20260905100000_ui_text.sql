-- The language each customer message was written in, as `agent.locale` read it. 'und'
-- is a message that carries no language (a policy number, an 「ok」); the next reply keeps
-- the conversation's last real locale.
ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS locale text NOT NULL DEFAULT 'und';

-- The one-tap chips under a reply, in every language but the desk's own. The reply itself
-- is written by the model in the customer's language; the chips are literals the code
-- holds in zh-TW. The key is that sentence exactly as the code holds it, so adding a
-- language is adding rows. zh-CN has no rows: it is zh-TW through OpenCC at read time.
CREATE TABLE IF NOT EXISTS ui_text (
  locale text NOT NULL,
  key    text NOT NULL,
  text   text NOT NULL,
  PRIMARY KEY (locale, key)
);

INSERT INTO ui_text (locale, key, text) VALUES
  -- openers
  ('en', '我想了解目前的保單保什麼', 'What do my current policies cover?'),
  ('en', '想確認一年繳多少保費', 'How much premium do I pay per year?'),
  ('en', '理賠要準備哪些文件？', 'Which documents does a claim need?'),
  ('en', '我想了解有沒有適合的方案', 'Is there a plan that suits me?'),
  ('en', '猶豫期是幾天？', 'How long is the free-look period?'),
  ('en', '健康告知沒寫到會怎樣？', 'What happens if my health disclosure missed something?'),
  ('en', '保單停效還能不能復效？', 'Can a lapsed policy be reinstated?'),
  ('en', '你們有哪些商品？', 'Which products do you offer?'),
  -- chips
  ('en', '這些保障有哪些不賠的情況？', 'Which situations do these covers exclude?'),
  ('en', '我想了解保額夠不夠', 'Is my sum insured enough?'),
  ('en', '想確認有沒有重複投保', 'Do any of my policies overlap?'),
  ('en', '這張有哪些不賠的情況？', 'Which situations does this policy exclude?'),
  ('en', '等待期是多久？', 'How long is the waiting period?'),
  ('en', '我想了解目前的保障夠不夠', 'Is my current cover enough?'),
  ('en', '這幾張的差別在哪？', 'How do these policies differ?'),
  ('en', '我想了解等待期怎麼算', 'How is the waiting period counted?'),
  ('en', '想確認附約要不要先有主約', 'Does a rider need a main policy first?'),
  ('en', '這幾張的等待期差在哪？', 'How do their waiting periods differ?'),
  ('en', '我想了解各張的除外責任', 'What does each policy exclude?'),
  ('en', '想確認保費怎麼算出來的', 'How was the premium calculated?'),
  ('en', '診斷證明書要寫到什麼程度？', 'How detailed must the medical certificate be?'),
  ('en', '我想了解手術給付倍數怎麼算', 'How is the surgery benefit multiplier applied?'),
  ('en', '送出後大概多久會有結果？', 'How long after filing will I hear back?'),
  ('en', '我想了解可以改成月繳嗎？', 'Can I switch to monthly payments?'),
  ('en', '沒繳到會怎麼樣？', 'What happens if I miss a payment?'),
  ('en', '想確認下一期的繳費日', 'When is my next payment due?'),
  ('en', '我想了解這些保額夠不夠', 'Are these sums insured enough?'),
  ('en', '已經理賠過的會扣掉嗎？', 'Are earlier claims deducted?'),
  ('en', '逾期了還能補繳嗎？', 'Can I still pay after the due date?'),
  ('en', '可以改成年繳嗎？', 'Can I switch to annual payments?'),
  ('en', '催告通知會寄到哪裡？', 'Where is the payment notice sent?'),
  ('en', '我想知道這條實際怎麼適用在我身上', 'How does this provision apply to me?'),
  ('en', '申訴要向誰提出？', 'Whom do I file a complaint with?'),
  ('en', '我的保單條款是怎麼寫的？', 'What do my policy terms say?'),
  ('en', '受益人可以填未成年的孩子嗎？', 'Can a minor child be the beneficiary?'),
  ('en', '如果超過期限還有機會嗎？', 'Is there still a chance after the deadline?'),
  ('en', '復效需要準備哪些文件？', 'Which documents does reinstatement need?'),
  ('en', '想加保壽險', 'I would like to add life cover'),
  ('en', '我想了解怎麼補上這個缺口', 'How can I close this gap?'),
  ('en', '我想了解這張保單的保障內容', 'What does this policy cover?'),
  ('en', '我想查其他保單的理賠', 'Check claims on my other policies'),
  ('en', '我想確認保額夠不夠', 'Is the sum insured enough?'),
  ('en', '我想確認我的保單什麼時候生效', 'When does my policy take effect?'),
  ('en', '我這張保單也有這個權利嗎？', 'Does my policy carry this right too?'),
  ('en', '換一個保額再算一次可以嗎？', 'Can you recalculate with a different sum insured?'),
  ('en', '換工作後保費會怎麼算？', 'How does a job change affect my premium?'),
  ('en', '撤銷之後保費怎麼退？', 'How is the premium refunded after cancellation?'),
  ('en', '現在補告知還來得及嗎？', 'Can I still add to my disclosure now?'),
  ('en', '理賠結果不滿意可以怎麼處理？', 'What can I do if I disagree with a claim decision?'),
  ('en', '萬一忘了通知會怎樣？', 'What if I forget to notify you?'),
  ('en', '要用什麼方式通知才算數？', 'Which form of notice counts?'),
  ('en', '變更後從什麼時候生效？', 'When does the change take effect?'),
  ('en', '這些保障各自的除外責任是什麼', 'What does each of these covers exclude?'),
  ('en', '這件事要跟誰確認比較準？', 'Who is the right person to confirm this with?'),
  ('en', '這個理賠案還缺什麼文件？', 'Which documents is this claim still missing?'),
  ('en', '這個要準備什麼文件？', 'Which documents does this need?'),
  ('en', '這張的等待期是多久？', 'How long is this policy''s waiting period?'),
  ('en', '還有沒有更便宜的同類商品？', 'Is there a cheaper product of the same kind?'),
  ('en', '除斥期間是怎麼算的？', 'How is the limitation period counted?')
ON CONFLICT (locale, key) DO UPDATE SET text = excluded.text;
