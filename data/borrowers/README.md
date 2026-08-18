# Borrower Registries

These files are persistent candidate registries for snapshot construction.
They contain unique lowercase `onBehalfOf` addresses from Aave V3 Pool
`Borrow` events, sorted for deterministic diffs.

| Deployment | Pool proxy blocks covered | Candidates |
|---|---:|---:|
| Ethereum | 16,291,127 to 25,780,402 | 84,427 |
| Linea | 12,430,836 to 31,749,322 | 13,397 |

A registry is not a current borrower book. Snapshot construction re-queries
every candidate with `getUserAccountData` at one pinned block, then stores all
accounts above the configured current-debt floor. Addresses remain in the
registry after repayment so a later borrow does not depend on a rolling event
window.

Use `--registry-out` for routine refreshes so the committed release registry
remains unchanged:

```bash
python -m aave_risk_engine.data.build_snapshot --chain ethereum --asset wstETH --registry-out .runtime/aave_v3_ethereum.json --account-cache-dir .runtime --out .runtime/ethereum-wsteth-live.json
```

The update is incremental and checkpointed. Account reads are block-pinned and
checkpointed separately in SQLite. Rebuilding an older historical block
requires `--rebuild-borrower-registry` and a separate registry path; a newer
registry is rejected rather than silently including future candidates.

The release backfill used keyless public archive endpoints. Known historical
ranges and borrower inclusions were checked across independent endpoints
before the registries were committed. Public RPC remains an operational
dependency for future updates.
