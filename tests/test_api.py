"""End-to-end HTTP tests over the FastAPI app."""

from __future__ import annotations


async def test_requires_authentication(client):
    resp = await client.get("/v1/accounts")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "authentication_error"


async def test_health_is_public(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def _create_account(client, headers, name, type_, currency="USD"):
    resp = await client.post(
        "/v1/accounts",
        headers=headers,
        json={"name": name, "type": type_, "currency": currency},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_full_deposit_and_reversal_flow(client, auth):
    headers = auth["headers"]

    cash = await _create_account(client, headers, "cash", "asset")
    deposits = await _create_account(client, headers, "customer deposits", "liability")

    # Post a balanced deposit.
    post = await client.post(
        "/v1/transactions",
        headers=headers,
        json={
            "description": "customer deposit",
            "postings": [
                {"account_id": cash["id"], "direction": "debit", "amount": "150.00"},
                {"account_id": deposits["id"], "direction": "credit", "amount": "150.00"},
            ],
        },
    )
    assert post.status_code == 201, post.text
    transaction = post.json()
    assert len(transaction["postings"]) == 2
    assert transaction["postings"][0]["amount"] == "150.00"

    # Balance reflects the posting.
    bal = await client.get(f"/v1/accounts/{cash['id']}/balance", headers=headers)
    assert bal.status_code == 200
    assert bal.json()["balance"] == "150.00"

    # Trial balance is balanced.
    tb = await client.get("/v1/ledger/trial-balance", headers=headers)
    assert tb.status_code == 200
    assert tb.json()["balanced"] is True

    # Reverse it.
    rev = await client.post(
        f"/v1/transactions/{transaction['id']}/reversal",
        headers=headers,
        json={"description": "oops"},
    )
    assert rev.status_code == 201, rev.text
    assert rev.json()["reverses_transaction_id"] == transaction["id"]

    bal_after = await client.get(f"/v1/accounts/{cash['id']}/balance", headers=headers)
    assert bal_after.json()["balance"] == "0.00"

    # Reversing again is a conflict.
    rev2 = await client.post(
        f"/v1/transactions/{transaction['id']}/reversal", headers=headers, json={}
    )
    assert rev2.status_code == 409


async def test_unbalanced_transaction_returns_422(client, auth):
    headers = auth["headers"]
    cash = await _create_account(client, headers, "cash", "asset")
    deposits = await _create_account(client, headers, "deposits", "liability")

    resp = await client.post(
        "/v1/transactions",
        headers=headers,
        json={
            "postings": [
                {"account_id": cash["id"], "direction": "debit", "amount": "10.00"},
                {"account_id": deposits["id"], "direction": "credit", "amount": "9.00"},
            ]
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unbalanced_transaction"


async def test_idempotent_post_over_http(client, auth):
    headers = auth["headers"]
    cash = await _create_account(client, headers, "cash", "asset")
    deposits = await _create_account(client, headers, "deposits", "liability")

    payload = {
        "idempotency_key": "txn-001",
        "postings": [
            {"account_id": cash["id"], "direction": "debit", "amount": "20.00"},
            {"account_id": deposits["id"], "direction": "credit", "amount": "20.00"},
        ],
    }
    first = await client.post("/v1/transactions", headers=headers, json=payload)
    second = await client.post("/v1/transactions", headers=headers, json=payload)
    assert first.json()["id"] == second.json()["id"]

    bal = await client.get(f"/v1/accounts/{cash['id']}/balance", headers=headers)
    assert bal.json()["balance"] == "20.00"
