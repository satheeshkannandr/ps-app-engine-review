## PeopleSoft AE Extractor

A script-first companion to [`ps-app-engine-review`](../ps-app-engine-review/README.md).
Instead of the assistant issuing 30+ live MCP queries (one per turn) while it reviews, a single
Python run pulls **everything** out of the PeopleTools metadata tables and writes one
`<AE_APPLID>_review_package.md`. The assistant then reads that one file and produces the review —
**zero DB round-trips during analysis**.

**Connects to whatever database you name** (SQLcl saved connections; e.g. `TEST`).
Reference instance `TEST` = PeopleSoft FSCM 9.2 / PeopleTools 8.58 · Oracle 19c;
other instances may differ and the extractor reports the actual session identity in the package header.

| | `ps-app-engine-review` | `ps-ae-extractor` |
| --- | --- | --- |
| How it gets data | live MCP SQLcl queries, one per turn | one Python run, 4–5 batched SQLcl sessions |
| Where the data lands | in the conversation | `output/<AE_APPLID>_review_package.md` |
| Best for | quick look, ad-hoc follow-ups, "just explain step 3" | full review of a large AE, repeatable/offline review, sharing the extract |

Both produce the **same structured review** — *Part 1: Business User Overview (purpose, lifecycle role, rules/retention matrix, user interaction & safeguards) → Part 2: Technical Architecture & Risk Review (active steps flow, dependencies, categorized risks, prioritized action plan)*.

## Pre-requisites

### Python

Python **3.9+** (`str.removeprefix` is used). Standard library only — **no `pip install`** needed.

```powershell
python --version
```

### SQLcl

The script shells out to SQLcl and reuses its **saved connections**, so no credentials ever appear
in the script, in the command line, or in the output.

Download SQLcl from [Oracle's website](https://www.oracle.com/database/sqldeveloper/technologies/sqlcl/).
Unzip to a permanent location (e.g. `C:\tools\sqlcl`) and add `C:\tools\sqlcl\bin` to your system `PATH`.

Verify:
```powershell
sql -v
```

If you'd rather not touch `PATH`, pass the binary explicitly: `--sqlcl C:\tools\sqlcl\bin\sql.exe`.

> The **MCP SQLcl server is not required** for extraction — the script calls the `sql` executable
> directly. You still want the MCP server configured (see the
> [review skill's README](../ps-app-engine-review/README.md)) for the follow-up queries the package
> deliberately leaves out.

### Save Named Database Connections

The script connects by **connection name** (e.g. `TEST`, `DEV`) — the same store the review skill uses,
so if you already set this up there's nothing to do. Start SQLcl with no connection:

```powershell
sql /nolog
```

Then, at the `SQL>` prompt (`-save` / `-savepwd` are flags of the `connect` command, not the `sql` launcher):

```sql
connect -save TEST -savepwd <username>/<password>@<connect_string>
```

Repeat for each environment (DEV, TEST, UAT, PROD). Saved connections live under the DBTools home —
on **Windows** `%APPDATA%\DBTools\connections`; on macOS/Linux `~/.dbtools`.

> **Security note:** Saved passwords are stored in an encrypted wallet. Do **not** hardcode
> credentials in scripts or skill files.

---

## Install the skill in your AI tool

[SKILL.md](SKILL.md) is the playbook — how to invoke the script, what each package section contains,
and the review output format. The real work lives in
[scripts/ae_extractor.py](scripts/ae_extractor.py), so keep the two together.

**Claude Code** — put the folder where skills are discovered, then it auto-invokes (or call it with
`/ps-ae-extractor`):

```
<your-project>/.claude/skills/ps-ae-extractor/
├── SKILL.md
└── scripts/ae_extractor.py
```

**GitHub Copilot (VS Code)** — save the playbook as a reusable prompt file and keep the script
somewhere in the repo:

```
<your-project>/.github/prompts/ps-ae-extractor.prompt.md
```

**Any other tool** — you don't need a tool at all for step 1. Run the script yourself, then paste or
attach the generated `review_package.md` into any chat and ask for the review.

> The `---` frontmatter block at the top of `SKILL.md` (`name` / `description`) is Claude Code's
> skill format. Copilot prompt files use `---` frontmatter too but different keys, so replace it
> with `mode: agent` and a `description:` line if you go that route; harmless to leave otherwise.

## How to use

### Step 1 — extract

```powershell
python .claude/skills/ps-ae-extractor/scripts/ae_extractor.py --ae AR_AGING --database TEST --output-dir output
```

| Flag | Required | Meaning |
| --- | --- | --- |
| `--ae` | yes | `AE_APPLID`, auto-uppercased; validated as a PeopleSoft identifier |
| `--database` | yes | SQLcl **saved connection name** |
| `--output-dir` | no | defaults to the current directory |
| `--sqlcl` | no | path to `sql` / `sql.exe` if it isn't on `PATH` |

Progress goes to **stderr** (`[1/5] Checking saved connections…` → `[5/5] Extracting dependencies…`);
a one-line JSON summary goes to **stdout**, so the run is easy to script:

```json
{"ae":"AR_AGING","database":"TEST","db_name":"TEST","output":"output/AR_AGING_review_package.md","lines":4271}
```

Exit code `0` on success, `1` with an `ERROR:` line on failure (unknown connection, AE not in
`PSAEAPPLDEFN`, SQLcl not found, timeout).

### Step 2 — review

Ask (substitute the program id and the database):

> **Review App Engine `<AE_APPLID>` using the extractor. Connect to '<DB_NAME>' database**

The assistant runs step 1, reads `output/<AE_APPLID>_review_package.md`, and returns:
**Part 1: Business User Overview (plain-English purpose, business lifecycle role, rules/retention matrix, user interaction & safeguards) → Part 2: Technical Architecture & Risk Review (active steps flow, dependencies, categorized risks, prioritized action plan).**

**If the package is already generated**, say so and the extraction is skipped entirely — no
database needed:

> **Review the `<AE_APPLID>` package**

No longer prompt is required. The review format lives in [SKILL.md](SKILL.md), and the script
appends a `Reviewer Notes — Checklist` block to the end of every package — so the file carries its
own review instructions and works the same way pasted into any other LLM.

## What the package contains

One Markdown file, sections 1–8 plus a reviewer checklist:

| § | Content | Source tables |
| --- | --- | --- |
| 1 | Program header — restart flag, temp-table instances, program type | `PSAEAPPLDEFN` |
| 2 | AE Action Plugins, **both directions** (this AE overridden / this AE *is* the plugin) | `PS_PTAE_ACT_PLUGIN` |
| 3 | Process definition + the jobs that schedule it | `PS_PRCSDEFN`, `PS_PRCSJOBITEM` |
| 4 | State records **with AET column data types** (for date/bind checks) | `PSAEAPPLSTATE`, `all_tab_columns` |
| 5 | Section / step / action flow in execution order | `PSAESECTDEFN`, `PSAESTEPDEFN`, `PSAESTMTDEFN` |
| 6 | Assembled SQL per action (Oracle variant selected) | `PSSQLTEXTDEFN` |
| 7 | Assembled PeopleCode per action (GBL/default variant) | `PSPCMTXT` |
| 8a–8l | Dependencies: App Package classes, FUNCLIB functions, named SQL, Strings Table, File Layouts, Component Interfaces, Message Catalog, URL definitions, IB handler registration, records + their indexes, and **plugin AE source** | `PSPCMTXT`, `PSSQLTEXTDEFN`, `PSSQLDEFN`, `PS_STRINGS_TBL`, `PSFLD*`, `PSBC*`, `PSMSGCATDEFN`, `PSURLDEFN`, `PSOPERATIONAE`, `PSRECDEFN`, `PSINDEXDEFN`, `PSKEYDEFN` |

Dependencies aren't a fixed list — the script scans the assembled SQL and PeopleCode for
`import`, `Declare Function`, `SQL.<name>`, `FileLayout.<name>`, `CompIntfc.<name>`, `URL.<name>`,
`MsgGet`/`MessageBox` message numbers and `PS_*` table references, then pulls exactly what it found.

## Why the assembled source is trustworthy

Two things are easy to get wrong when reassembling PeopleTools source by hand; the script handles both:

- **Platform variants.** `PSSQLTEXTDEFN` is keyed by `SQLID, SQLTYPE, MARKET, DBTYPE, EFFDT, SEQNUM`.
  The script keeps Oracle (`DBTYPE = '2'`) when present, else the common blank/`0` row, at the latest
  `EFFDT` — never concatenating Oracle + DB2 + SQL Server into syntactic garbage. PeopleCode variants
  prefer `OBJECTVALUE3 = 'GBL'` / `OBJECTVALUE4 = 'default'`.
- **Chunk boundaries.** `SQLTEXT` and `PCTEXT` are concatenated **untrimmed**, in `SEQNUM` / `PROGSEQ`
  order. PeopleSoft splits source mid-token, so trimming a boundary space welds tokens
  (`SELECT *` + ` FROM X` → `SELECT *FROM X`) and corrupts the review.

## When to still query the database

The package covers the standard extraction. Connect via the SQLcl MCP server for follow-ups only when
the package can't resolve something material:

- row volumes behind a `Do Select` you need to judge a performance finding
- `PSXLATITEM` translate values behind a hard-coded status literal
- a second-level dependency (a class the pulled class itself calls) that is central to a finding
- `PSURLDEFN.URL` when you need to confirm an embedded credential (deliberately excluded — see below)
- IB routing/node state (`PSIBRTNGDEFN`, `PSMSGNODEDEFN`) when the AE publishes

Server-side run logs live on the App Server / Process Scheduler host (Unix), not the database — reach
them with your own saved SSH setup when program logging is too sparse to explain a failure.

## Tests

```powershell
python tests/run_tests.py
```

47 tests, no pytest and no database needed. They cover DBTYPE/EFFDT variant selection,
chunk-boundary whitespace preservation, dependency-scan regexes, SQL-injection guards on `--ae` /
`--database`, schema-aware query building, and an end-to-end render smoke test.

## Safeguards

- Reuses SQLcl **saved connections** (`sql -name`) — credentials stay in SQLcl's encrypted store
- **Read-only:** `SET TRANSACTION READ ONLY` + `ROLLBACK`; no DML or DDL is issued
- `--ae` and `--database` are validated against strict patterns before any interpolation into SQL,
  and every literal is quote-escaped
- **Schema-aware:** every table/column is checked in `all_tab_columns` first, so a missing table on an
  older tools release skips its query instead of erroring the run
- `PSURLDEFN.URL` values are **omitted** from the package (noted in §8h) so saved credentials don't
  leak into a file you might share
- Standard library only — nothing to install, nothing to audit

## Known limitations

- **`--ae` / `--database` are required** — unlike the interactive skill, the script never guesses a
  database. Name one explicitly.
- **Effective-dated variants:** the latest `EFFDT` is chosen for SQL text; AE sections/PeopleCode with
  multiple effective-dated versions are not filtered by `EFFDT`, so verify §7 against App Designer if
  a program is effective-dated.
- **Market variants:** SQL is selected per `(SQLID, SQLTYPE, MARKET)` but rendered per `SQLID`, so a
  program with real market-specific SQL overrides needs a manual check.
- **Record scan** matches literal `PS_*` names in the SQL; records referenced only through
  `%Table(RECNAME)` meta-SQL won't appear in §8j/§8k.
- **Called AEs** (`CallSection` to another program, `CallAppEngine`) are detected but their source is
  **not** pulled — extract those programs separately.
- `PSPCMTXT` is large; on a full FSCM instance the PeopleCode queries are the slow part of the run.
  The SQLcl subprocess timeout is 300 s per batch.

## Notes

- Generated packages, `output/`, and `__pycache__/` are git-ignored — the extract is a build artifact,
  not source. Commit one only if you deliberately want a snapshot.
- A package for a mid-size AE is typically a few thousand lines; the whole point is that the assistant
  reads it **once** instead of paging it in over 30 turns.
