import os
os.environ['LEDGER_DB'] = ':memory:'  # overridden below for separate connections
from fastapi.testclient import TestClient
import app.main as ledger
import pytest

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, 'DB_PATH', str(tmp_path/'test.db'))
    with TestClient(ledger.app) as c:
        yield c

def account(client, name='A'):
    r=client.post('/accounts',json={'name':name})
    assert r.status_code==201
    return r.json()['id']

def tx(client, uid, kind, amount, ref):
    return client.post(f'/accounts/{uid}/transactions',json={'type':kind,'amount':amount,'reference_id':ref,'description':'test'})

def test_balance_and_duplicates(client):
    uid=account(client)
    assert tx(client,uid,'credit','100.00','a').status_code==201
    assert tx(client,uid,'debit','30.00','b').status_code==201
    assert tx(client,uid,'credit','999.00','a').status_code==409
    assert tx(client,uid,'debit','100.00','c').status_code==409
    assert client.get(f'/accounts/{uid}/balance').json()['balance']=='70.00'
    assert client.get(f'/accounts/{uid}/transactions').json()['pagination']['total_records']==2

def test_transfer_atomic_and_history(client):
    a,b=account(client),account(client,'B')
    tx(client,a,'credit','100','seed')
    assert client.post('/transfers',json={'from_account_id':a,'to_account_id':b,'amount':'30','reference_id':'t1'}).status_code==201
    assert client.get(f'/accounts/{a}/balance').json()['balance']=='70.00'
    assert client.get(f'/accounts/{b}/balance').json()['balance']=='30.00'
    assert client.post('/transfers',json={'from_account_id':a,'to_account_id':b,'amount':'10','reference_id':'t1'}).status_code==409
    assert client.get(f'/accounts/{a}/balance').json()['balance']=='70.00'
    assert len(client.get(f'/accounts/{a}/transactions',params={'type':'debit','page':1,'limit':1}).json()['transactions'])==1

def test_validation_and_immutability(client):
    assert client.post('/accounts',json={'name':'A','balance':100}).status_code==422
    assert client.post('/accounts',json={'name':' '}).status_code==422
    a=account(client)
    assert tx(client,a,'credit','0.001','x').status_code==422
    assert tx(client,a,'credit','1.00','x').status_code==201
    assert client.get(f'/accounts/{a}/transactions',params={'page':0}).status_code==422
    assert client.delete(f'/accounts/{a}/transactions/anything').status_code==404
    with ledger.connection() as db:
        with pytest.raises(Exception):
            db.execute('DELETE FROM transactions WHERE account_id=?',(a,))
