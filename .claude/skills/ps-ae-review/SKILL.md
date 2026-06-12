---
name: ps-ae-review
description: Review a PeopleSoft Application Engine (AE) program. Use when asked to review, explain, or analyze an App Engine / AE_APPLID (e.g. "Review App Engine AR_AGING and connect to TEST"). Connects to the database, extracts the program straight from PeopleTools metadata tables (no XML export), reassembles SQL + PeopleCode, pulls the referenced App Package / FUNCLIB / named-SQL dependencies, and produces a structured review.
---

# PeopleSoft App Engine Review Playbook

## Purpose
When the user says:

> **Review App Engine `<AE_APPLID>` and explain what it does. Connect to '<DB_NAME>' database**

follow this playbook: connect to the database, extract the program straight from the
PeopleTools metadata tables (no XML export needed), reassemble it, and produce a review
in the **Output Format** below.

## Environment
- **Database connection:** use the `<DB_NAME>` the user names in the request (e.g. `TEST`).
  Connect with the `mcp__sqlcl` tools: `mcp__sqlcl__connect` (connection_name = `<DB_NAME>`),
  then `mcp__sqlcl__sql_run`. If no DB is named, **ask the user which database to connect to —
  never assume or default to one.** If already connected to the requested DB, don't reconnect;
  if connected to a *different* DB, confirm before switching.
- **Reference instance — `TEST`:** Oracle 19c, schema **`SYSADM`**, charset UTF8,
  `NLS_DATE_FORMAT = DD-MON-RR`, PeopleSoft **FSCM 9.2 / PeopleTools 8.58**. Other instances
  may differ (tools release, schema, NLS); the `connect` call returns the actual context —
  rely on that rather than assuming these values.
- Tables are owned by `SYSADM`; query them unqualified (the connection user has access).

## Where the program lives (metadata map)

| Table | Holds | Key columns |
|---|---|---|
| `PSAEAPPLDEFN` | Program header | `AE_APPLID`, `DESCR`, `AE_DISABLE_RESTART`, `AEPROGTYPE`, `AE_APPLLIBRARY`, `TEMPTBLINSTANCES`, `MESSAGE_SET_NBR` |
| `PSAESECTDEFN` | Sections | `AE_SECTION`, `AE_SECTION_TYPE`, `AE_PUBLIC_SW` |
| `PSAESTEPDEFN` | Steps (order + flow) **and the Call Section target** | `AE_SECTION`, `AE_STEP`, `AE_SEQ_NUM`, `AE_COMMIT_AFTER`, `AE_DO_APPL_ID`, `AE_DO_SECTION`, `AE_DYNAMIC_DO`, `AE_ON_NOROWS`, `AE_PC_ON_FALSE`, `DESCR` |
| `PSAESTMTDEFN` | Actions per step | `AE_SECTION`, `AE_STEP`, `AE_STMT_TYPE`, `AE_DO_SELECT_TYPE`, `AE_REUSE_STMT`, `SQLID`, `DESCR` |
| `PSSQLTEXTDEFN` | **SQL action text** (CLOB) | `SQLID`, `SQLTYPE`, `SEQNUM`, `SQLTEXT` |
| `PSPCMTXT` | **PeopleCode source** as plain-text CLOB | `OBJECTID1..7` / `OBJECTVALUE1..7`, `PROGSEQ`, `PCTEXT` |

> NOTE: In 8.58, `PSAESTMTDEFN` has **no** `SQLTEXT` column. The SQL action text lives in
> `PSSQLTEXTDEFN`, joined on `SQLID`. PeopleCode is **plain readable text** in
> `PSPCMTXT.PCTEXT` (not compiled bytecode) — long programs are split across `PROGSEQ` rows.
>
> NOTE: `PSAESTMTDEFN` has only these 12 columns — `AE_APPLID, AE_SECTION, MARKET, DBTYPE,
> EFFDT, AE_STEP, AE_STMT_TYPE, AE_REUSE_STMT, AE_DO_SELECT_TYPE, SQLID, DESCR, DESCRLONG`.
> It does **not** hold the Call Section target. For a Call Section step (`AE_STMT_TYPE = 'C'`)
> the target program + section live in **`PSAESTEPDEFN.AE_DO_APPL_ID` + `AE_DO_SECTION`**
> (a blank `AE_DO_APPL_ID` means the call targets the same program). Don't query `PSAESTMTDEFN`
> for `AE_DO_PGM` / `AE_DO_RECNAME` / `AE_DODYNSECTYP` — those columns don't exist there and the
> query will fail with ORA-00904.

### `AE_STMT_TYPE` codes
`P` = PeopleCode · `S` = SQL · `C` = Call Section · `D` = Do Select · `H` = Do When ·
`W` = Do While · `L` = Log Message.

### Action execution order **within a step**
`Do When` → `Do While` → `Do Select` → `PeopleCode` → `SQL`/`Call Section`/`Log Message`.
(A `Do Select` loops the *following* actions in the same step once per fetched row.)

### `PSPCMTXT` key scheme for App Engine (`OBJECTID1 = 66`)
| Field | Meaning | Example |
|---|---|---|
| `OBJECTVALUE1` | AE program | `AP_VCHRBLD` |
| `OBJECTVALUE2` | Section | `C0000` |
| `OBJECTVALUE3` | Market | `GBL` |
| `OBJECTVALUE4` | DB platform | `default` |
| `OBJECTVALUE5` | Effective date | `1900-01-01` |
| `OBJECTVALUE6` | **Step** | `Step01` |
| `OBJECTVALUE7` | Event | `OnExecute` |
| `PROGSEQ` | Chunk # within one program | `0,1,2…` |

### `PSPCMTXT` key scheme for referenced code (also plain text)
Same table, different key layout — query by the **name** in `OBJECTVALUE1` (no need to know `OBJECTID`):

| Referenced object | `OBJECTVALUE1` | `OBJECTVALUE2` | `OBJECTVALUE3` | Example |
|---|---|---|---|---|
| **App Package class** | Package (root) | Class | `OnExecute` (or sub-pkg, then class, then `OnExecute`) | `EXAMPLE_PKG` / `ExampleClass` / `OnExecute` |
| **Record (FUNCLIB) PeopleCode** | Record | Field | Event (`FieldFormula`, `FieldChange`…) | `EXAMPLE_FUNCLIB` / `EXAMPLE_FLD` / `FieldFormula` |

> Deeper packages (`A:B:C`) push the class name into `OBJECTVALUE3`/`4…`; just `ORDER BY OBJECTVALUE2..7, PROGSEQ`
> and reassemble. Long classes are split across `PROGSEQ` rows (e.g. a 30 KB FTP class = 3 chunks).

## Extraction queries (replace `<AE_APPLID>`)

**1. Header**
```sql
SELECT AE_APPLID, DESCR, AE_DISABLE_RESTART, AEPROGTYPE, AE_APPLLIBRARY,
       TEMPTBLINSTANCES, MESSAGE_SET_NBR, AE_DATE_OVERRIDE
FROM   PSAEAPPLDEFN WHERE AE_APPLID = '<AE_APPLID>';
```

**2. Section / step flow (with Call Section targets & Do-When/Do-Select hints)**
```sql
SELECT AE_SECTION, AE_STEP, AE_SEQ_NUM, AE_ACTIVE_STATUS, AE_COMMIT_AFTER,
       AE_DO_APPL_ID, AE_DO_SECTION, AE_DYNAMIC_DO, AE_ON_NOROWS, AE_PC_ON_FALSE, DESCR
FROM   PSAESTEPDEFN WHERE AE_APPLID = '<AE_APPLID>'
ORDER  BY AE_SECTION, AE_SEQ_NUM;
```

**3. Actions per step**
```sql
SELECT AE_SECTION, AE_STEP, AE_STMT_TYPE, AE_DO_SELECT_TYPE, AE_REUSE_STMT, SQLID, DESCR
FROM   PSAESTMTDEFN WHERE AE_APPLID = '<AE_APPLID>'
ORDER  BY AE_SECTION, AE_STEP;
```

**4. SQL action text** (join PSAESTMTDEFN → PSSQLTEXTDEFN; concat by `SEQNUM` if chunked)
```sql
SELECT s.AE_SECTION, s.AE_STEP, s.AE_STMT_TYPE, t.DBTYPE, t.MARKET, t.SEQNUM, t.SQLTEXT
FROM   PSAESTMTDEFN s
JOIN   PSSQLTEXTDEFN t ON t.SQLID = s.SQLID
WHERE  s.AE_APPLID = '<AE_APPLID>'
ORDER  BY s.AE_SECTION, s.AE_STEP, s.AE_STMT_TYPE, t.DBTYPE, t.SEQNUM;
```
(Quick alternative: `SELECT SQLID, DBTYPE, MARKET, SEQNUM, SQLTEXT FROM PSSQLTEXTDEFN WHERE SQLID LIKE '<AE_APPLID>%' ORDER BY SQLID, DBTYPE, SEQNUM;`)

> **Platform/market/effdt variants — don't blindly concatenate.** `PSSQLTEXTDEFN` is keyed by
> `SQLID, SQLTYPE, MARKET, DBTYPE, EFFDT, SEQNUM`. Most AE action SQL is a single row with
> `DBTYPE = ' '` (blank = common/all-platform). But named meta-SQL and some steps carry
> **per-platform overrides**: `DBTYPE` `0`=default, `1`=DB2 z/OS, **`2`=Oracle**, `3`=Informix,
> `4`=DB2/UNIX, `6`=Sybase, `7`=MS SQL Server. If a `SQLID` returns multiple `DBTYPE`s, reassemble
> **only the one relevant to the connected database**: prefer `DBTYPE = '2'` (Oracle), else the default (`' '`/`'0'`) —
> never glue different `DBTYPE`s together (you'd merge Oracle + DB2 + SQL-Server syntax into garbage).
> Likewise pick the program's `MARKET` (usually `GBL`) and the latest `EFFDT <= as-of`. Selecting
> `DBTYPE, MARKET` (above) makes multi-variant cases visible instead of silently doubling the text.

**5. PeopleCode (reassemble by concatenating `PCTEXT` within each identical key)**
```sql
SELECT OBJECTVALUE2 AS SECTION, OBJECTVALUE6 AS STEP, PROGSEQ, PCTEXT
FROM   PSPCMTXT
WHERE  OBJECTVALUE1 = '<AE_APPLID>'
ORDER  BY OBJECTVALUE2, OBJECTVALUE5, OBJECTVALUE6, OBJECTVALUE7, PROGSEQ;
```

> **Variants:** like SQL, PeopleCode can have market (`OBJECTVALUE3`) and platform
> (`OBJECTVALUE4`) overrides. Most AE code is `OBJECTVALUE3 = 'GBL'`, `OBJECTVALUE4 = 'default'`.
> If query 5 returns the *same* section/step under more than one `OBJECTVALUE3/4`, reassemble only
> the `GBL`/`default` rows (or the program's market) — don't concatenate across variants.
>
> **Fallback:** `PSPCMTXT.PCTEXT` stores PeopleCode as plain text across PeopleTools versions,
> so query 5 should always return readable source. If it returns **no rows** or empty `PCTEXT`,
> the cause is almost certainly an input problem, not storage — re-check the exact `<AE_APPLID>`
> spelling/case (`SELECT AE_APPLID FROM PSAEAPPLDEFN WHERE AE_APPLID LIKE '%...%'`), confirm the
> program actually has PeopleCode actions (`AE_STMT_TYPE = 'P'` in query 3), and verify you're on
> the intended `<DB_NAME>`. Only if it's still empty, fall back to an App Designer print listing
> or project XML export for that program.

**6. State record (AET) field types** — needed to judge bind/date correctness
```sql
SELECT table_name, column_name, data_type, data_length
FROM   all_tab_columns
WHERE  table_name = 'PS_<AET_RECORD>'   -- usually PS_<AE_APPLID>_AET
ORDER  BY column_id;
```

**7. Referenced code the AE depends on — pull this by default, don't wait to be asked.**
After reassembling the AE's own PeopleCode (query 5), scan it for dependencies and pull each one,
because the AE's real behavior (config lookups, file transfer, edit/error handling, parsing) usually
lives in these. Look for:
- `import <Pkg>:<Class>;` / `create <Pkg>:<Class>(...)` → **App Package class**
- `Declare Function <fn> PeopleCode <RECORD>.<FIELD> <Event>;` → **record/FUNCLIB PeopleCode**
- `CreateSQL(SQL.<name> ...)` / `SQLExec(SQL.<name> ...)` → **named SQL definition** (in `PSSQLTEXTDEFN`)

```sql
-- 7a. App Package class (or whole package: drop the OBJECTVALUE2 predicate)
SELECT OBJECTVALUE2 AS CLASS, PROGSEQ, PCTEXT
FROM   PSPCMTXT
WHERE  OBJECTVALUE1 = '<PACKAGE>' AND OBJECTVALUE2 = '<CLASS>'
ORDER  BY OBJECTVALUE2, OBJECTVALUE3, OBJECTVALUE4, OBJECTVALUE5, OBJECTVALUE6, OBJECTVALUE7, PROGSEQ;

-- 7b. Record (FUNCLIB) PeopleCode behind a Declare Function
SELECT OBJECTVALUE2 AS FIELD, OBJECTVALUE3 AS EVENT, PROGSEQ, PCTEXT
FROM   PSPCMTXT
WHERE  OBJECTVALUE1 = '<RECORD>' AND OBJECTVALUE2 = '<FIELD>'
ORDER  BY OBJECTVALUE3, PROGSEQ;

-- 7c. Named SQL definition referenced as SQL.<name>  (watch DBTYPE — see the variant note under query 4)
SELECT SQLID, DBTYPE, MARKET, SEQNUM, SQLTEXT FROM PSSQLTEXTDEFN WHERE SQLID = '<SQL_NAME>'
ORDER BY DBTYPE, SEQNUM;   -- if multiple DBTYPEs, read the Oracle ('2') or default (' '/'0') row, not all
```

> **Depth:** pull what the AE actually invokes (the imported classes, the declared functions, the
> named SQL). Follow one level deeper only when that code is itself central to a finding — don't
> recurse through an entire helper library. A class often holds an **old, block-commented (`<* … *>`)
> implementation** alongside the live one; review the active path but flag dead/commented code.
> Use `LENGTH(PCTEXT)` first if a pull might be large, and read big results in chunks.

## Review checklist (what to actually look for)
- **Restart safety:** `AE_DISABLE_RESTART`. If restart is **enabled** but the program holds
  open **File** handles or relies on **Component** variables initialized in an early section,
  a mid-run abend + restart resumes *past* the init step → invalid handles / lost state.
  File-writing report AEs almost always should set **Disable Restart = Yes**. The same hazard
  applies to **irreversible external side effects** (web-service/OIC calls, payments, FTP): a
  restart can re-send work that already left the system — verify idempotency or disable restart.
- **Commits inside loops** (`AE_COMMIT_AFTER`, commit frequency) vs. Do Select / file writes.
- **Date binds:** PeopleSoft **Date** fields are stored/read as `'YYYY-MM-DD'` strings, so
  `%Select(... TO_CHAR(dt,'YYYY-MM-DD') ...)` into a *Date* AET field and `%Bind(dateField)`
  are correct — **verify the AET field is type DATE before flagging** an implicit-conversion
  bug (see query 6). Don't cry wolf on this.
- **Inner vs outer joins** that can silently drop rows (e.g. effective-dated address joins) —
  do "variant A" and "variant B" reports stay reconcilable?
- **Shared staging / concurrency:** if `TEMPTBLINSTANCES = 0` the program uses real shared
  tables — check that "tag/claim" UPDATEs are scoped (e.g. `process_instance = 0`) so concurrent
  runs can't steal each other's rows.
- **Performance:** row-by-row `Do Select` + per-row PeopleCode over large tables
  (`PS_ITEM_ACTIVITY`, `PS_ITEM`, ledger/journal tables); `OR` predicates across columns that
  defeat indexes; missing `%Bind` / hard-coded literals; functions on columns (`upper(col)=`).
- **Effective-date / SetID logic** correctness; `%Bind` vs literal SQL injection risk.
- **Hard-coded literals** (SetID/prefix strings, BU codes) baked into SQL; arbitrary `MAX()`
  picks when a mapping join can return several rows.
- **Referenced App Package / FUNCLIB code (query 7):** review the live path of each imported class
  and declared function the AE calls. Common finds: `getRequiredValue`-style helpers that **abend on
  missing config**, parsing/clean-up functions with subtle bugs, file-transfer classes that build
  shell commands (`Exec`) or embed credentials, code that **writes status/control fields on another
  record without re-validating** (e.g. forcing a voucher Postable over a closed period), and **old
  block-commented implementations** left beside the active one. Attribute bugs to the dependency,
  not the AE, and say which AE step triggers them.
- **Loose ends:** unused `TEMPTBLINSTANCES`, unguarded file `Close`/`WriteLine`, `AE_ON_NOROWS`,
  inactive/`**OBSOLETE**` steps, and speculative code paths that are coded but never configured.

## Output Format (match this structure)
1. **What it does** — 1 short paragraph; if run-control flags drive branching, add a small
   table mapping flag → section → behavior.
2. **Flow** — `MAIN` and each called section, with the action(s) per step in plain language.
3. **Issues identified, highest impact first** — restart/reliability, then correctness, then
   performance, then minor/robustness. Be concrete; cite the section/step. Include findings from
   referenced App Package / FUNCLIB code (query 7), attributing each to its dependency and the AE
   step that triggers it.
4. **Net** — the one or two things to fix first, and offer to write up a formal review doc.

> Pulling referenced App Package / FUNCLIB / SQL-definition code (query 7) is a **default step**, not
> an offer — the dependencies are part of the program's real behavior. Only skip it for a trivial AE
> with no `import` / `Declare Function` / `SQL.<name>` references.

Keep it brief and concrete. Verify before asserting a bug (especially date/bind issues).
Save extracted source to a `<APPLID>/` subfolder only if the user asks.
