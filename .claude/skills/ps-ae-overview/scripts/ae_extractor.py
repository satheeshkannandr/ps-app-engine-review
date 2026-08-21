#!/usr/bin/env python3
"""
ae_extractor.py — Extract a PeopleSoft Application Engine for LLM review.

Connects to Oracle via SQLcl saved connections, runs all PeopleTools metadata
queries in batched subprocess calls, reassembles SQL and PeopleCode, pulls all
dependencies (App Package, FUNCLIB, named SQL, File Layout, CI, Process Def,
Message Catalog, URL, IB, Records, Indexes), and writes a single
<AE_APPLID>_review_package.md the LLM reads and reviews — zero DB round-trips
during analysis.

Usage:
    python ae_extractor.py --ae AR_AGING --database TEST [--output-dir .]

Requirements: Python 3.9+, SQLcl (sql / sql.exe) on PATH or --sqlcl.
No third-party packages — standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AE_STMT_TYPES: dict[str, str] = {
    "P": "PeopleCode",
    "S": "SQL",
    "C": "Call Section",
    "D": "Do Select",
    "H": "Do When",
    "W": "Do While",
    "N": "Do Until",
    "M": "Log Message",
    "X": "XSLT",
}

IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
CONNECTION_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")

CORE_TABLES = [
    "PSAEAPPLDEFN", "PSAEAPPLSTATE", "PSAESECTDEFN", "PSAESTEPDEFN",
    "PSAESTMTDEFN", "PSSQLTEXTDEFN", "PSPCMTXT", "PS_PTAE_ACT_PLUGIN",
    "PS_PRCSDEFN", "PS_PRCSJOBITEM",
]

DEP_TABLES = [
    "PSPACKAGEDEFN", "PSAPPCLASSDEFN", "PSSQLDEFN", "PSFLDDEFN",
    "PSFLDSEGDEFN", "PSFLDFIELDDEFN", "PSBCDEFN", "PSBCITEM",
    "PSMSGCATDEFN", "PSURLDEFN", "PSOPERATIONAE", "PSRECDEFN",
    "PSINDEXDEFN", "PSKEYDEFN",
]

REC_TYPE_LABELS: dict[str, str] = {
    "0": "SQL Table", "1": "View", "2": "Derived/Work",
    "5": "Sub-Record", "6": "Dynamic View", "7": "Temp Table",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ExtractorError(RuntimeError):
    """User-actionable extraction failure."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_in(values: Sequence[str]) -> str:
    if not values:
        return "('__EMPTY__')"
    return "(" + ",".join(sql_literal(v) for v in sorted(set(str(v).upper() for v in values))) + ")"


def row_value(row: dict[str, Any], *names: str, default: str = "") -> str:
    """Trimmed value — for identifiers, flags, and display cells."""
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        v = lowered.get(name.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def raw_value(row: dict[str, Any], name: str) -> str:
    """Untrimmed value — required for SQLTEXT/PCTEXT.

    PeopleSoft splits source across chunk rows at arbitrary character boundaries, so a
    leading or trailing space in a chunk is significant. Trimming it welds tokens together
    ("SELECT *" + " FROM X" -> "SELECT *FROM X") and corrupts the reassembled source.
    """
    lowered = {str(k).lower(): v for k, v in row.items()}
    v = lowered.get(name.lower())
    return "" if v is None else str(v)


def parse_json_documents(text: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start < 0:
            break
        try:
            doc, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(doc, dict) and "results" in doc:
            docs.append(doc)
        i = start + consumed
    return docs


def rows_from_document(doc: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in doc.get("results", []):
        for item in result.get("items", []):
            if isinstance(item, dict):
                rows.append({str(k).lower(): v for k, v in item.items()})
    return rows


def md_fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.rstrip()}\n```\n"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(s: Any) -> str:
        return str(s or "").replace("|", "\\|").replace("\n", " ").replace("\r", "").strip()
    sep = " | ".join("---" for _ in headers)
    hdr = " | ".join(cell(h) for h in headers)
    lines = [f"| {hdr} |", f"| {sep} |"]
    for row in rows:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SQLcl runner
# ---------------------------------------------------------------------------

@dataclass
class SQLclRunner:
    executable: str
    database: str

    @staticmethod
    def resolve(explicit: str | None) -> str:
        candidate = explicit or shutil.which("sql") or shutil.which("sql.exe")
        if not candidate:
            raise ExtractorError("SQLcl not found. Add sql.exe to PATH or pass --sqlcl.")
        return str(Path(candidate).resolve())

    def _run(self, args: Sequence[str], script: str) -> str:
        completed = subprocess.run(
            [self.executable, *args],
            input=script,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
        output = completed.stdout or ""
        if completed.returncode != 0 or re.search(r"\b(?:ORA|SP2|SODA)-\d+", output):
            raise ExtractorError(f"SQLcl error:\n{output.strip()[:3000]}")
        return output

    def list_connections(self) -> list[str]:
        output = self._run(["-S", "/nolog"], "connmgr list -flat\nexit\n")
        ignored = ("SQLcl:", "Oracle SQL", "Connected", "SQL>")
        return sorted(
            line.strip()
            for line in output.splitlines()
            if line.strip()
            and not any(line.strip().startswith(p) for p in ignored)
            and CONNECTION_RE.fullmatch(line.strip())
        )

    def verify_connection(self) -> dict[str, str]:
        rows = self.run_queries([("id", (
            "SELECT sys_context('USERENV','DB_NAME') db_name, "
            "sys_context('USERENV','SESSION_USER') session_user FROM dual"
        ))])["id"]
        if not rows:
            raise ExtractorError(f"Connection '{self.database}' returned no session identity.")
        return {
            "db_name": row_value(rows[0], "db_name"),
            "session_user": row_value(rows[0], "session_user"),
        }

    def run_queries(self, queries: Sequence[tuple[str, str]]) -> dict[str, list[dict[str, Any]]]:
        """Run multiple SQL statements in one SQLcl session, return results keyed by name."""
        commands = [
            "whenever sqlerror exit sql.sqlcode",
            "set sqlformat json-formatted",
            "set feedback off pagesize 0 long 4000000 longchunksize 4000000 trimspool on",
            "alter session set nls_date_format = 'YYYY-MM-DD HH24:MI:SS';",
            "set transaction read only;",
        ]
        for name, query in queries:
            commands += [
                f"prompt __EXT_{name}_START__",
                query.rstrip("; \n") + ";",
                f"prompt __EXT_{name}_END__",
            ]
        commands += ["rollback", "exit"]
        output = self._run(["-S", "-L", "-name", self.database], "\n".join(commands) + "\n")
        result: dict[str, list[dict[str, Any]]] = {}
        for name, _ in queries:
            sm = f"__EXT_{name}_START__"
            em = f"__EXT_{name}_END__"
            s = output.find(sm)
            e = output.find(em, s + len(sm)) if s >= 0 else -1
            segment = output[s + len(sm):e] if s >= 0 and e >= 0 else ""
            docs = parse_json_documents(segment)
            result[name] = rows_from_document(docs[0]) if docs else []
        return result


# ---------------------------------------------------------------------------
# Schema inspection
# ---------------------------------------------------------------------------

def inspect_columns(runner: SQLclRunner, tables: Sequence[str]) -> dict[str, set[str]]:
    values = ",".join(sql_literal(t) for t in tables)
    rows = runner.run_queries([(
        "cols",
        f"SELECT table_name, column_name FROM all_tab_columns "
        f"WHERE owner = 'SYSADM' AND table_name IN ({values}) "
        f"ORDER BY table_name, column_id"
    )])["cols"]
    schema: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        t = row_value(row, "table_name").upper()
        c = row_value(row, "column_name").upper()
        if t and c:
            schema[t].add(c)
    return dict(schema)


def tbl(schema: dict[str, set[str]], table: str) -> bool:
    return bool(schema.get(table.upper()))


def col(schema: dict[str, set[str]], table: str, column: str) -> bool:
    return column.upper() in schema.get(table.upper(), set())


# ---------------------------------------------------------------------------
# SQL and PeopleCode reassembly
# ---------------------------------------------------------------------------

def select_sql_variants(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep Oracle (DBTYPE=2) variant, else blank/default; latest EFFDT; sort by SEQNUM."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row_value(row, "sqlid").upper(),
            row_value(row, "sqltype", default="0"),
            row_value(row, "market", default="GBL").upper(),
        )
        grouped[key].append(row)
    selected: list[dict[str, Any]] = []
    for candidates in grouped.values():
        dbtypes = {row_value(r, "dbtype") for r in candidates}
        wanted = "2" if "2" in dbtypes else (" " if " " in dbtypes else "0" if "0" in dbtypes else sorted(dbtypes)[0])
        platform_rows = [r for r in candidates if row_value(r, "dbtype") == wanted]
        dates = [row_value(r, "effdt") for r in platform_rows]
        latest = max(dates) if dates else ""
        selected.extend(r for r in platform_rows if row_value(r, "effdt") == latest)
    return sorted(selected, key=lambda r: (
        row_value(r, "sqlid").upper(),
        int(row_value(r, "seqnum", default="0") or "0"),
    ))


def assemble_sql_by_id(rows: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Return {SQLID: assembled_text} after DBTYPE/EFFDT filtering."""
    filtered = select_sql_variants(rows)
    result: dict[str, str] = {}
    for row in filtered:
        sid = row_value(row, "sqlid").upper()
        result[sid] = result.get(sid, "") + raw_value(row, "sqltext")
    return result


def prefer_gbl_default(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """From PeopleCode rows with market/platform variants, return GBL/default."""
    by_variant: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        mk = row_value(row, "objectvalue3", default="GBL").upper().strip() or "GBL"
        db = row_value(row, "objectvalue4", default="default").lower().strip() or "default"
        by_variant[(mk, db)].append(row)
    for key in [("GBL", "default"), ("GBL", ""), (" ", "default"), ("GBL", "gbl")]:
        if key in by_variant:
            return by_variant[key]
    return next(iter(by_variant.values())) if by_variant else []


def concat_pctext(rows: Sequence[dict[str, Any]]) -> str:
    """Concatenate PCTEXT in PROGSEQ order."""
    sorted_rows = sorted(rows, key=lambda r: int(row_value(r, "progseq", default="0") or "0"))
    return "".join(raw_value(r, "pctext") for r in sorted_rows)


def concat_by_object_key(rows: Sequence[dict[str, Any]]) -> str:
    """Concatenate PCTEXT ordered by the full OBJECTVALUE2..7 key, then PROGSEQ.

    Used for App Package classes and other non-AE objects where OBJECTVALUE3/4 are part
    of the object path, not the market/platform pair that prefer_gbl_default() assumes.
    """
    def key(row: dict[str, Any]) -> tuple:
        return tuple(row_value(row, f"objectvalue{i}") for i in range(2, 8)) + (
            int(row_value(row, "progseq", default="0") or "0"),
        )
    return "".join(raw_value(r, "pctext") for r in sorted(rows, key=key))


# ---------------------------------------------------------------------------
# Dependency scanning
# ---------------------------------------------------------------------------

def scan_for_dependencies(
    ae: str,
    sql_text: str,
    pc_text: str,
    actions: Sequence[dict[str, Any]],
    steps: Sequence[dict[str, Any]],
    plugins: Sequence[dict[str, Any]],
) -> dict[str, list[str]]:
    combined = (sql_text + "\n" + pc_text).upper()

    named_sql: set[str] = set()
    for m in re.finditer(r"\b(?:GETSQL|CREATESQL|SQLEXEC)\s*\(\s*SQL\.([A-Z][A-Z0-9_]*)", combined):
        named_sql.add(m.group(1))
    for m in re.finditer(r"\bSQL\.([A-Z][A-Z0-9_]*)\b", combined):
        named_sql.add(m.group(1))
    named_sql.difference_update({"CLOSE", "EXECUTE", "FETCH", "OPEN", "STATUS"})

    app_classes: set[str] = set()
    for m in re.finditer(r"(?im)^\s*IMPORT\s+([A-Z][A-Z0-9_:]*)", combined):
        app_classes.add(m.group(1).rstrip(";: "))
    for m in re.finditer(r"\bCREATE\s+([A-Z][A-Z0-9_]*:[A-Z][A-Z0-9_:]*)\s*\(", combined):
        app_classes.add(m.group(1))

    funclibrary: set[str] = set()
    for m in re.finditer(
        r"DECLARE\s+FUNCTION\s+\w+\s+PEOPLECODE\s+([A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)", combined
    ):
        funclibrary.add(f"{m.group(1)}.{m.group(2)}")

    file_layouts: set[str] = set()
    for m in re.finditer(r"\bFILELAYOUT\.([A-Z][A-Z0-9_]*)", combined):
        file_layouts.add(m.group(1))

    component_interfaces: set[str] = set()
    for m in re.finditer(r"\bCOMPINTFC\.([A-Z][A-Z0-9_]*)", combined):
        component_interfaces.add(m.group(1))

    urls: set[str] = set()
    for m in re.finditer(r"\b(?:URL|GETURL\s*\(\s*URL)\.([A-Z][A-Z0-9_]*)", combined):
        urls.add(m.group(1))

    messages: set[str] = set()
    # MessageBox(style, title, set, nbr, ...) — two args precede the message set number.
    for m in re.finditer(r"\bMESSAGEBOX\s*\([^,]*,[^,]*,\s*(\d+)\s*,\s*(\d+)", combined):
        messages.add(f"{m.group(1)}:{m.group(2)}")
    # MsgGet(set, nbr, ...) / %MsgGet(...) / MsgGetText(...) — set number is the first arg.
    for m in re.finditer(r"%?\bMSGGET(?:TEXT)?\s*\(\s*(\d+)\s*,\s*(\d+)", combined):
        messages.add(f"{m.group(1)}:{m.group(2)}")

    strings_programs: set[str] = set()
    for m in re.finditer(
        r"PS_STRINGS_TBL.*?PROGRAM_ID\s*=\s*['\"]([A-Z][A-Z0-9_]*)['\"]", combined, re.DOTALL
    ):
        strings_programs.add(m.group(1))

    records: set[str] = set()
    for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+(PS_[A-Z0-9_]+)", combined):
        records.add(m.group(1))

    plugin_aes: set[str] = set()
    for row in plugins:
        if row_value(row, "enabled").upper() not in {"Y", "1", "A", ""}:
            continue
        plug = row_value(row, "ptae_plug_applid").upper()
        if plug and plug != ae.upper():
            plugin_aes.add(plug)

    called_aes: set[str] = set()
    for row in steps:
        do_appl = row_value(row, "ae_do_appl_id").upper()
        if do_appl and do_appl != ae.upper():
            called_aes.add(do_appl)
    for m in re.finditer(r"\bCALLAPPENGINE\s*\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']", combined):
        called_aes.add(m.group(1))

    return {
        "named_sql": sorted(named_sql),
        "app_classes": sorted(app_classes),
        "funclibrary": sorted(funclibrary),
        "file_layouts": sorted(file_layouts),
        "component_interfaces": sorted(component_interfaces),
        "urls": sorted(urls),
        "messages": sorted(messages),
        "strings_programs": sorted(strings_programs),
        "records": sorted(records),
        "plugin_aes": sorted(plugin_aes),
        "called_aes": sorted(called_aes),
    }


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

def build_core_queries(ae: str, schema: dict[str, set[str]]) -> list[tuple[str, str]]:
    lit = sql_literal(ae)
    queries: list[tuple[str, str]] = []

    # Order matters for the flow table — AE_SEQ_NUM drives step execution order.
    for name, table, key_col, order_by in [
        ("program_header", "PSAEAPPLDEFN",  "AE_APPLID", ""),
        ("sections",       "PSAESECTDEFN",  "AE_APPLID", "AE_SECTION"),
        ("steps",          "PSAESTEPDEFN",  "AE_APPLID", "AE_SECTION, AE_SEQ_NUM"),
        ("actions",        "PSAESTMTDEFN",  "AE_APPLID", "AE_SECTION, AE_STEP"),
        ("state_records",  "PSAEAPPLSTATE", "AE_APPLID", ""),
        ("process_def",    "PS_PRCSDEFN",   "PRCSNAME",  ""),
        ("job_callers",    "PS_PRCSJOBITEM","PRCSNAME",  ""),
    ]:
        if not (tbl(schema, table) and col(schema, table, key_col)):
            continue
        query = f"SELECT * FROM {table} WHERE upper({key_col}) = {lit}"
        order_cols = [c for c in order_by.replace(" ", "").split(",") if c and col(schema, table, c)]
        if order_cols:
            query += " ORDER BY " + ", ".join(order_cols)
        queries.append((name, query))

    if tbl(schema, "PSPCMTXT"):
        ov = sorted(c for c in schema["PSPCMTXT"] if re.match(r"^OBJECTVALUE[1-7]$", c))
        if ov:
            pred = " OR ".join(f"upper({c}) = {lit}" for c in ov)
            order = ",".join(sorted(c for c in schema["PSPCMTXT"] if re.match(r"^(OBJECTID|OBJECTVALUE|PROGSEQ)", c))) or "1"
            queries.append(("peoplecode", f"SELECT * FROM PSPCMTXT WHERE {pred} ORDER BY {order}"))

    if tbl(schema, "PS_PTAE_ACT_PLUGIN"):
        acols = [c for c in schema["PS_PTAE_ACT_PLUGIN"] if "APPLID" in c]
        if acols:
            pred = " OR ".join(f"upper({c}) = {lit}" for c in acols)
            queries.append(("plugins", f"SELECT * FROM PS_PTAE_ACT_PLUGIN WHERE {pred} ORDER BY 1,2,3"))

    return queries


def build_sql_and_state_queries(
    schema: dict[str, set[str]],
    sql_ids: Sequence[str],
    state_recnames: Sequence[str],
) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    if sql_ids and tbl(schema, "PSSQLTEXTDEFN") and col(schema, "PSSQLTEXTDEFN", "SQLID"):
        queries.append(("sql_text",
            f"SELECT * FROM PSSQLTEXTDEFN WHERE upper(SQLID) IN {sql_in(sql_ids)} "
            f"ORDER BY SQLID, DBTYPE, SEQNUM"))
    for recname in state_recnames:
        if not IDENTIFIER_RE.fullmatch(recname.upper()):
            continue
        table = f"PS_{recname}".upper()
        qname = f"aet_{recname.lower()}"
        queries.append((qname,
            f"SELECT column_name, data_type, data_length FROM all_tab_columns "
            f"WHERE table_name = {sql_literal(table)} ORDER BY column_id"))
    return queries


def build_dep_queries(
    schema: dict[str, set[str]],
    deps: dict[str, list[str]],
    ae: str,
    plugin_aes: Sequence[str],
) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []

    # App Package and FUNCLIB source from PSPCMTXT
    packages = sorted({cls.split(":")[0] for cls in deps.get("app_classes", [])})
    funclib_recs = sorted({fl.split(".")[0] for fl in deps.get("funclibrary", [])})
    pc_roots = sorted(set(packages + funclib_recs))
    if pc_roots and tbl(schema, "PSPCMTXT"):
        ov = sorted(c for c in schema["PSPCMTXT"] if re.match(r"^OBJECTVALUE[1-7]$", c))
        if ov:
            pred = " OR ".join(f"upper({ov[0]}) = {sql_literal(r)}" for r in pc_roots)
            order = ",".join(sorted(c for c in schema["PSPCMTXT"] if re.match(r"^(OBJECTID|OBJECTVALUE|PROGSEQ)", c))) or "1"
            queries.append(("dep_peoplecode", f"SELECT * FROM PSPCMTXT WHERE ({pred}) ORDER BY {order}"))

    # Named SQL
    if deps.get("named_sql"):
        if tbl(schema, "PSSQLTEXTDEFN"):
            queries.append(("named_sql_text",
                f"SELECT * FROM PSSQLTEXTDEFN WHERE upper(SQLID) IN {sql_in(deps['named_sql'])} "
                f"ORDER BY SQLID, DBTYPE, SEQNUM"))
        if tbl(schema, "PSSQLDEFN") and col(schema, "PSSQLDEFN", "SQLID"):
            queries.append(("named_sql_headers",
                f"SELECT * FROM PSSQLDEFN WHERE upper(SQLID) IN {sql_in(deps['named_sql'])}"))

    # Strings Table
    if deps.get("strings_programs"):
        queries.append(("strings_tbl",
            f"SELECT * FROM PS_STRINGS_TBL WHERE upper(PROGRAM_ID) IN {sql_in(deps['strings_programs'])} "
            f"ORDER BY PROGRAM_ID, STRING_ID, LABEL_ID"))

    # File Layouts
    if deps.get("file_layouts"):
        for table, qname in [
            ("PSFLDDEFN",      "fl_header"),
            ("PSFLDSEGDEFN",   "fl_segments"),
            ("PSFLDFIELDDEFN", "fl_fields"),
        ]:
            if tbl(schema, table) and col(schema, table, "FLDDEFNNAME"):
                queries.append((qname,
                    f"SELECT * FROM {table} WHERE upper(FLDDEFNNAME) IN {sql_in(deps['file_layouts'])} ORDER BY 1"))

    # Component Interfaces
    if deps.get("component_interfaces"):
        for table, qname in [("PSBCDEFN", "ci_headers"), ("PSBCITEM", "ci_items")]:
            if tbl(schema, table) and col(schema, table, "BCNAME"):
                queries.append((qname,
                    f"SELECT * FROM {table} WHERE upper(BCNAME) IN {sql_in(deps['component_interfaces'])} ORDER BY 1"))

    # App Package hierarchy
    if packages:
        for table, qname in [("PSPACKAGEDEFN", "pkg_hierarchy"), ("PSAPPCLASSDEFN", "pkg_classes")]:
            if tbl(schema, table) and col(schema, table, "PACKAGEROOT"):
                queries.append((qname,
                    f"SELECT * FROM {table} WHERE upper(PACKAGEROOT) IN {sql_in(packages)} ORDER BY 1"))

    # Message Catalog
    if deps.get("messages"):
        predicates = []
        for pair in deps["messages"]:
            try:
                s, n = pair.split(":", 1)
                predicates.append(f"(MESSAGE_SET_NBR = {int(s)} AND MESSAGE_NBR = {int(n)})")
            except (ValueError, AttributeError):
                pass
        if predicates and tbl(schema, "PSMSGCATDEFN"):
            queries.append(("messages",
                "SELECT * FROM PSMSGCATDEFN WHERE " + " OR ".join(predicates) +
                " ORDER BY MESSAGE_SET_NBR, MESSAGE_NBR"))

    # URL Definitions
    if deps.get("urls") and tbl(schema, "PSURLDEFN"):
        url_id_col = next((c for c in ("URL_ID", "URLID") if col(schema, "PSURLDEFN", c)), "")
        if url_id_col:
            # Pull description and flags only — URL value itself may contain credentials
            safe_cols = [c for c in schema["PSURLDEFN"] if c.upper() not in {"URL", "URLTEXT", "PASSWORD"}]
            projection = ", ".join(safe_cols) if safe_cols else "*"
            queries.append(("url_defs",
                f"SELECT {projection} FROM PSURLDEFN WHERE upper({url_id_col}) IN {sql_in(deps['urls'])}"))

    # IB handler — is this AE registered as a handler?
    if tbl(schema, "PSOPERATIONAE") and col(schema, "PSOPERATIONAE", "AE_APPLID"):
        queries.append(("ib_handler",
            f"SELECT * FROM PSOPERATIONAE WHERE upper(AE_APPLID) = {sql_literal(ae)}"))

    # Records referenced by AE SQL
    recnames = sorted({r.upper().removeprefix("PS_") for r in deps.get("records", [])})
    if recnames and tbl(schema, "PSRECDEFN") and col(schema, "PSRECDEFN", "RECNAME"):
        queries.append(("record_defs",
            f"SELECT * FROM PSRECDEFN WHERE upper(RECNAME) IN {sql_in(recnames)} ORDER BY RECNAME"))
    if recnames and tbl(schema, "PSINDEXDEFN") and col(schema, "PSINDEXDEFN", "RECNAME"):
        queries.append(("index_defs",
            f"SELECT * FROM PSINDEXDEFN WHERE upper(RECNAME) IN {sql_in(recnames)} ORDER BY RECNAME, INDEXID"))
    if recnames and tbl(schema, "PSKEYDEFN") and col(schema, "PSKEYDEFN", "RECNAME"):
        queries.append(("index_keys",
            f"SELECT * FROM PSKEYDEFN WHERE upper(RECNAME) IN {sql_in(recnames)} ORDER BY RECNAME, INDEXID, KEYPOSN"))

    # Plugin AE actions + SQL text (via SQLID LIKE pattern — reliable since AE SQL IDs use AE prefix)
    if plugin_aes:
        if tbl(schema, "PSAESTMTDEFN") and col(schema, "PSAESTMTDEFN", "AE_APPLID"):
            queries.append(("plugin_actions",
                f"SELECT * FROM PSAESTMTDEFN WHERE upper(AE_APPLID) IN {sql_in(plugin_aes)} ORDER BY AE_APPLID, AE_SECTION, AE_STEP"))
        if tbl(schema, "PSSQLTEXTDEFN"):
            sql_like_preds = " OR ".join(
                f"upper(SQLID) LIKE {sql_literal(p + '%')}" for p in plugin_aes
            )
            queries.append(("plugin_sql",
                f"SELECT * FROM PSSQLTEXTDEFN WHERE ({sql_like_preds}) ORDER BY SQLID, DBTYPE, SEQNUM"))
        if tbl(schema, "PSPCMTXT"):
            ov = sorted(c for c in schema["PSPCMTXT"] if re.match(r"^OBJECTVALUE[1-7]$", c))
            if ov:
                pred = " OR ".join(f"upper({ov[0]}) = {sql_literal(p)}" for p in plugin_aes)
                order = ",".join(sorted(c for c in schema["PSPCMTXT"] if re.match(r"^(OBJECTID|OBJECTVALUE|PROGSEQ)", c))) or "1"
                queries.append(("plugin_peoplecode",
                    f"SELECT * FROM PSPCMTXT WHERE ({pred}) ORDER BY {order}"))

    return queries


# ---------------------------------------------------------------------------
# Review package renderer
# ---------------------------------------------------------------------------

def render(
    ae: str,
    db_identity: dict[str, str],
    core: dict[str, list[dict[str, Any]]],
    phase2: dict[str, list[dict[str, Any]]],
    deps: dict[str, list[str]],
    dep_data: dict[str, list[dict[str, Any]]],
) -> str:
    out: list[str] = []
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def h(level: int, title: str) -> None:
        out.append(f"\n{'#' * level} {title}\n")

    def note(text: str) -> None:
        out.append(f"> {text}\n")

    # ---- Header ----
    out.append(f"# AE Review Package: {ae}")
    out.append(
        f"\n**Database:** `{db_identity.get('db_name', '?')}` | "
        f"**User:** `{db_identity.get('session_user', '?')}` | "
        f"**Extracted:** {now}\n"
    )

    # ---- 1. Program Header ----
    h(2, "1. Program Header (`PSAEAPPLDEFN`)")
    prog_rows = core.get("program_header", [])
    if prog_rows:
        p = prog_rows[0]
        out.append(md_table(["Field", "Value"], [
            ["AE_APPLID",         row_value(p, "ae_applid")],
            ["Description",       row_value(p, "descr")],
            ["Program Type",      row_value(p, "aeprogtype") + " (1=Standard, 2=Import, 4=XSLT, 5=Message)"],
            ["Disable Restart",   row_value(p, "ae_disable_restart") + " (Y=disabled, N=restartable)"],
            ["Library Flag",      row_value(p, "ae_appllibrary") + " (Y=library, no scheduler entry)"],
            ["Temp Table Instances", row_value(p, "temptblinstances") + " (0=shared tables)"],
            ["Message Set",       row_value(p, "message_set_nbr")],
            ["Date Override",     row_value(p, "ae_date_override", default="(none)")],
        ]))
    else:
        out.append(f"**No program header found for `{ae}`.** Verify the AE_APPLID spelling.\n")

    # ---- 2. AE Action Plugins ----
    h(2, "2. AE Action Plugins (`PS_PTAE_ACT_PLUGIN`)")
    all_plugins = core.get("plugins", [])
    target_plugins = [r for r in all_plugins if row_value(r, "ae_applid").upper() == ae.upper()]
    source_plugins = [r for r in all_plugins if row_value(r, "ptae_plug_applid").upper() == ae.upper()]

    h(3, "2a. Steps in this AE overridden by a plugin (it is the TARGET)")
    if target_plugins:
        note("Mode: R=Replace (delivered action skipped), B=Before, A=After. "
             "For ENABLED=Y + Mode=R, the delivered SQL/PC in Sections 6/7 does NOT run "
             "-- review Section 8l instead.")
        out.append(md_table(
            ["Section", "Step", "Action Type", "Enabled", "Plugin AE", "Plugin Section", "Plugin Step", "Plugin Action", "Mode", "Seq"],
            [[row_value(r, "ptae_section"), row_value(r, "ptae_step"), row_value(r, "ptae_action_type"),
              row_value(r, "enabled"), row_value(r, "ptae_plug_applid"), row_value(r, "ptae_plug_section"),
              row_value(r, "ptae_plug_step"), row_value(r, "ptae_plug_actntype"),
              row_value(r, "ptae_plug_mode"), row_value(r, "ptae_mode_seq")]
             for r in target_plugins]
        ))
    else:
        out.append("No plugins found targeting this AE — all delivered steps run as-is.\n")

    h(3, "2b. This AE is itself a plugin injected into another AE (it is the PLUGIN)")
    if source_plugins:
        note("This AE is not run standalone — review it in the context of the target AE listed below.")
        out.append(md_table(
            ["Target AE", "Target Section", "Target Step", "Target Action", "Plugin Section", "Plugin Step", "Mode", "Enabled"],
            [[row_value(r, "ae_applid"), row_value(r, "ptae_section"), row_value(r, "ptae_step"),
              row_value(r, "ptae_action_type"), row_value(r, "ptae_plug_section"),
              row_value(r, "ptae_plug_step"), row_value(r, "ptae_plug_mode"), row_value(r, "enabled")]
             for r in source_plugins]
        ))
    else:
        out.append("This AE is not registered as a plugin for any other AE.\n")

    # ---- 3. Process Definition ----
    h(2, "3. Process Definition (`PS_PRCSDEFN`)")
    proc_rows = core.get("process_def", [])
    if proc_rows:
        pr = proc_rows[0]
        out.append(md_table(["Field", "Value"], [
            ["PRCSNAME",        row_value(pr, "prcsname")],
            ["Process Type",    row_value(pr, "prcstype")],
            ["Restart Enabled", row_value(pr, "restartenabled") + " (1=enabled — cross-check vs AE_DISABLE_RESTART in §1)"],
            ["Max Concurrent",  row_value(pr, "maxconcurrent") + " (0=unlimited — risk if shared tables, see §1 TEMPTBLINSTANCES)"],
            ["Run Location",    row_value(pr, "runlocation")],
            ["Server",          row_value(pr, "servername")],
            ["Timeout Minutes", row_value(pr, "timeoutminutes")],
            ["Timeout Max",     row_value(pr, "timeoutmaxmins")],
            ["Retry Count",     row_value(pr, "retrycount")],
            ["Category",        row_value(pr, "prcscategory")],
        ]))
    else:
        out.append(f"No PS_PRCSDEFN row found for `{ae}`.\n")

    job_rows = core.get("job_callers", [])
    if job_rows:
        jobs = sorted({row_value(r, "prcsjobname") for r in job_rows if row_value(r, "prcsjobname")})
        out.append(f"**Scheduled by jobs:** {', '.join(f'`{j}`' for j in jobs)}\n")

    # ---- 4. State Records ----
    h(2, "4. State Records (`PSAEAPPLSTATE`)")
    state_rows = core.get("state_records", [])
    if state_rows:
        out.append(md_table(
            ["State Record", "Default (Y=used for unqualified %State refs)"],
            [[row_value(r, "ae_state_recname"), row_value(r, "ae_default_state")] for r in state_rows]
        ))
        for r in state_rows:
            recname = row_value(r, "ae_state_recname")
            col_rows = phase2.get(f"aet_{recname.lower()}", [])
            if col_rows:
                h(3, f"Columns: PS_{recname}")
                out.append(md_table(
                    ["Column", "Data Type", "Length"],
                    [[row_value(cr, "column_name"), row_value(cr, "data_type"), row_value(cr, "data_length")]
                     for cr in col_rows]
                ))
            else:
                out.append(f"_Could not retrieve columns for PS_{recname} (may be a view or Derived/Work record)._\n")
    else:
        out.append("No state records found in PSAEAPPLSTATE.\n")

    # ---- 5. Section / Step Flow ----
    h(2, "5. Section / Step Flow")

    section_rows = core.get("sections", [])
    if section_rows:
        h(3, "5a. Sections (`PSAESECTDEFN`)")
        note("AE_PUBLIC_SW = Y means the section is callable from other AE programs.")
        out.append(md_table(
            ["Section", "Type", "Public", "Market", "Description"],
            [[row_value(r, "ae_section"), row_value(r, "ae_section_type"),
              row_value(r, "ae_public_sw"), row_value(r, "market"),
              row_value(r, "descr")] for r in section_rows]
        ))
        h(3, "5b. Steps and Actions")

    step_rows = core.get("steps", [])
    action_rows = core.get("actions", [])
    action_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for ar in action_rows:
        key = (row_value(ar, "ae_section").upper(), row_value(ar, "ae_step").upper())
        action_map[key].append(ar)

    if step_rows:
        out.append(md_table(
            ["Section", "Step", "Seq", "Active", "Commit After", "On No Rows", "Action Types", "Call Target"],
            [[
                row_value(r, "ae_section"),
                row_value(r, "ae_step"),
                row_value(r, "ae_seq_num"),
                row_value(r, "ae_active_status", default="?"),
                row_value(r, "ae_commit_after"),
                row_value(r, "ae_on_norows"),
                ", ".join(sorted({
                    AE_STMT_TYPES.get(row_value(a, "ae_stmt_type").upper(), row_value(a, "ae_stmt_type"))
                    for a in action_map.get(
                        (row_value(r, "ae_section").upper(), row_value(r, "ae_step").upper()), []
                    )
                })),
                (row_value(r, "ae_do_appl_id") + "." if row_value(r, "ae_do_appl_id") else "")
                + row_value(r, "ae_do_section"),
            ] for r in step_rows]
        ))
    else:
        out.append("No steps found.\n")

    # ---- 6. SQL Actions ----
    h(2, "6. SQL Actions (Assembled)")
    sql_by_id = assemble_sql_by_id(phase2.get("sql_text", []))

    sql_type_set = {"S", "D", "H", "W", "N"}
    sql_actions = [ar for ar in action_rows if row_value(ar, "ae_stmt_type").upper() in sql_type_set]
    if sql_actions:
        for ar in sql_actions:
            sec = row_value(ar, "ae_section")
            step = row_value(ar, "ae_step")
            stype = row_value(ar, "ae_stmt_type").upper()
            sqlid = row_value(ar, "sqlid").upper()
            descr = row_value(ar, "descr")
            reuse = row_value(ar, "ae_reuse_stmt")
            type_label = AE_STMT_TYPES.get(stype, stype)
            h(3, f"{sec}.{step} ({type_label})" + (f" — {descr}" if descr else ""))
            if sqlid:
                out.append(f"SQLID: `{sqlid}` | Reuse: `{reuse}`\n")
            text = sql_by_id.get(sqlid, "")
            if text.strip():
                out.append(md_fence(text, "sql"))
            else:
                out.append(f"_No SQL text found for SQLID `{sqlid}` in PSSQLTEXTDEFN._\n")
    else:
        out.append("No SQL-type actions found.\n")

    # ---- 7. PeopleCode Actions ----
    h(2, "7. PeopleCode Actions (Assembled)")
    pc_rows = core.get("peoplecode", [])
    # Index by (section=OV2, step=OV6) for AE PeopleCode (OBJECTVALUE1=AE)
    pc_by_step: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pc_rows:
        sec = row_value(row, "objectvalue2")
        step = row_value(row, "objectvalue6")
        if sec:
            pc_by_step[(sec.upper(), step.upper())].append(row)

    pc_actions = [ar for ar in action_rows if row_value(ar, "ae_stmt_type").upper() == "P"]
    if pc_actions:
        for ar in pc_actions:
            sec = row_value(ar, "ae_section")
            step = row_value(ar, "ae_step")
            descr = row_value(ar, "descr")
            h(3, f"{sec}.{step} (PeopleCode)" + (f" — {descr}" if descr else ""))
            step_pc = pc_by_step.get((sec.upper(), step.upper()), [])
            variant_rows = prefer_gbl_default(step_pc) if step_pc else []
            text = concat_pctext(variant_rows)
            if text.strip():
                out.append(md_fence(text, "peoplecode"))
            else:
                out.append(f"_No PSPCMTXT rows for `{sec}.{step}`. Check OBJECTVALUE1={ae}, OV2={sec}, OV6={step}._\n")
    else:
        out.append("No PeopleCode actions found.\n")

    # ---- 8. Dependencies ----
    h(2, "8. Referenced Dependencies")

    dep_pc_rows = dep_data.get("dep_peoplecode", [])
    packages = sorted({cls.split(":")[0] for cls in deps.get("app_classes", [])})
    funclib_recs = sorted({fl.split(".")[0] for fl in deps.get("funclibrary", [])})

    # 8a. App Package Classes
    if deps.get("app_classes"):
        h(3, "8a. App Package Classes")
        for cls_ref in deps["app_classes"]:
            parts = cls_ref.split(":")
            pkg = parts[0]
            cls_name = parts[-1] if len(parts) > 1 else ""
            h(4, f"`{cls_ref}`")
            # Rows for this package root
            pkg_rows = [r for r in dep_pc_rows if row_value(r, "objectvalue1").upper() == pkg.upper()]
            if cls_name:
                # Try to narrow by class name in OV2..OV7
                cls_rows = [
                    r for r in pkg_rows
                    if any(row_value(r, f"objectvalue{i}").upper() == cls_name.upper() for i in range(2, 8))
                ]
                if not cls_rows:
                    cls_rows = pkg_rows  # fallback to all package rows
            else:
                cls_rows = pkg_rows
            text = concat_by_object_key(cls_rows)
            if text.strip():
                out.append(md_fence(text, "peoplecode"))
            else:
                out.append(f"_No PSPCMTXT rows for package `{pkg}`._\n")

        # App Package class hierarchy
        pkg_classes = dep_data.get("pkg_classes", [])
        if pkg_classes:
            h(4, "Package Class Hierarchy")
            out.append(md_table(
                ["Package Root", "Sub-path", "App Class ID", "Description"],
                [[row_value(r, "packageroot"), row_value(r, "qualifypath"),
                  row_value(r, "appclassid"), row_value(r, "descr")] for r in pkg_classes]
            ))

    # 8b. FUNCLIB PeopleCode
    if deps.get("funclibrary"):
        h(3, "8b. FUNCLIB (Record PeopleCode) Functions")
        for fl_ref in deps["funclibrary"]:
            parts = fl_ref.split(".")
            rec = parts[0]
            fld = parts[1] if len(parts) > 1 else ""
            h(4, f"`{fl_ref}`")
            fl_rows = [r for r in dep_pc_rows if row_value(r, "objectvalue1").upper() == rec.upper()]
            if fld:
                fl_rows = [r for r in fl_rows if row_value(r, "objectvalue2").upper() == fld.upper()]
            fl_rows.sort(key=lambda r: int(row_value(r, "progseq", default="0") or "0"))
            text = "".join(raw_value(r, "pctext") for r in fl_rows)
            if text.strip():
                out.append(md_fence(text, "peoplecode"))
            else:
                out.append(f"_No PSPCMTXT rows for `{fl_ref}`._\n")

    # 8c. Named SQL
    if deps.get("named_sql"):
        h(3, "8c. Named SQL Objects")
        named_sql_assembled = assemble_sql_by_id(dep_data.get("named_sql_text", []))
        named_hdr_rows = dep_data.get("named_sql_headers", [])
        for sql_name in deps["named_sql"]:
            h(4, f"`SQL.{sql_name}`")
            hdr = next((r for r in named_hdr_rows if row_value(r, "sqlid").upper() == sql_name.upper()), None)
            if hdr:
                out.append(f"Type: `{row_value(hdr, 'sqltype')}` | Updated: `{row_value(hdr, 'lastupddttm')}` | By: `{row_value(hdr, 'lastupdoprid')}`\n")
            text = named_sql_assembled.get(sql_name.upper(), "")
            if text.strip():
                out.append(md_fence(text, "sql"))
            else:
                out.append(f"_No text found for SQL.{sql_name} in PSSQLTEXTDEFN._\n")

    # 8d. Strings Table
    if dep_data.get("strings_tbl"):
        h(3, "8d. Strings Table Values (`PS_STRINGS_TBL`)")
        out.append(md_table(
            ["PROGRAM_ID", "STRING_ID", "LABEL_ID", "Type", "Default Label", "String Text"],
            [[row_value(r, "program_id"), row_value(r, "string_id"), row_value(r, "label_id"),
              row_value(r, "str_lbltype"), row_value(r, "default_label"), row_value(r, "string_text")]
             for r in dep_data["strings_tbl"]]
        ))

    # 8e. File Layouts
    if deps.get("file_layouts"):
        h(3, "8e. File Layouts")
        fl_hdrs = dep_data.get("fl_header", [])
        fl_segs = dep_data.get("fl_segments", [])
        fl_flds = dep_data.get("fl_fields", [])
        for layout in deps["file_layouts"]:
            h(4, f"Layout: `{layout}`")
            hdr = next((r for r in fl_hdrs if row_value(r, "flddefnname").upper() == layout.upper()), None)
            if hdr:
                out.append(md_table(["Field", "Value"], [
                    ["Description",    row_value(hdr, "descr")],
                    ["Format",         row_value(hdr, "fldformat")],
                    ["Delimiter",      row_value(hdr, "flddelimiter")],
                    ["Default Path",   row_value(hdr, "fldfilename") + (" ← CHECK: hard-coded path?" if row_value(hdr, "fldfilename") else "")],
                    ["Segment Count",  row_value(hdr, "fldsegcount")],
                ]))
            segs = [r for r in fl_segs if row_value(r, "flddefnname").upper() == layout.upper()]
            if segs:
                out.append("**Segments:**\n")
                out.append(md_table(
                    ["Segment", "ID", "Parent", "ID Start", "ID Length", "Record", "Seq"],
                    [[row_value(r, "fldsegname"), row_value(r, "fldsegid"), row_value(r, "fldsegparent"),
                      row_value(r, "fldsegidstart"), row_value(r, "fldsegidlength"),
                      row_value(r, "recname_file"), row_value(r, "fldseqno")] for r in segs]
                ))
            flds = [r for r in fl_flds if row_value(r, "flddefnname").upper() == layout.upper()]
            if flds:
                out.append("**Fields** (FLDFIELDTYPE: 0=Char, 2=Number):\n")
                out.append(md_table(
                    ["Segment", "Field", "Start", "Length", "Type", "Decimal", "Trim", "Seq"],
                    [[row_value(r, "fldsegname"), row_value(r, "fldfieldname"),
                      row_value(r, "fldstart"), row_value(r, "fldlength"),
                      row_value(r, "fldfieldtype"), row_value(r, "decimal_pos"),
                      row_value(r, "fldtrimspaces"), row_value(r, "fldseqno")] for r in flds]
                ))

    # 8f. Component Interfaces
    if deps.get("component_interfaces"):
        h(3, "8f. Component Interfaces")
        ci_hdrs = dep_data.get("ci_headers", [])
        ci_items = dep_data.get("ci_items", [])
        for ci in deps["component_interfaces"]:
            h(4, f"CI: `{ci}`")
            hdr = next((r for r in ci_hdrs if row_value(r, "bcname").upper() == ci.upper()), None)
            if hdr:
                out.append(md_table(["Field", "Value"], [
                    ["Display Name",   row_value(hdr, "bcdisplayname")],
                    ["Page",           row_value(hdr, "bcpgname")],
                    ["Menu",           row_value(hdr, "menuname")],
                    ["Search Record",  row_value(hdr, "searchrecname")],
                    ["Market",         row_value(hdr, "market")],
                ]))
            items = [r for r in ci_items if row_value(r, "bcname").upper() == ci.upper()]
            if items:
                note("BCACCESS: read-only properties the AE sets silently do nothing.")
                out.append(md_table(
                    ["Type", "Parent", "Item Name", "Access", "Record", "Field", "Seq"],
                    [[row_value(r, "bctype"), row_value(r, "bcitemparent"), row_value(r, "bcitemname"),
                      row_value(r, "bcaccess"), row_value(r, "recname"), row_value(r, "fieldname"),
                      row_value(r, "sequence_nbr_6")] for r in items]
                ))

    # 8g. Message Catalog
    if dep_data.get("messages"):
        h(3, "8g. Message Catalog (`PSMSGCATDEFN`)")
        out.append(md_table(
            ["Set", "Number", "Severity", "Message Text"],
            [[row_value(r, "message_set_nbr"), row_value(r, "message_nbr"),
              row_value(r, "msg_severity"), row_value(r, "message_text")]
             for r in dep_data["messages"]]
        ))

    # 8h. URL Definitions
    if dep_data.get("url_defs"):
        h(3, "8h. URL Definitions (`PSURLDEFN`)")
        note("URL column values are excluded from this package — check PSURLDEFN.URL directly "
             "for hard-coded credentials or environment-specific hosts.")
        url_row = dep_data["url_defs"][0] if dep_data["url_defs"] else {}
        display_cols = [k for k in url_row if k.upper() not in {"URL", "URLTEXT", "PASSWORD"}]
        if display_cols:
            out.append(md_table(
                display_cols,
                [[row_value(r, c) for c in display_cols] for r in dep_data["url_defs"]]
            ))

    # 8i. Integration Broker Handler
    if dep_data.get("ib_handler"):
        h(3, "8i. IB Service Operation Handler (`PSOPERATIONAE`)")
        note("This AE is registered as an IB handler — review in context of the inbound message, not standalone.")
        out.append(md_table(
            ["Operation", "Handler Name", "Package Root", "App Class", "Method"],
            [[row_value(r, "ib_operationname"), row_value(r, "handlername"),
              row_value(r, "packageroot"), row_value(r, "appclassid"),
              row_value(r, "appclassmethod")] for r in dep_data["ib_handler"]]
        ))

    # 8j. Records Referenced
    if dep_data.get("record_defs"):
        h(3, "8j. Records Referenced in SQL")
        out.append(md_table(
            ["Record", "Type", "SQL Table Name", "Field Count"],
            [[row_value(r, "recname"),
              REC_TYPE_LABELS.get(row_value(r, "rectype"), row_value(r, "rectype")),
              row_value(r, "sqltablename"),
              row_value(r, "fieldcount")] for r in dep_data["record_defs"]]
        ))

    # 8k. Indexes
    if dep_data.get("index_defs") and dep_data.get("index_keys"):
        h(3, "8k. Indexes (for performance analysis)")
        idx_rows = dep_data["index_defs"]
        key_rows = dep_data["index_keys"]
        unique_map = {"U": "Unique", "D": "Duplicate", "H": "Unique(+)", "": ""}
        for idx in idx_rows:
            recname = row_value(idx, "recname")
            idxid   = row_value(idx, "indexid")
            active  = row_value(idx, "activeflag")
            ora     = row_value(idx, "platform_ora")
            unique  = unique_map.get(row_value(idx, "uniqueflag"), row_value(idx, "uniqueflag"))
            keys = [k for k in key_rows
                    if row_value(k, "recname").upper() == recname.upper()
                    and row_value(k, "indexid") == idxid]
            key_list = ", ".join(
                f"{row_value(k, 'fieldname')} {row_value(k, 'ascdesc')}" for k in keys
            )
            out.append(f"`{recname}` Index **{idxid}** ({unique}) — Active: `{active}`, Oracle: `{ora}` — `{key_list}`\n")

    # 8l. Plugin AE Source
    if deps.get("plugin_aes"):
        h(3, "8l. Plugin AE Source")
        plugin_actions = dep_data.get("plugin_actions", [])
        plugin_sql_assembled = assemble_sql_by_id(dep_data.get("plugin_sql", []))
        plugin_pc_rows = dep_data.get("plugin_peoplecode", [])

        for plug_ae in deps["plugin_aes"]:
            h(4, f"Plugin AE: `{plug_ae}`")
            p_actions = [r for r in plugin_actions if row_value(r, "ae_applid").upper() == plug_ae.upper()]
            if p_actions:
                out.append(md_table(
                    ["Section", "Step", "Type", "SQLID", "Description"],
                    [[row_value(r, "ae_section"), row_value(r, "ae_step"),
                      AE_STMT_TYPES.get(row_value(r, "ae_stmt_type").upper(), row_value(r, "ae_stmt_type")),
                      row_value(r, "sqlid"), row_value(r, "descr")] for r in p_actions]
                ))
                for ar in p_actions:
                    stype = row_value(ar, "ae_stmt_type").upper()
                    sqlid = row_value(ar, "sqlid").upper()
                    sec = row_value(ar, "ae_section")
                    step_name = row_value(ar, "ae_step")
                    if stype in {"S", "D", "H", "W", "N"} and sqlid:
                        text = plugin_sql_assembled.get(sqlid, "")
                        if text.strip():
                            h(5, f"{sec}.{step_name} SQL (`{sqlid}`)")
                            out.append(md_fence(text, "sql"))
                    elif stype == "P":
                        p_pc = [r for r in plugin_pc_rows if row_value(r, "objectvalue1").upper() == plug_ae.upper()]
                        step_pc = [r for r in p_pc
                                   if row_value(r, "objectvalue2").upper() == sec.upper()
                                   and row_value(r, "objectvalue6").upper() == step_name.upper()]
                        variant_rows = prefer_gbl_default(step_pc) if step_pc else []
                        text = concat_pctext(variant_rows)
                        if text.strip():
                            h(5, f"{sec}.{step_name} PeopleCode")
                            out.append(md_fence(text, "peoplecode"))

    # ---- Reviewer Notes ----
    out.append("\n---\n")
    h(2, "Reviewer Notes — Checklist")
    out.append("""
Apply the review checklist to the source above. Key reminders:

1. **Plugins first (Sec 2):** Any ENABLED=Y row with Mode=R means the delivered SQL/PeopleCode
   in Sec 6/7 is bypassed -- review the plugin source in Sec 8l as the real logic.
2. **Restart cross-check (Sec 1 vs Sec 3):** `AE_DISABLE_RESTART` (program) and `RESTARTENABLED`
   (scheduler) are independent switches and must agree. File-writing and external-call AEs
   (FTP, web service, payment) should disable restart -- a re-run can re-send work that already
   left the system. Also check for open File handles or Component variables set in an early
   section: a mid-run abend + restart resumes past the init step.
3. **Date binds (Sec 4 + Sec 6):** Verify the AET column is type `DATE` before flagging an
   implicit conversion. `%Select(... TO_CHAR(dt,'YYYY-MM-DD') ...)` into a DATE field with
   `%Bind(dateField)` is correct PeopleSoft. Do not raise a false positive here.
4. **Shared staging (Sec 1 + Sec 8j):** `TEMPTBLINSTANCES = 0` + `MaxConcurrent = 0` means
   concurrent runs share real tables. Check every INSERT/UPDATE has a scoping predicate
   (PROCESS_INSTANCE, OPRID, ...) so runs cannot steal each other's rows. `RECTYPE = 7` in
   Sec 8j confirms a record really is a temp table.
5. **Commits in loops (Sec 5b):** `AE_COMMIT_AFTER` against Do Select loops and file writes --
   a commit inside a fetch loop can invalidate the cursor or split a logical unit of work.
6. **Performance (Sec 6 + Sec 8k):** Row-by-row Do Select + per-row PeopleCode over large tables;
   OR predicates across columns that defeat indexes; functions on columns (`upper(col) =`);
   missing `%Bind` or hard-coded literals. Back a claim with the index list in Sec 8k rather
   than asserting "no index" -- check `ACTIVEFLAG` and Oracle platform first.
7. **App Package / FUNCLIB (Sec 8a / 8b):** Review the live path. Common finds: helpers that
   abend on missing config, file-transfer classes building shell commands via `Exec` or
   embedding credentials, code writing status/control fields on another record without
   re-validating, and old block-commented (`<* ... *>`) implementations beside the active one.
8. **File Layout (Sec 8e):** FLDFIELDTYPE=2 (numeric, strips leading zeros) treated as char by
   the code; field positions/lengths that do not match what the program parses; a hard-coded
   personal or dev-share path left in FLDFILENAME; a segment the AE never handles.
9. **Component Interface (Sec 8f):** BCACCESS -- a property the AE sets that the CI exposes
   read-only silently does nothing. The CI also runs the online component's PeopleCode, so the
   AE inherits its defaulting and edits.
10. **Message Catalog (Sec 8g):** Count `%1`/`%2` placeholders in Message Text against the
    parameters actually passed -- a mismatch produces a useless operator log line.
11. **IB handler (Sec 8i):** If this AE appears there it is not run standalone -- scope the
    review to the inbound message rather than a scheduled run.
12. **Loose ends:** unused TEMPTBLINSTANCES, unguarded file `Close`/`WriteLine`, `AE_ON_NOROWS`
    behavior, inactive or `**OBSOLETE**` steps, and speculative code paths never configured.
""")

    return "\n".join(str(x) for x in out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ae", required=True, help="AE_APPLID (auto-uppercased)")
    parser.add_argument("--database", required=True, help="SQLcl saved-connection name")
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--sqlcl", help="Path to sql / sql.exe (defaults to PATH)")
    ns = parser.parse_args(argv)
    ns.ae = ns.ae.strip().upper()
    if not IDENTIFIER_RE.fullmatch(ns.ae):
        parser.error("--ae must be a valid PeopleSoft identifier (A-Z, 0-9, underscore, start with letter).")
    if not CONNECTION_RE.fullmatch(ns.database):
        parser.error("--database contains unsupported characters.")
    return ns


def main(argv: Sequence[str] | None = None) -> int:
    ns = parse_args(argv)
    ae = ns.ae
    database = ns.database
    output_dir: Path = ns.output_dir.resolve()

    try:
        runner = SQLclRunner(SQLclRunner.resolve(ns.sqlcl), database)

        print(f"[1/5] Checking saved connections for '{database}'...", file=sys.stderr)
        connections = runner.list_connections()
        if database not in connections:
            raise ExtractorError(
                f"Connection '{database}' not in SQLcl. "
                f"Available: {', '.join(connections) or 'none'}"
            )

        print("[2/5] Verifying connection...", file=sys.stderr)
        db_identity = runner.verify_connection()
        print(f"      {db_identity['db_name']} as {db_identity['session_user']}", file=sys.stderr)

        print("[3/5] Inspecting table columns...", file=sys.stderr)
        schema = inspect_columns(runner, CORE_TABLES + DEP_TABLES)
        found = sorted(t for t in CORE_TABLES + DEP_TABLES if tbl(schema, t))
        print(f"      Tables found: {len(found)}", file=sys.stderr)

        print("[4/5] Extracting core AE metadata (header, sections, steps, actions, "
              "PeopleCode, plugins, process def)...", file=sys.stderr)
        core_queries = build_core_queries(ae, schema)
        core = runner.run_queries(core_queries)

        if not core.get("program_header"):
            raise ExtractorError(f"AE '{ae}' not found in PSAEAPPLDEFN on '{database}'.")
        descr = row_value(core["program_header"][0], "descr")
        print(f"      Found: {ae} — {descr}", file=sys.stderr)

        # Phase 2: SQL text + state record columns
        sql_ids = sorted({
            row_value(r, "sqlid").upper()
            for r in core.get("actions", [])
            if row_value(r, "sqlid")
        })
        state_recnames = [row_value(r, "ae_state_recname") for r in core.get("state_records", [])]
        print(f"      SQL IDs: {len(sql_ids)}, State records: {len(state_recnames)}", file=sys.stderr)

        sql_state_queries = build_sql_and_state_queries(schema, sql_ids, state_recnames)
        phase2 = runner.run_queries(sql_state_queries) if sql_state_queries else {}

        # Dependency scanning
        all_sql  = " ".join(raw_value(r, "sqltext") for r in phase2.get("sql_text", []))
        all_pc   = " ".join(raw_value(r, "pctext") for r in core.get("peoplecode", []))
        plugins  = core.get("plugins", [])
        deps     = scan_for_dependencies(ae, all_sql, all_pc, core.get("actions", []), core.get("steps", []), plugins)
        plugin_aes = deps.pop("plugin_aes", [])

        dep_summary = ", ".join(f"{k}={len(v)}" for k, v in deps.items() if v)
        print(f"      Dependencies: {dep_summary or 'none'}", file=sys.stderr)
        if plugin_aes:
            print(f"      Plugin AEs: {', '.join(plugin_aes)}", file=sys.stderr)

        print("[5/5] Extracting dependencies (App Packages, FUNCLIB, SQL objs, File Layouts, "
              "CIs, Messages, URLs, IB, Records, Indexes, Plugin AE code)...", file=sys.stderr)
        dep_queries = build_dep_queries(schema, deps, ae, plugin_aes)
        dep_data = runner.run_queries(dep_queries) if dep_queries else {}

        deps["plugin_aes"] = plugin_aes

        print("      Assembling review_package.md...", file=sys.stderr)
        markdown = render(ae, db_identity, core, phase2, deps, dep_data)

        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"{ae}_review_package.md"
        out_path.write_text(markdown, encoding="utf-8")

        line_count = len(markdown.splitlines())
        print(f"\nDone. {line_count} lines → {out_path}", file=sys.stderr)
        print(json.dumps({
            "ae": ae,
            "database": database,
            "db_name": db_identity.get("db_name"),
            "output": str(out_path),
            "lines": line_count,
        }))
        return 0

    except (ExtractorError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
