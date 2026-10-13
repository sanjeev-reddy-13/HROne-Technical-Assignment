# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees and unique `(emp_code, date)` on logs: they block duplicates and serve per-employee queries. `(date desc, emp_code)` serves the list sort and month scans; `(status, date desc, emp_code)` serves the status filter; `(department, emp_code)` and `joined_on` serve employee lists and headcount. Rejected: an index on `late_minutes`; the month filter already narrows the data and it is not selective.

2. **Punch-in race.** Both requests compute the same attendance date and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I return as 409. There is no read-before-write.

3. **Ties.** `$rank` gives tied employees the same rank, and I keep every row with `rank <= limit`. So everyone tied at the cutoff is returned and the list can be longer than `limit`.

4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end) and `$lookup`s each person's logs. Someone with no logs gets an empty lookup, but their row stays, so they are still counted.

5. **100x data.** I would keep pre-aggregated daily totals per employee and department, updated on every write, so analytics read small rollup documents instead of scanning logs.
