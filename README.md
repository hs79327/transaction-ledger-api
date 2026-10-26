# Transaction Ledger API

FastAPI + SQLite ledger with atomic credits, debits, and transfers. Balances and amounts are represented as **decimal strings in rupees** over HTTP and stored as **integer paise** in SQLite.

## Setup (Python 3.10+)

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive OpenAPI documentation. The SQLite database `ledger.db` is created automatically. Optional `LEDGER_DB` environment variable overrides the path.

## API reference

| Method | Route | Description |
|---|---|---|
| POST | `/accounts` | Create account, body `{"name":"Alice"}`; 201 |
| GET | `/accounts/{account_id}/balance` | Current balance; 200 |
| POST | `/accounts/{account_id}/transactions` | Credit/debit; 201 |
| GET | `/accounts/{account_id}/transactions?type=credit&page=1&limit=20` | Filtered, paginated, newest first; 200 |
| POST | `/transfers` | Atomic transfer; 201 |

### Examples

```bash
curl -X POST http://127.0.0.1:8000/accounts -H "Content-Type: application/json" -d '{"name":"Alice"}'
```

Replace `ACCOUNT_ID` with the UUID returned above:

```bash
curl -X POST http://127.0.0.1:8000/accounts/ACCOUNT_ID/transactions -H "Content-Type: application/json" -d '{"type":"credit","amount":"100.00","description":"Deposit","reference_id":"dep-001"}'
curl http://127.0.0.1:8000/accounts/ACCOUNT_ID/balance
curl 'http://127.0.0.1:8000/accounts/ACCOUNT_ID/transactions?page=1&limit=10'
```

Transfer body:

```json
{"from_account_id":"SOURCE_UUID","to_account_id":"DEST_UUID","amount":"30.00","reference_id":"transfer-001","description":"Payment"}
```

Responses include UUID identifiers, UTC ISO-8601 timestamps and money formatted as strings (e.g. `"70.00"`). History returns `transactions` and `pagination` (`page`, `limit`, `total_records`, `total_pages`).

### Errors

- `404` account missing
- `409` duplicate account-scoped reference or insufficient funds
- `422` invalid payload, amount, account names, or pagination
- `404` unknown transaction modification paths (no modification endpoints exist)

FastAPI provides standard JSON error bodies using `detail` (string for business errors, structured array for request validation).

## Assumptions and guarantees

1. All amounts use two-decimal-place currency units (e.g. INR); values are stored in integer paise. Maximum per-operation amount: 90 trillion currency units.
2. Account IDs and transaction IDs are generated UUIDs. Initial balances are always zero; client-supplied balance is rejected.
3. A transaction reference is unique **within its account**, including transfers; the same reference can exist in different accounts. Duplicate submissions return HTTP 409, without balance changes.
4. Transfers use the same reference on both sides, so retries cannot post twice. If a destination already has that reference, the entire transfer rolls back.
5. SQLite `BEGIN IMMEDIATE` serializes writers; both ledger entry insertion and balance update occur in one database transaction. SQLite foreign keys and CHECK constraints are enabled on every connection.
6. Transaction rows are immutable through the API and database triggers. For production, restrict direct database access and add authorization, observability, backup/recovery and migration tooling.
7. `created_at` uses UTC. History orders by timestamp descending, then UUID descending for deterministic ties. Page starts at 1; limit 1–100; empty results have 0 total pages.
8. Transfers generate two ledger entries. A dedicated transfer table is omitted for assignment scope; the response includes both entries and the debit ID as the transfer identifier.
9. Accounts are not exposed for update/delete. Administrative direct SQL changes to balances are outside the trusted API model.

## Tests

```bash
pytest -q
```

Tests cover account validation, balance calculations, insufficient funds, duplicate prevention, transfer rollback, history filtering/pagination and transaction immutability.
