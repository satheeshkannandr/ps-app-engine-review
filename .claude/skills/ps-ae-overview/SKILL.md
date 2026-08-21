---
name: ps-ae-overview
description: "Review, analyze, and explain a PeopleSoft Application Engine (AE) program. Supports dual-mode execution: (1) autonomous extraction directly from database metadata using the bundled Python CLI (ae_extractor.py) via SQLcl saved connections, or (2) reading a pre-extracted review package. Reassembles SQL and PeopleCode, resolves dependencies (App Packages, FUNCLIBs, Named SQL, File Layouts, CIs, IB), and produces a structured two-part review (Executive Business Overview + Technical Architecture & Risk Review)."
---

# PeopleSoft AE Overview & Review Playbook

## Purpose

Use this skill when asked to review, analyze, audit, or explain a PeopleSoft Application Engine program (e.g., *"Review App Engine AR_AGING and connect to TEST"* or *"Review the extracted package for LN_BI_EINV"*).

The skill supports **Dual-Mode Execution**:
- **Mode 1: Autonomous Agent Extraction** — The LLM executes the bundled Python script (`ae_extractor.py`) via SQLcl in seconds, pulls all metadata and dependencies into a single `<AE_APPLID>_review_package.md`, reads the file, and produces the structured review with zero interactive DB turn bottlenecks.
- **Mode 2: Pre-Extracted Package Review** — The user runs `ae_extractor.py` manually in their terminal; the LLM detects the existing review package file, skips database extraction, reads the file, and produces the review immediately.

---

## Workflow

### Mode 1: Autonomous Agent Extraction (Default when connecting to DB)

When the user asks to review an AE and provides/confirms the target database:

1. **Run the extractor CLI**:
   ```powershell
   python .agents/skills/ps-ae-overview/scripts/ae_extractor.py --ae <AE_APPLID> --database <DB_NAME> --output-dir output
   ```
   *(Uses SQLcl saved connections `sql -name <database>` — no credentials in code).*

2. **Read the generated package**:
   Read `output/<AE_APPLID>_review_package.md` end-to-end.

3. **Produce the Two-Part Review** following the [Output Format](#output-format) below.

---

### Mode 2: Pre-Extracted Review (Manual Human CLI Run)

When the user has already run the extractor, or says *"Review the package for <AE_APPLID>"*:

1. Verify `output/<AE_APPLID>_review_package.md` exists.
2. Read the review package completely without issuing database queries.
3. Produce the Two-Part Review following the [Output Format](#output-format) below.

---

## What the Extractor Pulls

| Section in Package | Source Tables |
| :--- | :--- |
| **Program Header** | `PSAEAPPLDEFN` |
| **AE Action Plugins** (both target & plugin directions) | `PS_PTAE_ACT_PLUGIN` |
| **Process Definition & Job Callers** | `PS_PRCSDEFN`, `PS_PRCSJOBITEM` |
| **State Records & AET Column Types** | `PSAEAPPLSTATE`, `all_tab_columns` |
| **Section / Step / Action Flow** | `PSAESECTDEFN`, `PSAESTEPDEFN`, `PSAESTMTDEFN` |
| **Assembled SQL Actions** (Oracle DBTYPE filtered) | `PSSQLTEXTDEFN` |
| **Assembled PeopleCode** (GBL/default filtered) | `PSPCMTXT` |
| **App Package Classes** | `PSPCMTXT` (by package root) |
| **FUNCLIB Functions** | `PSPCMTXT` (by record.field) |
| **Named SQL Objects** | `PSSQLTEXTDEFN`, `PSSQLDEFN` |
| **Strings Table Values** | `PS_STRINGS_TBL` |
| **File Layouts** | `PSFLDDEFN`, `PSFLDSEGDEFN`, `PSFLDFIELDDEFN` |
| **Component Interfaces** | `PSBCDEFN`, `PSBCITEM` |
| **Message Catalog Entries** | `PSMSGCATDEFN` |
| **URL Definitions** (secrets redacted) | `PSURLDEFN` |
| **Integration Broker Handlers** | `PSOPERATIONAE` |
| **Referenced Tables & Indexes** | `PSRECDEFN`, `PSINDEXDEFN`, `PSKEYDEFN` |
| **Plugin AE Source** | `PSPCMTXT`, `PSAESTMTDEFN` |

---

## Variant Handling & Trustworthy Assembly

The extraction engine resolves critical metadata nuances:
- **SQL Platform Variants:** `PSSQLTEXTDEFN` is filtered for Oracle (`DBTYPE = '2'`) when present, falling back to default/common (`' '`/`'0'`) at the latest `EFFDT`. It never concatenates across different database platforms.
- **PeopleCode Variants:** Prefers `OBJECTVALUE3 = 'GBL'` and `OBJECTVALUE4 = 'default'`.
- **Chunk Boundaries:** `SQLTEXT` and `PCTEXT` are reassembled untrimmed in `SEQNUM` / `PROGSEQ` order, preserving mid-token whitespace.

---

## Review Checklist (Key Checks to Perform)

- **AE Action Plugins:** Check §2 of the package first. If any step is overridden (`Replace`, `Before`, `After`), evaluate the plugin's logic rather than the vanilla code.
- **Restart Safety:** Check `AE_DISABLE_RESTART` vs `PS_PRCSDEFN.RESTARTENABLED`. If restart is enabled but the program opens file handles, relies on in-memory component variables, or executes external side effects (web services, payments, FTP), flag it.
- **Commit Boundaries:** Verify `AE_COMMIT_AFTER` frequency inside loops vs Do Select / file writes.
- **Component Interface (CI) Abends:** A failed CI method (`.get()`, `.Save()`) latches the step error status below the PeopleCode layer and causes the AE to abend at end-of-step even if caught in a `try/catch`. Flag any in-step retry loops.
- **Date Binds:** PeopleSoft Date fields are formatted as `'YYYY-MM-DD'`. Check the AET field data type before flagging implicit conversion bugs.
- **Concurrency & Staging:** If `TEMPTBLINSTANCES = 0`, verify that table updates are properly scoped by `PROCESS_INSTANCE` or run control to prevent cross-run collisions.
- **Performance:** Flag row-by-row `Do Select` + PeopleCode loops over large tables (`PS_ITEM_ACTIVITY`, `PS_ITEM`, ledger tables), unindexed `OR` predicates, and functions on columns (`UPPER(col) =`).
- **Dead / Obsolete Code:** Identify inactive steps (`AE_ACTIVE_STATUS = 'I'`) or block-commented implementations (`<* ... *>`).

---

## Output Format

Produce the review in a clean, two-part structure:

### Part 1: Business User Overview (Executive / Functional Persona)
1. **What is this process?** — 1–2 plain-English paragraphs explaining what business problem it solves and where it fits in the PeopleSoft functional lifecycle.
2. **Why does the business need it?** — Bulleted list of business benefits (operational efficiency, duplicate prevention, automated audit compliance, risk mitigation).
3. **How the Business Rules Work** — A structured table or mapping explaining the core business rules, qualification criteria, status transitions, or retention policies in business terminology.
4. **User Interaction & Operational Safeguards** — Who/what triggers it (scheduled batch recurrence, online page action, IB event), required manual parameters (if any), and data safeguards (e.g., financial ledger protection, validation enforced via Component Interfaces, duplicate checks).

### Part 2: Technical Architecture & Risk Review (Developer Persona)
1. **Execution Flow & Program Structure** — Sequential walkthrough table containing **ACTIVE steps and sections ONLY** (`AE_ACTIVE_STATUS = 'A'`). Do **not** clutter the flow table with inactive or obsolete steps. For each step list: `Section`, `Step`, `Type`, `Commit`, and plain-language logic description. If any step is overridden by an AE Action Plugin, mark it clearly (e.g., *"[plugin: replaced by `<PLUGIN_AE>.MAIN.Step01`]"*).
2. **Technical Dependencies Extracted** — List the referenced state records (AET), App Packages, FUNCLIBs, Component Interfaces, Named SQL objects, File Layouts, and Message Sets that drive the real logic.
3. **Issues Identified (Highest Impact First)** — Categorized findings citing exact section/step and dependency names:
   - *Restart & Reliability* (`AE_DISABLE_RESTART` vs scheduler, commit boundaries, file handles)
   - *Correctness & Data Integrity* (Join row loss, bind syntax, staging table purge leaks, date formatting)
   - *Performance* (Row-by-row `Do Select` + PeopleCode vs set-based SQL, index coverage)
   - *Code Quality & Maintainability* (Inactive/dead code, hardcoded literals, unbound dynamic SQL)
4. **Action Plan & Summary** — Prioritized table of actionable recommendations.

---

## Interactive Fallback Queries

For rare cases where additional runtime data is required (e.g., profiling row counts in a `Do Select` table, or checking `PSXLATITEM` translate values), use the SQLcl MCP tools to run targeted read-only queries against `SYSADM` tables, always inspecting `all_tab_columns` first.

---

## Tests

Run the test suite to verify extraction integrity:
```powershell
python .agents/skills/ps-ae-overview/tests/run_tests.py
```
