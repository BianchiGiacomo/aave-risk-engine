# RWA Proxy Data

`hyg_adjusted_close_2016-07-01_2026-07-15.csv` contains 2,522 dated HYG
adjusted-close observations downloaded from the Yahoo Finance chart endpoint
on August 31, 2026. Its adjacent metadata file records the exact request,
field, sample, retrieval timestamp, and source links.

The series is committed to reproduce the RWA drawdown stress offline. HYG is
a market-price proxy for high-yield credit. It is not HINC NAV, does not
contain the J.P. Morgan CLOIE Post-BB component, and must not be presented as
the exact 70/30 blend described in the HINC proposal.

Four-session returns use five observations: adjusted close at session `t+4`
divided by adjusted close at session `t`, minus one. Warehouse funding uses
the actual calendar days between those two dates.
