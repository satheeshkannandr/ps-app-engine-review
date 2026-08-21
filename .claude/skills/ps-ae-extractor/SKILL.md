---
name: ps-ae-extractor
description: "Extract a PeopleSoft Application Engine from PeopleTools metadata into a single review_package.md for LLM review. Runs a Python script that connects via SQLcl saved connections, gathers all metadata (header, plugins, SQL, PeopleCode, all 8 dependency groups), and writes one file. The LLM reads that file and produces the structured review — zero DB round-trips during analysis."
---

# PeopleSoft AE Extractor

## Purpose

Run a Python script to pull everything from PeopleTools metadata tables into one
`<AE_APPLID>_review_package.md` file — then read it and review. No MCP DB calls
needed during review.

Addresses the bottleneck in the interactive `ps-app-engine-review` skill: the LLM
burns 30+ MCP round-trips on data-gathering (one query per turn). This script does
all that in 4–5 subprocess calls, then the LLM reads one file and analyses.

## Workflow

### Step 1 — Run the extractor

```powershell
python .claude/skills/ps-ae-extractor/scripts/ae_extractor.py \
    --ae AR_AGING \
    --database TEST \
    --output-dir output
```

- Uses SQLcl saved connections (`sql -name <database>`) — no credentials in the script
- SQLcl must be on PATH, or pass `--sqlcl path\to\sql.exe`
- Output: `output/AR_AGING_review_package.md`

### Step 2 — Read and review

```
Read("output/AR_AGING_review_package.md")
```

Read it end to end — a package is large and arrives over several `Read` calls. Do not grep it
in place of reading it. Then produce the structured review (see Output Format below), working
through the `Reviewer Notes — Checklist` block at the end of the package.

**If the package already exists, skip Step 1.** Any of these means "read the existing
`output/<AE_APPLID>_review_package.md` and review it" — no extraction, no database:

> Review the AR_AGING package
> Review package for AR_AGING

Re-run the extractor only when the file is missing, or the user asks for a fresh extract.

## What the script extracts

| Section in output | Source tables |
| --- | --- |
| Program header | `PSAEAPPLDEFN` |
| AE Action Plugins (both directions) | `PS_PTAE_ACT_PLUGIN` |
| Process Definition + job callers | `PS_PRCSDEFN`, `PS_PRCSJOBITEM` |
| State records + AET column types | `PSAEAPPLSTATE`, `all_tab_columns` |
| Section / step / action flow | `PSAESECTDEFN`, `PSAESTEPDEFN`, `PSAESTMTDEFN` |
| Assembled SQL actions (DBTYPE=Oracle filtered) | `PSSQLTEXTDEFN` |
| Assembled PeopleCode (GBL/default filtered) | `PSPCMTXT` |
| App Package class source | `PSPCMTXT` (by package root) |
| FUNCLIB function source | `PSPCMTXT` (by record.field) |
| Named SQL objects | `PSSQLTEXTDEFN`, `PSSQLDEFN` |
| Strings Table values | `PS_STRINGS_TBL` |
| File Layouts | `PSFLDDEFN`, `PSFLDSEGDEFN`, `PSFLDFIELDDEFN` |
| Component Interfaces | `PSBCDEFN`, `PSBCITEM` |
| Message Catalog entries | `PSMSGCATDEFN` |
| URL Definitions | `PSURLDEFN` |
| Integration Broker handler registrations | `PSOPERATIONAE` |
| Records referenced + types | `PSRECDEFN` |
| Indexes on those records | `PSINDEXDEFN`, `PSKEYDEFN` |
| Plugin AE source | `PSPCMTXT`, `PSAESTMTDEFN` (for plugin AE IDs) |

## Variant handling (why the assembled source is trustworthy)

The script resolves the two things that are easy to get wrong by hand:

- **SQL platform variants** — `PSSQLTEXTDEFN` is keyed by `SQLID, SQLTYPE, MARKET, DBTYPE,
  EFFDT, SEQNUM`. The script keeps Oracle (`DBTYPE = '2'`) when present, else the common
  blank/`0` row, at the latest `EFFDT` — never concatenating Oracle + DB2 + SQL Server into
  syntactic garbage.
- **PeopleCode market/platform variants** — prefers `OBJECTVALUE3 = 'GBL'` /
  `OBJECTVALUE4 = 'default'` rather than gluing market overrides together.
- **Chunk boundaries** — `SQLTEXT` and `PCTEXT` are concatenated *untrimmed*, in `SEQNUM` /
  `PROGSEQ` order. PeopleSoft splits source mid-token, so trimming a boundary space would
  weld tokens (`SELECT *` + ` FROM X` → `SELECT *FROM X`) and corrupt the review.

## Output format for the LLM review

After reading `review_package.md`, produce the review in a clean, two-part structure:

### Part 1: Business User Overview (Executive / Functional Persona)
1. **What is this process?** — 1–2 plain-English paragraphs explaining what business problem it solves and where it fits in the PeopleSoft functional lifecycle (e.g. AR refund creation, billing generation, ledger close, system housekeeping).
2. **Why does the business need it?** — Bulleted list of business benefits (operational efficiency, duplicate prevention, automated audit compliance, risk mitigation).
3. **How the Business Rules Work** — A structured table or mapping explaining the core business rules, qualification criteria, status transitions, or retention policies in business terminology.
4. **User Interaction & Operational Safeguards** — Who/what triggers it (scheduled batch recurrence, online page action, IB event), required manual parameters (if any), and data safeguards (e.g., financial ledger protection, validation enforced via Component Interfaces, duplicate checks).

### Part 2: Technical Architecture & Risk Review (Developer Persona)
1. **Execution Flow & Program Structure** — Sequential walkthrough table containing **ACTIVE steps and sections ONLY** (`AE_ACTIVE_STATUS = 'A'`). Do **not** clutter the flow table with inactive or obsolete steps. For each step list: `Section`, `Step`, `Type`, `Commit`, and plain-language logic description. If any step is overridden by an AE Action Plugin (§2 of the package), mark it clearly (e.g., *"[plugin: replaced by `<PLUGIN_AE>.MAIN.Step01`]"*).
2. **Technical Dependencies Extracted** — List the referenced state records (AET), App Packages, FUNCLIBs, Component Interfaces, Named SQL objects, File Layouts, and Message Sets that drive the real logic.
3. **Issues Identified (Highest Impact First)** — Categorized findings citing exact section/step and dependency names (restart/reliability → correctness → performance → minor/dead code).
4. **Action Plan & Summary** — Prioritized table of actionable recommendations.

## When to fall back to interactive queries

The script covers the standard extraction. Connect via the SQLcl MCP server for follow-ups
only when the package cannot resolve something material:

- a `Do Select` over a table whose row volume you need to judge a performance finding
- `PSXLATITEM` translate values behind a hard-coded status literal you want to read concretely
- a second-level dependency (a class the pulled class itself calls) that is central to a finding
- `PSURLDEFN.URL` when you need to confirm an embedded credential (deliberately excluded here)
- IB routing/node state (`PSIBRTNGDEFN`, `PSMSGNODEDEFN`) when the AE publishes

Server-side run logs live on the App Server / Process Scheduler host (Unix), not the database —
reach them with your own saved SSH setup when program logging is too sparse to explain a failure.

## Tests

```powershell
python tests/run_tests.py     # 47 tests, no pytest needed, no DB needed
```

Covers DBTYPE/EFFDT variant selection, chunk-boundary whitespace preservation, dependency-scan
regexes, SQL-injection guards on `--ae` / `--database`, schema-aware query building, and an
end-to-end render smoke test.

## Safeguards

- Reuses SQLcl saved connections (`sql -name`) — credentials stay in SQLcl's encrypted store
- Read-only (`SET TRANSACTION READ ONLY` + `ROLLBACK`) — no DML/DDL
- All identifiers validated before interpolation into SQL
- URL values omitted from output (noted in §8h) to avoid leaking credentials
- Standard library only — no pip install required
