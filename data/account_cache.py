"""Resumable, block-pinned account reads backed by SQLite."""

from __future__ import annotations

import hashlib
import os
import sqlite3

from . import aave_v3
from .aave_v3 import ChainConfig
from .rpc import EthRpc


def _users_hash(users: list[str]) -> str:
    return hashlib.sha256("\n".join(users).encode("ascii")).hexdigest()


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT key, value FROM metadata"))


def _initialize(
    connection: sqlite3.Connection,
    users: list[str],
    chain: ChainConfig,
    block: int,
) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE candidates (
            position INTEGER PRIMARY KEY,
            address TEXT NOT NULL UNIQUE
        );
        CREATE TABLE account_state (
            position INTEGER PRIMARY KEY,
            address TEXT NOT NULL UNIQUE,
            collateral_usd REAL NOT NULL,
            debt_usd REAL NOT NULL,
            avg_liquidation_threshold REAL NOT NULL,
            health_factor REAL NOT NULL
        );
        """
    )
    metadata = {
        "version": "1",
        "chain": chain.name,
        "pool": chain.pool.lower(),
        "block": str(block),
        "users_hash": _users_hash(users),
    }
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
    )
    connection.executemany(
        "INSERT INTO candidates(position, address) VALUES (?, ?)",
        enumerate(users),
    )
    connection.commit()


def _open_cache(
    path: str,
    users: list[str],
    chain: ChainConfig,
    block: int,
) -> tuple[sqlite3.Connection, list[str]]:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    exists = os.path.exists(path)
    connection = sqlite3.connect(path)
    if not exists:
        _initialize(connection, users, chain, block)
        return connection, users

    metadata = _metadata(connection)
    expected = {
        "version": "1",
        "chain": chain.name,
        "pool": chain.pool.lower(),
        "block": str(block),
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            connection.close()
            raise ValueError(
                f"account cache {key} {metadata.get(key)!r} does not match {value!r}"
            )
    cached_users = [
        row[0]
        for row in connection.execute(
            "SELECT address FROM candidates ORDER BY position"
        )
    ]
    if metadata.get("users_hash") != _users_hash(cached_users):
        connection.close()
        raise ValueError("account cache candidate hash is invalid")
    if metadata.get("users_hash") != _users_hash(users):
        connection.close()
        raise ValueError("account cache candidate set does not match the registry")
    return connection, cached_users


def fetch_account_data(
    rpc: EthRpc,
    users: list[str],
    chain: ChainConfig,
    block: int,
    path: str,
    request_batch_size: int = 200,
) -> tuple[list[dict], list[str]]:
    """Fetch every candidate once and resume completed batches after failure."""
    if request_batch_size <= 0:
        raise ValueError("request_batch_size must be positive")
    connection, cached_users = _open_cache(path, users, chain, block)
    try:
        total = len(cached_users)
        while True:
            missing = list(
                connection.execute(
                    """
                    SELECT c.position, c.address
                    FROM candidates c
                    LEFT JOIN account_state a ON a.position = c.position
                    WHERE a.position IS NULL
                    ORDER BY c.position
                    LIMIT ?
                    """,
                    (request_batch_size,),
                )
            )
            if not missing:
                break
            addresses = [row[1] for row in missing]
            rows = aave_v3.account_data(rpc, addresses, chain=chain, block=block)
            connection.executemany(
                """
                INSERT INTO account_state(
                    position, address, collateral_usd, debt_usd,
                    avg_liquidation_threshold, health_factor
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        position,
                        row["address"],
                        row["collateral_usd"],
                        row["debt_usd"],
                        row["avg_liquidation_threshold"],
                        row["health_factor"],
                    )
                    for (position, _), row in zip(missing, rows)
                ],
            )
            connection.commit()
            completed = connection.execute(
                "SELECT COUNT(*) FROM account_state"
            ).fetchone()[0]
            print(f"  account checkpoint {completed:,}/{total:,}")

        output = [
            {
                "address": row[0],
                "collateral_usd": row[1],
                "debt_usd": row[2],
                "avg_liquidation_threshold": row[3],
                "health_factor": row[4],
            }
            for row in connection.execute(
                """
                SELECT address, collateral_usd, debt_usd,
                       avg_liquidation_threshold, health_factor
                FROM account_state
                ORDER BY position
                """
            )
        ]
    finally:
        connection.close()
    return output, cached_users
