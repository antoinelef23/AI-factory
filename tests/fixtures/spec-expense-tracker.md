---
type: spec
feature: expense-tracker
version: 0.1.0
status: draft
---

# Spec: Expense tracker

## 1. Intent

Employees submit expenses (amount in EUR, category, date) through an HTTP API. Managers list the expenses of a given employee for a given month. An expense above 500.00 EUR must carry a receipt URL. The target maturity is `poc`.

**Target KPI**
- KPI-1: 100% of expenses above 500.00 EUR stored by the system have a receipt URL (measured over the eval dataset; 0 violations tolerated).
- KPI-2: a monthly listing for one employee holding 1,000 expenses returns in under 500 ms at p95 on the reference test machine.

## 2. Glossary

- **Expense**: a record with `id`, `employee_id`, `amount`, `category`, `date`, and optionally `receipt_url`.
- **Amount**: a positive decimal number in EUR with at most 2 decimal places.
- **Category**: one of `travel`, `meals`, `lodging`, `equipment`, `other`.
- **Date**: the day the expense was incurred, ISO 8601 `YYYY-MM-DD`.
- **Month**: a calendar month in the form `YYYY-MM`.
- **Employee**: the person submitting an expense, identified by a non-empty `employee_id` string of at most 64 characters.
- **Manager**: the person who lists expenses. Authentication is out of scope (see Non-goals).
- **Receipt threshold**: 500.00 EUR. An amount strictly greater than it requires `receipt_url`; an amount equal to it does not.
- **Receipt URL**: a string starting with `http://` or `https://`, at most 2048 characters.

## 3. Invariants

- INV-1: Only technologies allowed by the company tech radar at maturity `poc` are used.
- INV-2: Every stored expense has an amount > 0.00 and ≤ 1,000,000.00 EUR, with at most 2 decimal places.
- INV-3: Every stored expense with an amount > 500.00 has a non-empty, valid receipt URL.
- INV-4: Every stored expense has a category from the allowed list and a valid calendar date.
- INV-5: A rejected submission (any 4xx response) never creates or modifies a stored expense.
- INV-6: Listing expenses never modifies stored data.
- INV-7: Every error response has a JSON body with an `error` field containing a non-empty string, and uses status 400 for validation errors or 404 for unknown routes.
- INV-8: Monetary amounts are never subject to floating-point rounding: an amount submitted as `12.34` is returned as exactly `12.34`.

## 4. Behaviors

- BHV-1: `GET /health` returns 200 {"status": "ok"}.
  - Given the service is running, When a client calls `GET /health`, Then the response is 200 with body `{"status": "ok"}`.
- BHV-2: Submit a valid expense.
  - Given a valid body with `employee_id`, `amount`, `category`, `date`, When a client calls `POST /expenses`, Then the response is 201 with the stored expense including a generated unique `id`.
- BHV-3: Receipt required above threshold.
  - Given an `amount` > 500.00 and no `receipt_url`, When a client calls `POST /expenses`, Then the response is 400 and nothing is stored.
- BHV-4: Receipt not required at or below threshold.
  - Given an `amount` ≤ 500.00 and no `receipt_url`, When a client calls `POST /expenses`, Then the response is 201.
- BHV-5: Invalid amount rejected.
  - Given an `amount` that is ≤ 0, > 1,000,000.00, has more than 2 decimals, or is not a number, When a client calls `POST /expenses`, Then the response is 400 and nothing is stored.
- BHV-6: Invalid category, date or employee rejected.
  - Given a `category` outside the allowed list, a `date` that is not a real calendar date in `YYYY-MM-DD`, or a missing/empty/over-64-character `employee_id`, When a client calls `POST /expenses`, Then the response is 400 and nothing is stored.
- BHV-7: Invalid receipt URL rejected.
  - Given a `receipt_url` that does not start with `http://` or `https://`, or exceeds 2048 characters, When a client calls `POST /expenses`, Then the response is 400 and nothing is stored.
- BHV-8: List by employee and month.
  - Given stored expenses, When a client calls `GET /expenses?employee_id=E&month=YYYY-MM`, Then the response is 200 with a JSON array of exactly the expenses of employee E whose date falls in that month, ordered by `date` ascending then by creation order.
- BHV-9: Empty listing.
  - Given no stored expense matches, When a client calls `GET /expenses?employee_id=E&month=YYYY-MM`, Then the response is 200 with `[]`.
- BHV-10: Listing parameter validation.
  - Given `employee_id` or `month` is missing, or `month` is not a valid `YYYY-MM` (month 01–12), When a client calls `GET /expenses`, Then the response is 400.
- BHV-11: Malformed body.
  - Given a request body that is not valid JSON or is not a JSON object, When a client calls `POST /expenses`, Then the response is 400 and nothing is stored.
- BHV-12: Unknown route.
  - Given a path that is not defined, When a client calls it, Then the response is 404 with a JSON `error` body.

## 5. Examples

```yaml
- id: EX-1
  covers: [BHV-1]
  request: GET /health
  response: {status: 200, body: {status: ok}}

- id: EX-2
  covers: [BHV-2, INV-8]
  request:
    POST /expenses
    body: {employee_id: "emp-042", amount: 48.90, category: meals, date: "2026-09-18"}
  response:
    status: 201
    body: {id: "<non-empty string>", employee_id: "emp-042", amount: 48.90, category: meals, date: "2026-09-18"}

- id: EX-3
  covers: [BHV-3, INV-3]
  request:
    POST /expenses
    body: {employee_id: "emp-042", amount: 820.00, category: lodging, date: "2026-09-20"}
  response: {status: 400, body: {error: "receipt_url required for amounts above 500.00"}}

- id: EX-4
  covers: [BHV-2, INV-3]
  request:
    POST /expenses
    body: {employee_id: "emp-042", amount: 820.00, category: lodging, date: "2026-09-20", receipt_url: "https://files.example.com/r/8841.pdf"}
  response: {status: 201}

- id: EX-5
  covers: [BHV-4]
  request:
    POST /expenses
    body: {employee_id: "emp-007", amount: 500.00, category: travel, date: "2026-09-02"}
  response: {status: 201}

- id: EX-6
  covers: [BHV-3]
  request:
    POST /expenses
    body: {employee_id: "emp-007", amount: 500.01, category: travel, date: "2026-09-02"}
  response: {status: 400}

- id: EX-7
  covers: [BHV-5]
  cases:
    - {amount: 0, expected: 400}
    - {amount: -12.50, expected: 400}
    - {amount: 10.999, expected: 400}
    - {amount: "abc", expected: 400}
    - {amount: 1000000.01, expected: 400}

- id: EX-8
  covers: [BHV-6]
  cases:
    - {category: "snacks", expected: 400}
    - {date: "2026-02-30", expected: 400}
    - {date: "18/09/2026", expected: 400}
    - {employee_id: "", expected: 400}

- id: EX-9
  covers: [BHV-7]
  cases:
    - {receipt_url: "ftp://files.example.com/r.pdf", expected: 400}
    - {receipt_url: "receipt.pdf", expected: 400}

- id: EX-10
  covers: [BHV-8]
  given: emp-042 has expenses dated 2026-08-31, 2026-09-01, 2026-09-18, 2026-10-01; emp-007 has one dated 2026-09-05
  request: GET /expenses?employee_id=emp-042&month=2026-09
  response: {status: 200, body: "array of 2 expenses, dated 2026-09-01 then 2026-09-18"}

- id: EX-11
  covers: [BHV-9]
  request: GET /expenses?employee_id=emp-999&month=2026-09
  response: {status: 200, body: []}

- id: EX-12
  covers: [BHV-10]
  cases:
    - {query: "month=2026-09", expected: 400}
    - {query: "employee_id=emp-042", expected: 400}
    - {query: "employee_id=emp-042&month=2026-13", expected: 400}
    - {query: "employee_id=emp-042&month=09-2026", expected: 400}

- id: EX-13
  covers: [BHV-11]
  request: POST /expenses with body `{amount: ` (truncated JSON)
  response: {status: 400}

- id: EX-14
  covers: [BHV-12]
  request: GET /unknown
  response: {status: 404, body: {error: "<non-empty string>"}}
```

## 6. Non-goals

- Authentication, authorization, and role checks (anyone can submit or list; "manager" is not enforced).
- Editing or deleting expenses.
- Approval or reimbursement workflows.
- Currencies other than EUR and currency conversion.
- Uploading or storing receipt files; only a URL is stored, and it is not fetched or checked for reachability.
- Pagination, sorting options other than the one defined, and filters other than `employee_id` and `month`.
- Reports, totals, exports, or notifications.
- Production-grade availability, scaling beyond 10,000 stored expenses, or data persistence guarantees beyond the `poc` level.
- A user interface.

## 7. Evals

| ID | Type | Description | Covers | Success threshold |
|----|------|-------------|--------|-------------------|
| EVAL-1 | static | Every technology used by the design appears in the tech radar at maturity `poc` or lower-risk; check dependency manifest against the radar | INV-1 | 0 technologies outside the radar |
| EVAL-2 | property | Generate 500 random submissions with amounts in (0, 1,000,000] and 2 decimals; every stored expense satisfies amount bounds and decimal precision | INV-2, BHV-5 | 500/500 pass |
| EVAL-3 | property | Generate 500 submissions with amounts above 500.00, half without receipt; no expense above 500.00 is stored without a valid receipt URL | INV-3, BHV-3, KPI-1 | 0 violations |
| EVAL-4 | property | Generate 300 submissions with random categories and dates (valid and invalid); all stored records have an allowed category and a real calendar date | INV-4, BHV-6 | 300/300 pass |
| EVAL-5 | integration | Snapshot the store, send 50 invalid POST requests, compare the store afterward | INV-5, BHV-11 | store identical (0 diffs) |
| EVAL-6 | integration | Snapshot the store, call `GET /expenses` 50 times with varied parameters, compare the store afterward | INV-6 | store identical (0 diffs) |
| EVAL-7 | integration | Trigger 20 distinct error cases (validation, malformed body, unknown route) and check status and JSON `error` field | INV-7, BHV-12 | 20/20 return the expected status and a non-empty `error` |
| EVAL-8 | integration | Submit amounts 0.10, 0.20, 12.34, 19.99, 499.99 and read them back; compare as decimal strings | INV-8, EX-2 | 5/5 exactly equal |
| EVAL-9 | integration | Call `GET /health` | BHV-1, EX-1 | status 200 and body exactly `{"status": "ok"}` |
| EVAL-10 | integration | Run EX-2 and EX-4 and confirm unique ids over 100 submissions | BHV-2 | 100/100 return 201 with distinct non-empty `id` |
| EVAL-11 | integration | Run EX-5 and EX-6 (boundary at 500.00 and 500.01) | BHV-4, BHV-3 | 2/2 as expected |
| EVAL-12 | integration | Run all cases of EX-7 | BHV-5 | 5/5 return 400 |
| EVAL-13 | integration | Run all cases of EX-9 and one 2049-character URL | BHV-7 | 3/3 return 400 |
| EVAL-14 | integration | Run EX-10 and a ordering check with 3 same-date expenses | BHV-8 | exact set and order match |
| EVAL-15 | integration | Run EX-11 | BHV-9 | status 200 and body `[]` |
| EVAL-16 | integration | Run all cases of EX-12 | BHV-10 | 4/4 return 400 |
| EVAL-17 | performance | Load 1,000 expenses for one employee in one month, call the listing 100 times, measure latency | KPI-2, BHV-8 | p95 < 500 ms |
