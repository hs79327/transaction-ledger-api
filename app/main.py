from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Literal
from uuid import uuid4
import os
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

DB_PATH = os.getenv('LEDGER_DB', str(Path(__file__).resolve().parent.parent / 'ledger.db'))

def connection():
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA foreign_keys=ON')
    return db

def init_db():
    with connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(trim(name)) > 0),
            balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
            type TEXT NOT NULL CHECK(type IN ('credit','debit')),
            amount INTEGER NOT NULL CHECK(amount > 0), description TEXT NOT NULL,
            reference_id TEXT NOT NULL CHECK(length(trim(reference_id)) > 0),
            created_at TEXT NOT NULL, UNIQUE(account_id, reference_id)
        );
        CREATE INDEX IF NOT EXISTS ix_transactions_history ON transactions(account_id, created_at DESC, id DESC);
        CREATE TRIGGER IF NOT EXISTS immutable_transactions_update
        BEFORE UPDATE ON transactions BEGIN SELECT RAISE(ABORT, 'transactions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_transactions_delete
        BEFORE DELETE ON transactions BEGIN SELECT RAISE(ABORT, 'transactions are immutable'); END;
        ''')

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title='Transaction Ledger API', version='1.0.0', lifespan=lifespan)

class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

class AccountIn(StrictBody):
    name: str = Field(min_length=1, max_length=200)
    @field_validator('name')
    @classmethod
    def nonblank(cls, v):
        if not v.strip(): raise ValueError('must not be blank')
        return v.strip()

class TransactionIn(StrictBody):
    type: Literal['credit', 'debit']
    amount: Decimal = Field(gt=0)
    description: str = Field(default='', max_length=1000)
    reference_id: str = Field(min_length=1, max_length=200)
    @field_validator('reference_id')
    @classmethod
    def nonblank(cls, v):
        if not v.strip(): raise ValueError('must not be blank')
        return v.strip()

class TransferIn(StrictBody):
    from_account_id: str
    to_account_id: str
    amount: Decimal = Field(gt=0)
    reference_id: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=1000)
    @field_validator('reference_id')
    @classmethod
    def nonblank(cls, v):
        if not v.strip(): raise ValueError('must not be blank')
        return v.strip()

def paise(amount: Decimal) -> int:
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise HTTPException(422, detail='Amount must be finite with at most 2 decimal places')
    cents = int(amount * 100)
    if cents <= 0 or cents > 9_000_000_000_000_000:
        raise HTTPException(422, detail='Amount out of range')
    return cents

def money(value: int) -> str:
    return str(Decimal(value) / 100 if value % 100 else (Decimal(value) / 100).quantize(Decimal('0.00')))

def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')

def account_view(row):
    return {'id': row['id'], 'name': row['name'], 'balance': f"{row['balance']//100}.{row['balance']%100:02d}", 'created_at': row['created_at']}

def tx_view(row):
    return {'id': row['id'], 'account_id': row['account_id'], 'type': row['type'], 'amount': f"{row['amount']//100}.{row['amount']%100:02d}", 'description': row['description'], 'reference_id': row['reference_id'], 'created_at': row['created_at']}

def require_account(db, account_id):
    row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if row is None: raise HTTPException(404, detail='Account not found')
    return row

def begin(db):
    db.execute('BEGIN IMMEDIATE')

def add_tx(db, account_id, kind, amount, description, reference_id):
    existing = db.execute('SELECT id FROM transactions WHERE account_id=? AND reference_id=?', (account_id, reference_id)).fetchone()
    if existing: raise HTTPException(409, detail='Transaction reference already exists for this account')
    delta = amount if kind == 'credit' else -amount
    updated = db.execute('UPDATE accounts SET balance=balance+? WHERE id=? AND balance+? >= 0', (delta, account_id, delta)).rowcount
    if not updated: raise HTTPException(409, detail='Insufficient balance')
    txid = str(uuid4())
    db.execute('INSERT INTO transactions (id,account_id,type,amount,description,reference_id,created_at) VALUES (?,?,?,?,?,?,?)', (txid,account_id,kind,amount,description,reference_id,stamp()))
    return db.execute('SELECT * FROM transactions WHERE id=?', (txid,)).fetchone()

@app.post('/accounts', status_code=201)
def create_account(body: AccountIn):
    with connection() as db:
        uid = str(uuid4())
        db.execute('INSERT INTO accounts(id,name,balance,created_at) VALUES (?,?,0,?)', (uid, body.name, stamp()))
        return account_view(require_account(db, uid))

@app.get('/accounts/{account_id}/balance')
def get_balance(account_id: str):
    with connection() as db:
        row = require_account(db, account_id)
        return {'account_id': account_id, 'balance': f"{row['balance']//100}.{row['balance']%100:02d}"}

@app.post('/accounts/{account_id}/transactions', status_code=201)
def create_transaction(account_id: str, body: TransactionIn):
    amount = paise(body.amount)
    with connection() as db:
        begin(db)
        try:
            require_account(db, account_id)
            row = add_tx(db, account_id, body.type, amount, body.description, body.reference_id)
            db.commit()
            return tx_view(row)
        except Exception:
            db.rollback()
            raise

@app.get('/accounts/{account_id}/transactions')
def history(account_id: str, type: Literal['credit','debit'] | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    with connection() as db:
        require_account(db, account_id)
        condition = 'account_id=?' + (' AND type=?' if type else '')
        params = (account_id, type) if type else (account_id,)
        total = db.execute(f'SELECT COUNT(*) FROM transactions WHERE {condition}', params).fetchone()[0]
        rows = db.execute(f'SELECT * FROM transactions WHERE {condition} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?', (*params, limit, (page-1)*limit)).fetchall()
        return {'transactions': [tx_view(r) for r in rows], 'pagination': {'page':page, 'limit':limit, 'total_records':total, 'total_pages':ceil(total/limit)}}

@app.post('/transfers', status_code=201)
def transfer(body: TransferIn):
    if body.from_account_id == body.to_account_id:
        raise HTTPException(422, detail='Source and destination accounts must differ')
    amount = paise(body.amount)
    with connection() as db:
        begin(db)
        try:
            require_account(db, body.from_account_id)
            require_account(db, body.to_account_id)
            debit = add_tx(db, body.from_account_id, 'debit', amount, body.description, body.reference_id)
            credit = add_tx(db, body.to_account_id, 'credit', amount, body.description, body.reference_id)
            db.commit()
            return {'transfer_id': debit['id'], 'debit_transaction': tx_view(debit), 'credit_transaction': tx_view(credit)}
        except Exception:
            db.rollback()
            raise
