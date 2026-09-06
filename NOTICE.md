# NOTICE

本專案的**程式碼**採 MIT 授權，見 [LICENSE](LICENSE)。

以下**內容**不屬於本專案，也不隨 MIT 授權轉授。

---

## 保險商品與條款素材

來源：國泰人壽官方網站公開之保險商品文件（<https://www.cathaylife.com.tw>），
擷取程序見 `src/policydesk/ingest/cathay.py`，遵守該站 robots.txt。

**版權屬國泰金控所有。** 本專案僅為技術展示而引用，未取得再授權。

- 條款 PDF 未納入版本控制，見 `.gitignore`。
- `data/policydesk.db` 內含由上述文件擷取的條款全文，共 660 個商品、11,741 條條款，
  僅供本專案的檢索與引用功能運作。
- 任何再散布、改作或商業使用，請自行向國泰金控取得授權。

若權利人認為本專案的引用逾越合理範圍，請透過下列任一方式通知，將立即移除：

- 電子郵件：<enorenor@gmail.com>
- 維護者 GitHub 個人頁：<https://github.com/coseto6125>
- GitHub 內容移除申訴：<https://github.com/contact/dmca>

本 repo 可能處於封存（read-only）狀態，此時無法開立 issue，請改用上列管道。

## 法規條文

`statute` 與 `statute_article` 的條文取自全國法規資料庫（<https://law.moj.gov.tw/>），
屬中華民國政府公開資訊。

## 合成資料

保戶、保單、繳費、理賠與身分驗證紀錄皆由 `src/policydesk/synthetic/` 產生，
與真實個人無關，不得作為任何投保、核保或理賠的依據。

費率、計價基數與資格標記為 `synthetic_demo`，不是正式費率表。
