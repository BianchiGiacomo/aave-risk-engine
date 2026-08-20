# Run Manifests

The August 18 pair is the `v0.1.0` publication evidence. It uses complete
Borrow-event registries from each configured Pool proxy deployment and records
the registry range and candidate count with the snapshot hash, block,
parameters, seed, results, cap sweep, and clearance test.

The July 30 and August 17 pairs preserve earlier research vintages. Their
rolling borrower discovery was incomplete, so their borrower-book totals must
not be interpreted as a like-for-like market time series. Their direct reserve
state and routed-depth observations remain useful historical evidence.

`ethereum-wsteth-time-to-exit-2026-08-18.json` is a deterministic analysis of
the publication snapshot. It records refill, redemption, delay, drawdown,
horizon, capacity, and required-throughput assumptions. Its `$25m/day`
redemption input is an illustrative sensitivity, not a live Lido queue
measurement.

## Verifying A Snapshot Hash

Each manifest records the SHA-256 of the snapshot file it consumed. For the
August 18 pair in the `v0.1.0` release tree:

```bash
git show v0.1.0:data/snapshots/aave_v3_ethereum_wsteth.json | sha256sum
```

The repository pins LF line endings through `.gitattributes`, and snapshot,
registry, episode, and manifest writers emit LF explicitly. The hash is
therefore stable before and after staging on Linux, macOS, and Windows.

Earlier vintages overwrote the same two snapshot paths, so their inputs live in
git history rather than in the working tree:

| Manifest | Snapshot commit | Block |
|---|---|---:|
| `ethereum-wsteth-2026-07-30` | `32a1eb1b3` | 25,645,558 |
| `linea-weth-2026-07-30` | `32a1eb1b3` | 31,568,531 |
| `ethereum-wsteth-2026-08-17` | `cd7c0157c` | 25,773,934 |
| `linea-weth-2026-08-17` | `cd7c0157c` | 31,741,470 |

```bash
git show 32a1eb1b3:data/snapshots/aave_v3_ethereum_wsteth.json | sha256sum
```

Manifests generated before `.gitattributes` was added recorded the SHA-256 of a
Windows CRLF checkout, which does not match the LF content stored in git. Those
four hashes were corrected to the LF value of the same commit and block; no
other manifest field was touched.
