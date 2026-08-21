---
name: ps-app-engine-review
description: Review a PeopleSoft Application Engine (AE) program. Use when asked to review, explain, or analyze an App Engine / AE_APPLID (e.g. "Review App Engine AR_AGING and connect to TEST"). Connects to the database, extracts the program straight from PeopleTools metadata tables (no XML export), reassembles SQL + PeopleCode, pulls the referenced dependencies (App Package, FUNCLIB, named SQL, File Layout, Component Interface, Process Definition, Message Catalog, URL, Integration Broker), and produces a structured review.
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
- **Confirm columns before querying any table — never guess them.** This applies to `PS_<REC>`
  **data records** AND to **PeopleTools catalog tables** (`PSBCDEFN`, `PSPNLGRPDEFN`, `PSPNLFIELD`,
  `PSRECGROUP`, `PSPRSMDEFN`, `PSPCMPROG`, …) — don't rely on remembered column names for the metadata
  tables just because they're queried often. Resolve the real columns first:
  `SELECT column_name, data_type, nullable FROM all_tab_columns WHERE table_name = '<TABLE>' ORDER BY column_id;`
  (or `DESCRIBE`), then write the SELECT. Never guess — it wastes turns on `ORA-00904`; also confirm
  the table name to avoid `ORA-00942` (many PeopleTools tables are `PS_<NAME>`, not `PS<NAME>` — e.g.
  `PS_PRCSDEFN`, not `PSPRCSDEFN`). Derived/Work records and views return no rows — fall back to
  `PSRECFIELD`/`PSDBFIELD` (PeopleTools field metadata) or `all_views`. This is the same lookup as
  query 6 does for the AET state record — apply it to every table, not just the AET.
- **Server-side logs (when DB metadata isn't enough):** if a program's own logging is too sparse to
  explain a failure (see the *Restart safety* / *Loose ends* checks below), the real run logs live on
  the App Server / Process Scheduler host (Unix), not in the database. Fetch them from that host —
  the connection method (SSH / PuTTY session, etc.) is environment-specific; don't hardcode hosts or keys.

## Where the program lives (metadata map)

| Table | Holds | Key columns |
|---|---|---|
| `PSAEAPPLDEFN` | Program header | `AE_APPLID`, `DESCR`, `AE_DISABLE_RESTART`, `AEPROGTYPE`, `AE_APPLLIBRARY`, `TEMPTBLINSTANCES`, `MESSAGE_SET_NBR` |
| `PSAESECTDEFN` | Sections | `AE_SECTION`, `AE_SECTION_TYPE`, `AE_PUBLIC_SW` |
| `PSAESTEPDEFN` | Steps (order + flow) **and the Call Section target** | `AE_SECTION`, `AE_STEP`, `AE_SEQ_NUM`, `AE_COMMIT_AFTER`, `AE_DO_APPL_ID`, `AE_DO_SECTION`, `AE_DYNAMIC_DO`, `AE_ON_NOROWS`, `AE_PC_ON_FALSE`, `DESCR` |
| `PSAESTMTDEFN` | Actions per step | `AE_SECTION`, `AE_STEP`, `AE_STMT_TYPE`, `AE_DO_SELECT_TYPE`, `AE_REUSE_STMT`, `SQLID`, `DESCR` |
| `PSSQLTEXTDEFN` | **SQL action text** (CLOB) | `SQLID`, `SQLTYPE`, `SEQNUM`, `SQLTEXT` |
| `PSPCMTXT` | **PeopleCode source** as plain-text CLOB | `OBJECTID1..7` / `OBJECTVALUE1..7`, `PROGSEQ`, `PCTEXT` |
| `PS_PTAE_ACT_PLUGIN` | **AE Action Plugins** — steps whose action is overridden by a custom AE without touching the vanilla program (8.58 feature) | target: `AE_APPLID`,`PTAE_SECTION`,`PTAE_STEP`,`PTAE_ACTION_TYPE` · plugin: `PTAE_PLUG_APPLID`,`PTAE_PLUG_SECTION`,`PTAE_PLUG_STEP`,`PTAE_PLUG_ACTNTYPE` · `PTAE_PLUG_MODE`,`PTAE_MODE_SEQ`,`ENABLED` |

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
`W` = Do While · `N` = Do Until · `M` = Log Message · `X` = XSLT (transform programs only,
`AEPROGTYPE = '4'`).

### Action execution order **within a step**
`Do When` → `Do While` → `Do Select` → `PeopleCode` → `SQL`/`Call Section`/`Log Message`.
(A `Do Select` loops the *following* actions in the same step once per fetched row.)

### AE Action Plugins — the vanilla step you read may **not** be what runs
An **AE Action Plugin** (`PS_PTAE_ACT_PLUGIN`, PeopleTools 8.58) lets a site override a
**delivered** step's action with a step from a **custom** AE *without modifying the vanilla
program*. At runtime PeopleSoft substitutes the plugin's action for (or around) the delivered
one. This is invisible in `PSAESTMTDEFN`/`PSSQLTEXTDEFN` — the delivered SQL/PeopleCode still
sits there unchanged — so **reviewing only the delivered action gives the wrong answer.** Always
run the plugin check (query 1b) before drawing conclusions about any step's behavior.

- **Target** (delivered action being overridden): `AE_APPLID` + `PTAE_SECTION` + `PTAE_STEP` +
  `PTAE_ACTION_TYPE`.
- **Plugin** (custom action that runs instead/alongside): `PTAE_PLUG_APPLID` +
  `PTAE_PLUG_SECTION` + `PTAE_PLUG_STEP` + `PTAE_PLUG_ACTNTYPE` — extract this program's action
  with the normal queries (4/5) and review it as the real logic.
- **`PTAE_PLUG_MODE`**: `R` = **Replace** (delivered action is skipped), `B` = **Before**,
  `A` = **After** (plugin runs in addition to the delivered one). `PTAE_MODE_SEQ` orders multiple
  Before/After plugins on the same action.
- **Action-type codes** (`PTAE_ACTION_TYPE` / `PTAE_PLUG_ACTNTYPE`): `S` = SQL, `P` = PeopleCode.
- **`ENABLED`** `Y`/`N` — a disabled row is configured but not active; note it, don't treat it as live.
- **Two directions to check:** (1) is the AE under review a *target* (its steps get overridden)?
  (2) is it a *plugin* (`PTAE_PLUG_APPLID`) whose sections are injected into some delivered AE —
  in which case it isn't run standalone and only makes sense in that host's context?

> Typical shape: delivered `AR_AGING` steps `DBUPDT.RSET_ITM` and `UPD_SUMC.DEL_EXST` are both
> **Replaced** by a custom plugin AE's `MAIN.Step01`/`Step02`. Reviewing the delivered AR_AGING SQL
> for those two steps is meaningless — the plugin's SQL is what executes (and is where e.g. an
> intentional PARALLEL hint actually lives).

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

### Other object types an AE can reference (definition tables)
An AE is rarely self-contained. When the reassembled SQL/PeopleCode references one of these,
pull its **definition** too — the behaviour under review often lives there, not in the AE.
Columns below are **verified on FSCM 9.2 / PT 8.58**; queries are in section 8.

| Referenced from AE code | Object (App Designer type) | Header table | Detail tables |
|---|---|---|---|
| `SetFileLayout(FileLayout.X)` | File Layout (31) | `PSFLDDEFN` | `PSFLDSEGDEFN`, `PSFLDFIELDDEFN` |
| `GetCompIntfc(CompIntfc.X)` | Component Interface (32) | `PSBCDEFN` | `PSBCITEM`; method code in `PSPCMTXT` |
| `import`/`create <Pkg>:<Cls>` | Application Package (57) | `PSPACKAGEDEFN` | `PSAPPCLASSDEFN`; code in `PSPCMTXT` |
| `ProcessRequest`, or the AE's own scheduler entry | Process Definition (20) | `PS_PRCSDEFN` | `PS_PRCSJOBDEFN` (jobs) |
| `MessageBox`/`%MsgGet`/Log Message action, header `MESSAGE_SET_NBR` | Message Catalog (25) | `PSMSGCATDEFN` | — |
| `URL.X` / `GetURL(URL.X)` | URL (56) | `PSURLDEFN` | — |
| `SQLExec(SQL.X)` / `CreateSQL(SQL.X)` | SQL Object (30) | `PSSQLDEFN` | `PSSQLTEXTDEFN` (text) |
| `%IntBroker.Publish`, `SyncRequest`, AE used as a handler | IB Service / Operation / Message / Node / Routing | `PSSERVICE`, `PSOPERATION`, `PSMSGDEFN`, `PSMSGNODEDEFN`, `PSIBRTNGDEFN` | `PSSERVICEOPR`, `PSOPERATIONAE`/`AC`/`CI`, `PSMSGREC`, `PSMSGPARTS` |
| Records read/written by the AE's SQL | Record (0) / Field (2) | `PSRECDEFN` | `PSRECFIELD`, `PSDBFIELD` |
| Hard-coded status/flag codes in SQL | Translate Values (4) | `PSXLATITEM` | — |
| Access paths behind a slow step | Index (1) | `PSINDEXDEFN` | `PSKEYDEFN` |

> **Deliberately out of scope for this skill:** Page (5), Menu (6), Component (7), Page PeopleCode
> (44), Portal Registry/CRef (55), Related Content (111/112/113). These are online-only objects an
> AE cannot invoke — they belong to a whole-project review, not an AE review.

## Extraction queries (replace `<AE_APPLID>`)

**1. Header**
```sql
SELECT AE_APPLID, DESCR, AE_DISABLE_RESTART, AEPROGTYPE, AE_APPLLIBRARY,
       TEMPTBLINSTANCES, MESSAGE_SET_NBR, AE_DATE_OVERRIDE
FROM   PSAEAPPLDEFN WHERE AE_APPLID = '<AE_APPLID>';
```

**1b. AE Action Plugins — run this early; it can change which SQL/PeopleCode you review**
```sql
-- (a) Is this program's action overridden by a plugin? (it's the TARGET)
SELECT PTAE_SECTION, PTAE_STEP, PTAE_ACTION_TYPE, ENABLED,
       PTAE_PLUG_APPLID, PTAE_PLUG_SECTION, PTAE_PLUG_STEP, PTAE_PLUG_ACTNTYPE,
       PTAE_PLUG_MODE, PTAE_MODE_SEQ, DESCR
FROM   PS_PTAE_ACT_PLUGIN
WHERE  AE_APPLID = '<AE_APPLID>'
ORDER  BY PTAE_SECTION, PTAE_STEP, PTAE_MODE_SEQ, SEQNBR;

-- (b) Is this program itself a plugin injected into some delivered AE? (it's the PLUGIN)
SELECT AE_APPLID AS TARGET_AE, PTAE_SECTION, PTAE_STEP, PTAE_ACTION_TYPE,
       PTAE_PLUG_SECTION, PTAE_PLUG_STEP, PTAE_PLUG_MODE, ENABLED, DESCR
FROM   PS_PTAE_ACT_PLUGIN
WHERE  PTAE_PLUG_APPLID = '<AE_APPLID>'
ORDER  BY AE_APPLID, PTAE_SECTION, PTAE_STEP;
```
For every **enabled** row from (a) with mode `R` (Replace), skip the delivered action at that
section/step and instead extract + review `PTAE_PLUG_APPLID.PTAE_PLUG_SECTION.PTAE_PLUG_STEP`
via queries 4/5. For mode `B`/`A` (Before/After), review **both** the delivered action and the
plugin action (they both run, in `PTAE_MODE_SEQ` order).

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

First get the program's *actual* state records from `PSAEAPPLSTATE` — do **not** guess
`PS_<AE_APPLID>_AET`. A program often has several state records, none of which need match the
program name, and only the `AE_DEFAULT_STATE = 'Y'` row is the default record that unqualified
state references resolve to:
```sql
SELECT AE_APPLID, AE_STATE_RECNAME, AE_DEFAULT_STATE
FROM   PSAEAPPLSTATE
WHERE  AE_APPLID = '<AE_APPLID>';
```
Then read the field types of each one returned:
```sql
SELECT table_name, column_name, data_type, data_length
FROM   all_tab_columns
WHERE  table_name = 'PS_<AE_STATE_RECNAME>'
ORDER  BY column_id;
```

**7. Referenced code the AE depends on — pull this by default, don't wait to be asked.**
After reassembling the AE's own PeopleCode (query 5), scan it for dependencies and pull each one,
because the AE's real behavior (config lookups, file transfer, edit/error handling, parsing) usually
lives in these. Look for:
- `import <Pkg>:<Class>;` / `create <Pkg>:<Class>(...)` → **App Package class**
- `Declare Function <fn> PeopleCode <RECORD>.<FIELD> <Event>;` → **record/FUNCLIB PeopleCode**
- `CreateSQL(SQL.<name> ...)` / `SQLExec(SQL.<name> ...)` → **named SQL definition** (in `PSSQLTEXTDEFN`)
- References to the **Strings Table** (`PS_STRINGS_TBL`, delivered) → resolve the actual text so the
  logic reads concretely. Most AEs key on `PROGRAM_ID + STRING_ID`; the value is in `STRING_TEXT`.
- `FileLayout.<name>` · `CompIntfc.<name>` · `URL.<name>` · `Message.<name>` · `MessageBox`/`%MsgGet`
  message sets → **other object definitions** — pull them with query 8 (same default-step rule).

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

-- 7d. Strings Table value referenced by the AE (delivered PS_STRINGS_TBL)
--     Full key is PROGRAM_ID + STRING_ID + LABEL_ID — select LABEL_ID so multiple labels are visible.
SELECT PROGRAM_ID, STRING_ID, LABEL_ID, STR_LBLTYPE, DEFAULT_LABEL, STRING_TEXT
FROM   PS_STRINGS_TBL WHERE PROGRAM_ID = '<PGM>' AND STRING_ID = '<ID>';
-- Dump everything one program uses:  ... WHERE PROGRAM_ID = '<PGM>' ORDER BY STRING_ID, LABEL_ID;
```

> **Depth:** pull what the AE actually invokes (the imported classes, the declared functions, the
> named SQL). Follow one level deeper only when that code is itself central to a finding — don't
> recurse through an entire helper library. A class often holds an **old, block-commented (`<* … *>`)
> implementation** alongside the live one; review the active path but flag dead/commented code.
> Use `LENGTH(PCTEXT)` first if a pull might be large, and read big results in chunks.

**8. Other referenced object definitions** — run the ones the AE actually references (see the
*Other object types* table above). Columns verified on FSCM 9.2 / PT 8.58; still re-check with
`all_tab_columns` for anything not listed here.

```sql
-- 8a. File Layout referenced as FileLayout.<X> — header, segment hierarchy, field map
SELECT FLDDEFNNAME, DESCR, FLDFORMAT, FLDDELIMITER, FLDFILENAME, FLDSEGCOUNT,
       SUBSTR(DESCRLONG,1,500) AS DESCRLONG
FROM   PSFLDDEFN WHERE FLDDEFNNAME = '<LAYOUT>';

SELECT FLDSEGNAME, FLDSEGID, FLDSEGPARENT, FLDSEGIDSTART, FLDSEGIDLENGTH,
       RECNAME_FILE, FLDSEQNO
FROM   PSFLDSEGDEFN WHERE FLDDEFNNAME = '<LAYOUT>' ORDER BY FLDSEQNO;

SELECT FLDSEGNAME, FLDFIELDNAME, FLDSTART, FLDLENGTH, FLDFIELDTYPE,
       DECIMAL_POS, FLDTRIMSPACES, FLDSEQNO
FROM   PSFLDFIELDDEFN WHERE FLDDEFNNAME = '<LAYOUT>' ORDER BY FLDSEGNAME, FLDSEQNO;
```
> `FLDFIELDTYPE` `0`=char, `2`=number (strips leading zeros/spaces). Read this field map instead of
> eyeballing a sample file. A personal/dev-share path left in `FLDFILENAME` is a finding.

```sql
-- 8b. Component Interface referenced as CompIntfc.<X> (internal name = Business Component)
SELECT BCNAME, BCDISPLAYNAME, BCPGNAME, MARKET, MENUNAME,
       SEARCHRECNAME, ADDSRCHRECNAME, ITEMCOUNT, DESCR
FROM   PSBCDEFN WHERE BCNAME = '<CI>';

SELECT BCTYPE, BCITEMPARENT, BCITEMNAME, BCACCESS, BCSCROLLNAME,
       RECNAME, FIELDNAME, SEQUENCE_NBR_6
FROM   PSBCITEM WHERE BCNAME = '<CI>' ORDER BY BCITEMPARENT, SEQUENCE_NBR_6;
```
> The CI drives the online component's PeopleCode, so an AE using a CI inherits the component's
> defaulting and edits. `BCACCESS` shows read-only vs read/write properties — a property the AE
> sets that the CI exposes read-only silently does nothing. CI **method** PeopleCode is in
> `PSPCMTXT` (query by `OBJECTVALUE1 = '<CI>'`).

```sql
-- 8c. Application Package hierarchy + classes (complements the code pull in 7a)
SELECT PACKAGEROOT, QUALIFYPATH, PACKAGELEVEL, DESCR
FROM   PSPACKAGEDEFN WHERE PACKAGEROOT = '<PACKAGE>' ORDER BY QUALIFYPATH, PACKAGELEVEL;

SELECT PACKAGEROOT, QUALIFYPATH, APPCLASSID, DESCR
FROM   PSAPPCLASSDEFN WHERE PACKAGEROOT = '<PACKAGE>' ORDER BY QUALIFYPATH, APPCLASSID;
```
> `QUALIFYPATH = ':'` means the class sits at the package root; anything else is the sub-package path.

```sql
-- 8d. Process Definition for this AE (and any job that runs it)
SELECT PRCSTYPE, PRCSNAME, RESTARTENABLED, MAXCONCURRENT, RUNLOCATION, SERVERNAME,
       PARMLIST, TIMEOUTMINUTES, TIMEOUTMAXMINS, RETRYCOUNT, PRCSCATEGORY,
       OUTDESTTYPE, OUTDEST, DESCR
FROM   PS_PRCSDEFN WHERE PRCSNAME = '<AE_APPLID>';

SELECT PRCSJOBNAME, PRCSTYPE, DESCR, JOBRUNMODE, MAXCONCURRENT, PRCSCATEGORY
FROM   PS_PRCSJOBDEFN WHERE PRCSJOBNAME = '<JOB>';
```
> **Cross-check restart in both places.** `PSAEAPPLDEFN.AE_DISABLE_RESTART` (the program) and
> `PS_PRCSDEFN.RESTARTENABLED` (the scheduler entry) are separate switches — a program that
> intends "no restart" but sits behind `RESTARTENABLED = '1'` is a finding. Also check
> `MAXCONCURRENT` against the shared-staging concern below (`MAXCONCURRENT = 0` = unlimited).

```sql
-- 8e. Message Catalog entries the AE emits (header MESSAGE_SET_NBR, MessageBox/%MsgGet, Log Message)
SELECT MESSAGE_SET_NBR, MESSAGE_NBR, MESSAGE_TEXT, MSG_SEVERITY,
       SUBSTR(DESCRLONG,1,500) AS DESCRLONG
FROM   PSMSGCATDEFN WHERE MESSAGE_SET_NBR = <SET> AND MESSAGE_NBR IN (<NBRS>)
ORDER  BY MESSAGE_NBR;
```
> Resolve every message the AE raises — the text is what an operator sees, and a `%1/%2` count
> that doesn't match the call's parameters produces a useless log line.

```sql
-- 8f. URL definition referenced as URL.<X>
SELECT URL_ID, URL, DESCR, ICLIENT_SERVERFLAG, COMMENTS FROM PSURLDEFN WHERE URL_ID = '<URL_ID>';
```
> Flag credentials embedded in the `URL` value, and an environment-specific host hard-coded where
> a per-environment URL definition (or config record) should be used.

```sql
-- 8g. Named SQL object header (the text itself is query 7c)
SELECT SQLID, SQLTYPE, ENABLEEFFDT, LASTUPDOPRID, LASTUPDDTTM FROM PSSQLDEFN WHERE SQLID = '<SQL_NAME>';
```

```sql
-- 8h. Integration Broker — is this AE a service-operation handler, and what does it publish?
SELECT IB_OPERATIONNAME, HANDLERNAME, AE_APPLID, PACKAGEROOT, APPCLASSID, APPCLASSMETHOD
FROM   PSOPERATIONAE WHERE AE_APPLID = '<AE_APPLID>';   -- AE invoked as a handler

SELECT IB_OPERATIONNAME, VERSION, DEFAULTVER, RTNGTYPE, IB_SERVICENAME,
       MSGNAME, IB_MSGVERSION, IB_REQUESTSTATUS, IB_THINKTIME, DESCR
FROM   PSOPERATION WHERE IB_OPERATIONNAME = '<OPERATION>';

SELECT MSGNAME, VERSION, XMLALIAS, MSGSTATUS, DEFAULTVER, DESCR
FROM   PSMSGDEFN WHERE MSGNAME = '<MESSAGE>';
SELECT MSGNAME, APMSGVER, RECNAME, PRNTRECNAME, SEQNO
FROM   PSMSGREC WHERE MSGNAME = '<MESSAGE>' ORDER BY SEQNO;     -- rowset-based structure

SELECT ROUTINGDEFNNAME, EFF_STATUS, SENDERNODENAME, RECEIVERNODENAME, RTNGTYPE,
       IB_OPERATIONNAME, CONNGATEWAYID, CONNID
FROM   PSIBRTNGDEFN WHERE IB_OPERATIONNAME = '<OPERATION>' ORDER BY EFFDT DESC;

SELECT MSGNODENAME, DESCR, ACTIVE_NODE, LOCALNODE, NODE_TYPE, IB_TGTLOCATION, CONNID
FROM   PSMSGNODEDEFN WHERE MSGNODENAME = '<NODE>';
```
> Handler tables by type: `PSOPERATIONAE` (App Engine), `PSOPERATIONAC` (app class),
> `PSOPERATIONCI` (Component Interface). An AE found in `PSOPERATIONAE` is **not run standalone** —
> review it in the context of that operation's inbound message. An inactive node
> (`ACTIVE_NODE = 'N'`) or an `EFF_STATUS = 'I'` routing means the publish silently goes nowhere.

```sql
-- 8i. Record / field definitions behind the AE's SQL (record type, temp tables, xlat codes)
SELECT RECNAME, RECTYPE, SQLTABLENAME, FIELDCOUNT, PARENTRECNAME,
       SUBSTR(DESCRLONG,1,500) AS DESCRLONG
FROM   PSRECDEFN WHERE RECNAME IN (<RECNAMES>);        -- RECTYPE 0=SQL table, 1=view, 7=temp table

SELECT FIELDNAME, FIELDTYPE, LENGTH, DECIMALPOS FROM PSDBFIELD WHERE FIELDNAME = '<FIELD>';

SELECT FIELDNAME, FIELDVALUE, EFFDT, EFF_STATUS, XLATLONGNAME
FROM   PSXLATITEM WHERE FIELDNAME = '<FIELD>' ORDER BY FIELDVALUE, EFFDT;
```
> `PSRECDEFN` has **no `DESCR` column** — the long comment is `DESCRLONG` (`RECDESCR` is a
> different flag). `RECTYPE = 7` confirms a record really is a temp table (cross-check against
> `TEMPTBLINSTANCES` on the header). Resolve hard-coded status literals in AE SQL against
> `PSXLATITEM` so the logic reads concretely, and flag codes that no longer exist or are
> `EFF_STATUS = 'I'`.

```sql
-- 8j. Indexes on a table a slow step hits — does the WHERE clause have a usable access path?
SELECT I.RECNAME, I.INDEXID, I.INDEXTYPE, I.UNIQUEFLAG, I.ACTIVEFLAG, I.PLATFORM_ORA,
       K.KEYPOSN, K.FIELDNAME, K.ASCDESC
FROM   PSINDEXDEFN I JOIN PSKEYDEFN K ON K.RECNAME = I.RECNAME AND K.INDEXID = I.INDEXID
WHERE  I.RECNAME = '<RECNAME>' ORDER BY I.INDEXID, K.KEYPOSN;
```
> `PLATFORM_ORA = 1` means the index is built on Oracle. Use this to back a performance finding
> with evidence rather than asserting "no index" — and check `ACTIVEFLAG` before assuming one exists.

## Review checklist (what to actually look for)
- **AE Action Plugins (query 1b) — check first.** If any step is overridden by a plugin, the
  delivered SQL/PeopleCode for that step is *not* what runs. Review the plugin's action as the real
  logic (Replace) or both actions (Before/After), and flag: a plugin that silently changes delivered
  behavior, a `Replace` that drops important delivered logic, an enabled plugin pointing at a missing
  section/step, or a disabled plugin someone expects to be live. This is easy to miss — a delivered AE
  can look completely stock while a custom plugin quietly rewrites two of its steps.
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
- **Referenced object definitions (query 8):** review the definition of every non-code object the AE
  touches, not just its PeopleCode. Common finds:
  - **File Layout (8a):** field positions/lengths in `PSFLDFIELDDEFN` that don't match what the AE
    parses or what the trading partner sends; a numeric (`FLDFIELDTYPE = 2`) field the code treats as
    char; a hard-coded personal/dev-share path in `FLDFILENAME`; a segment the AE never handles.
  - **Component Interface (8b):** the CI runs the online component's PeopleCode, so an AE using one
    inherits its defaults and edits. Check that every property the AE sets exists and is writable
    (`BCACCESS`), and that a missing default (a property never populated) can't abend the save.
  - **Process Definition (8d):** `PS_PRCSDEFN.RESTARTENABLED` vs `PSAEAPPLDEFN.AE_DISABLE_RESTART` —
    they're independent switches and must agree with the restart analysis above. Also `MAXCONCURRENT`
    (0 = unlimited) against shared staging, and `TIMEOUTMINUTES` against real runtime.
  - **Message Catalog (8e):** resolve every message the AE raises; flag missing entries and `%1/%2`
    placeholders that don't match the call's parameters (they produce useless operator log lines).
  - **URL / IB (8f, 8h):** credentials or an environment-specific host baked into `PSURLDEFN.URL`;
    an inactive node or `EFF_STATUS = 'I'` routing that makes a publish silently go nowhere; an AE
    listed in `PSOPERATIONAE` is a handler, so review it against its inbound message, not standalone.
  - **Record / Translate (8i):** confirm `RECTYPE = 7` for anything treated as a temp table; resolve
    hard-coded status literals against `PSXLATITEM` and flag codes that are missing or `EFF_STATUS = 'I'`.
  - **Index (8j):** back a performance finding with the actual index list rather than asserting
    "no index" — check `ACTIVEFLAG` and `PLATFORM_ORA`.
- **Loose ends:** unused `TEMPTBLINSTANCES`, unguarded file `Close`/`WriteLine`, `AE_ON_NOROWS`,
  inactive/`**OBSOLETE**` steps, and speculative code paths that are coded but never configured.

## Output Format (match this structure)

Produce the review in a clean, two-part structure:

### Part 1: Business User Overview (Executive / Functional Persona)
1. **What is this process?** — 1–2 plain-English paragraphs explaining what business problem it solves and where it fits in the PeopleSoft functional lifecycle (e.g. AR refund creation, billing generation, ledger close, system housekeeping).
2. **Why does the business need it?** — Bulleted list of business benefits (operational efficiency, duplicate prevention, automated audit compliance, risk mitigation).
3. **How the Business Rules Work** — A structured table or mapping explaining the core business rules, qualification criteria, status transitions, or retention policies in business terminology.
4. **User Interaction & Operational Safeguards** — Who/what triggers it (scheduled batch recurrence, online page action, IB event), required manual parameters (if any), and data safeguards (e.g., financial ledger protection, validation enforced via Component Interfaces, duplicate checks).

### Part 2: Technical Architecture & Risk Review (Developer Persona)
1. **Execution Flow & Program Structure** — Sequential walkthrough table containing **ACTIVE steps and sections ONLY** (`AE_ACTIVE_STATUS = 'A'`). Do **not** clutter the flow table with inactive or obsolete steps. For each step list: `Section`, `Step`, `Type`, `Commit`, and plain-language logic description. If any step is overridden by an AE Action Plugin (query 1b), mark it clearly (e.g., *"[plugin: replaced by `<PLUGIN_AE>.MAIN.Step01`]"*).
2. **Technical Dependencies Extracted** — List the referenced state records (AET), App Packages, FUNCLIBs, Component Interfaces, Named SQL objects, File Layouts, and Message Sets that drive the real logic.
3. **Issues Identified (Highest Impact First)** — Categorized findings citing exact section/step and dependency names:
   - *Restart & Reliability* (`AE_DISABLE_RESTART` vs scheduler, commit boundaries, file handles)
   - *Correctness & Data Integrity* (Join row loss, bind syntax, staging table purge leaks, date formatting)
   - *Performance* (Row-by-row `Do Select` + PeopleCode vs set-based SQL, index coverage)
   - *Code Quality & Maintainability* (Inactive/dead code, hardcoded literals, unbound dynamic SQL)
4. **Action Plan & Summary** — Prioritized table of actionable recommendations.

> Pulling referenced App Package / FUNCLIB / SQL-definition code (query 7) and the definitions of the
> other objects the AE references — File Layout, CI, Process Definition, Message Catalog, URL, IB
> (query 8) — is a **default step**, not an offer: the dependencies are part of the program's real
> behavior. Only skip it for a trivial AE with no `import` / `Declare Function` / `SQL.<name>` /
> `FileLayout.` / `CompIntfc.` / `URL.` references.

Keep it brief and concrete. Verify before asserting a bug (especially date/bind issues).
Save extracted source to a `<APPLID>/` subfolder only if the user asks.
