# Write-up: AI proposal triage (MATACS-TH-2026-02)

**Flow:** upload → ingest → **extract (AI)** → verify → mandatory checks and limits → score → rank → **human review** → human award decision. See `diagram/flow.png`. Proposals are referred to by letter (A–E).

## 1. AI vs. rules

I used AI only to extract structured information from proposal text, such as licence details, price, completion time, and warranty. Each extracted value must include an exact quote from the proposal so it can be checked. The remaining work, including validation, eligibility checks, scoring, ranking, and reasons, is handled by normal code.

I made this separation because extracted information can be checked against the original document, but an AI-generated score is harder to verify and can be influenced by instructions hidden inside a proposal. I wanted the system to follow the same rules for every bidder, so AI extracts the information but never decides who scores higher.

## 2. Consistency and explainability

- **Scoring:** I used a formula for each weighted criterion, based on the minimum and maximum values I configured. For example, a price of $260,000 receives zero marks, while $208,000 receives full marks. The same extracted data and criteria will produce the same scores. If two proposals tie, the lower-priced proposal ranks first, followed by the letter. I also round completion times up so rounding does not give a bidder an advantage.
- **Repeatable results:** AI extraction is not guaranteed to produce identical results every time, even at temperature 0. To avoid changing results unexpectedly, I store the extraction and use it again when recalculating scores. Re-extraction requires an explicit request and is logged.
- **Dates:** The five-year experience check uses the current year, so the date is part of the decision record.
- **Explainability:** Reviewers can see the points awarded for each criterion, the supporting quotes and page numbers, mandatory check results, and any flags. The system also identifies close rankings when the score difference is less than two points. I generate the reasons from the actual results in code rather than asking AI to write them, which helps keep the explanation consistent with the score.

## 3. Human in the loop

- **Flags:**  If information is unknown, unverified, or suspicious, the proposal is not ranked automatically. A reviewer can clear a flag only by providing their name and a written reason. The clearance is linked to the file's hash, so changing the file invalidates that clearance.
- **Verification:** The system checks what a bidder has stated in the proposal, not whether the claims are actually true. For example, it does not verify licence numbers against an official registry. A reviewer must verify the mandatory claims of the shortlisted bidders.
- **Award:** The ranking is only a recommendation to support the reviewer. A human makes the final award decision. Recording a reason for selecting a lower-ranked bidder is a planned feature. But it is not implemented in the current prototype.
- **Audit log (append-only):** I designed the audit trail to record file hashes, extraction details, model and prompt versions, criteria versions, decisions, rankings, and reviewer clearances. The purpose is to make it possible to understand how a result was produced and what changed during the process.

## 4. Changing criteria

I stored criteria by category with version numbers so previous decisions can be traced back to the rules used at that time.

- If weights, scoring anchors, or limits change, the system can recalculate scores using the stored extraction without calling AI again. The new decision gets its own number, and earlier decisions are retained.

- If a new criterion requires a field that was not extracted, the system refuses to score until that field is available and asks for a new extraction.

- If a field definition changes, the reviewer can see the criteria version and the version used for extraction, making possible mismatches visible.

- The admin screen should also reject criteria with weights that do not total 100 or fields with incorrect data types.

## 5. Risks

| Risk | How I handled it |
|---|---|
| Wrong extraction | 

I check that the supporting quote exists in the document and that extracted numbers appear in their own quotes. If validation fails, the field is dropped, treated as missing, and flagged. A quote can still be interpreted incorrectly, so reviewers need to check important details. |
| Made-up information | If something is not stated, it remains null. Even a negative answer must have supporting evidence. Statements such as "to be confirmed after award" are treated as missing information. |
| Prompt Injection | I treat proposal content as untrusted input. Use unique markers around it, and instruct the model not to follow embedded instructions. I also scan the text for suspicious phrases. Since AI does not control scoring or ranking. Proposal instructions cannot directly change the scoring rules. Suspicious proposals are flagged rather than automatically rejected because disqualification is a procurement decision. |
| False bidder claims | The system cannot establish whether every claim is true from the proposal alone. Registry checks are a possible future improvement, so human verification is important for shortlisted bidders. |
| Bias | The scoring rules use administrator-defined criteria rather than company names, company size, or location. However, the experience threshold can still favour more established firms, so I made the scoring anchors visible and configurable. A lead being named is accepted as a stated claim, not independently verified. |
| Scoring assumptions | The results depend on the anchors I selected. In particular, some proposals are separated by only a few points, so I would review how changing the anchors affects the ranking before relying on close results. |
| Scans and OCR errors | Low-confidence OCR results are rejected. When OCR is used, the reviewer is warned to verify important values, such as prices, dates, and experience counts, against the original PDF. |
| Cost | At most 50 files per run, 200 MB per request, 60,000 characters per proposal (flagged, not truncated), one retry, duplicates by hash are not re-processed, and extraction runs once per run.I set limits of 50 files per run, 200 MB per request, and 60,000 characters per proposal. Oversized proposals are flagged rather than silently truncated. Extraction is limited to one run with one retry, and duplicate files are identified by hash. |
| Privacy | Only fictional data is used. Keys live in `.env`, never in the repo. |

## Result on the sample set
- Already placed in the ranking.json file

## Assumptions I Made

- The brief gives weights only, so the full/zero anchors are mine: experience full at 12 installs, price full at $208,000, timeline full at 8 weeks, warranty full at 25 years (panel) and 15 years (workmanship). They are config, not code.
- I used the calendar year for the five-year experience check, meaning "since 2021" is accepted as within the window for the current year. For team criteria, I assigned four points for a named lead, three for a stated crew size, four for a safety plan, and four for a monitoring plan
- I did not send any emails to request clarification. I therefore documented the assumptions I made instead of presenting them as requirements specified by the brief. 