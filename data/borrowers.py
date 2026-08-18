"""Persistent, complete Borrow-event registries for Aave V3 deployments."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from .aave_v3 import ChainConfig, discover_borrowers
from .rpc import EthRpc


@dataclass
class BorrowerRegistry:
    """All `onBehalfOf` addresses observed since Pool deployment."""

    version: int
    chain: str
    pool: str
    from_block: int
    to_block: int
    users: list[str]


def default_registry_path(chain: str) -> str:
    return os.path.join(os.path.dirname(__file__), "borrowers", f"aave_v3_{chain}.json")


def load_registry(path: str, chain: ChainConfig | None = None) -> BorrowerRegistry:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    registry = BorrowerRegistry(**raw)
    if registry.version != 1:
        raise ValueError(f"unsupported borrower registry version {registry.version}")
    empty_initial = (
        registry.to_block == registry.from_block - 1 and not registry.users
    )
    if registry.from_block > registry.to_block and not empty_initial:
        raise ValueError("borrower registry block range is inverted")
    if registry.users != sorted(set(address.lower() for address in registry.users)):
        raise ValueError("borrower registry users must be unique, lowercase, and sorted")
    if chain is not None:
        if registry.chain != chain.name:
            raise ValueError(
                f"borrower registry chain {registry.chain!r} does not match {chain.name!r}"
            )
        if registry.pool.lower() != chain.pool.lower():
            raise ValueError("borrower registry Pool address does not match chain config")
        if registry.from_block != chain.borrower_registry_start_block:
            raise ValueError(
                "borrower registry start does not match the configured Pool deployment"
            )
    return registry


def save_registry(
    registry: BorrowerRegistry, path: str, allow_regression: bool = False
) -> None:
    """Atomically persist a registry beside its destination file."""
    if os.path.exists(path) and not allow_regression:
        with open(path, encoding="utf-8") as fh:
            current_to_block = int(json.load(fh)["to_block"])
        if current_to_block > registry.to_block:
            raise RuntimeError(
                f"refusing to move borrower registry backward from "
                f"{current_to_block:,} to {registry.to_block:,}"
            )
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(asdict(registry), fh, indent=1)
    os.replace(temp_path, path)


def update_registry(
    rpc: EthRpc,
    chain: ChainConfig,
    to_block: int,
    path: str,
    chunk_blocks: int | None = None,
    pause_s: float = 0.0,
    rebuild: bool = False,
    checkpoint_chunks: int = 100,
) -> BorrowerRegistry:
    """Backfill or increment a complete borrower registry through `to_block`."""
    chunk = chunk_blocks or chain.log_chunk_blocks
    if chunk <= 0:
        raise ValueError("chunk_blocks must be positive")
    if checkpoint_chunks <= 0:
        raise ValueError("checkpoint_chunks must be positive")

    existing = None
    if os.path.exists(path) and not rebuild:
        existing = load_registry(path, chain)
    if existing is None:
        registry = BorrowerRegistry(
            version=1,
            chain=chain.name,
            pool=chain.pool.lower(),
            from_block=chain.borrower_registry_start_block,
            to_block=chain.borrower_registry_start_block - 1,
            users=[],
        )
    else:
        registry = existing

    if registry.to_block > to_block:
        raise ValueError(
            f"borrower registry already extends through {registry.to_block:,}; "
            f"use --rebuild-borrower-registry with a separate registry path "
            f"to build historical block {to_block:,}"
        )
    if registry.to_block == to_block:
        return registry

    users = set(registry.users)
    start = registry.to_block + 1
    chunks_since_checkpoint = 0
    for chunk_start in range(start, to_block + 1, chunk):
        chunk_end = min(chunk_start + chunk - 1, to_block)
        users.update(
            discover_borrowers(
                rpc,
                chunk_start,
                chunk_end,
                chunk_blocks=chunk,
                pause_s=pause_s,
                chain=chain,
            )
        )
        registry.to_block = chunk_end
        chunks_since_checkpoint += 1
        if chunks_since_checkpoint >= checkpoint_chunks:
            registry.users = sorted(users)
            save_registry(registry, path, allow_regression=rebuild)
            print(
                f"  registry checkpoint {registry.to_block:,}: "
                f"{len(registry.users):,} borrowers"
            )
            chunks_since_checkpoint = 0

    registry.users = sorted(users)
    save_registry(registry, path, allow_regression=rebuild)
    return registry
