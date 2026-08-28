#!/usr/bin/env bash
# Contract PDFs are the insurers' copyright, so they stay out of git. Fetch them locally.
set -euo pipefail
cd "$(dirname "$0")/../data/clauses"
curl -sSkL -o cathay-inpatient-daily.pdf \
  "https://www.cathaylife.com.tw/cathaylife/-/media/7253dfd685a848449b6164739f4e360e.pdf?sc_lang=zh-tw&hash=945335A620C957D43CA47D06961FFCFD"
echo "fetched: $(ls -1 *.pdf | wc -l) contract(s)"
