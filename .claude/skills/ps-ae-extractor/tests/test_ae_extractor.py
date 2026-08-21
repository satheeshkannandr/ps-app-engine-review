"""Tests for ae_extractor pure logic — no database required."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ae_extractor as ex  # noqa: E402


# ---------------------------------------------------------------------------
# SQL literal / IN-list safety
# ---------------------------------------------------------------------------

def test_sql_literal_escapes_quotes():
    assert ex.sql_literal("O'BRIEN") == "'O''BRIEN'"


def test_sql_in_uppercases_dedupes_sorts():
    assert ex.sql_in(["b", "A", "a"]) == "('A','B')"


def test_sql_in_empty_is_safe():
    assert ex.sql_in([]) == "('__EMPTY__')"


# ---------------------------------------------------------------------------
# DBTYPE variant selection — the "don't glue Oracle + DB2 together" rule
# ---------------------------------------------------------------------------

def test_prefers_oracle_dbtype_over_default():
    rows = [
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": " ", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "GENERIC"},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "ORACLE"},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "7", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "MSSQL"},
    ]
    assert ex.assemble_sql_by_id(rows) == {"X": "ORACLE"}


def test_falls_back_to_blank_dbtype_when_no_oracle():
    rows = [
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": " ", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "GENERIC"},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "1", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "DB2ZOS"},
    ]
    assert ex.assemble_sql_by_id(rows) == {"X": "GENERIC"}


def test_concatenates_chunks_in_seqnum_order():
    rows = [
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "2", "sqltext": " WHERE A=1"},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "SELECT *"},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "1", "sqltext": " FROM PS_T"},
    ]
    assert ex.assemble_sql_by_id(rows) == {"X": "SELECT * FROM PS_T WHERE A=1"}


def test_picks_latest_effdt():
    rows = [
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "OLD"},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "2020-01-01",
         "seqnum": "0", "sqltext": "NEW"},
    ]
    assert ex.assemble_sql_by_id(rows) == {"X": "NEW"}


def test_separate_sqlids_stay_separate():
    rows = [
        {"sqlid": "A", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "AAA"},
        {"sqlid": "B", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "BBB"},
    ]
    assert ex.assemble_sql_by_id(rows) == {"A": "AAA", "B": "BBB"}


def test_chunk_boundary_whitespace_is_preserved_in_sql():
    """Chunks split mid-statement: trimming a boundary space welds tokens together."""
    rows = [
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "0", "sqltext": "SELECT A "},
        {"sqlid": "X", "sqltype": "0", "market": "GBL", "dbtype": "2", "effdt": "1900-01-01",
         "seqnum": "1", "sqltext": " FROM PS_T"},
    ]
    assert ex.assemble_sql_by_id(rows) == {"X": "SELECT A  FROM PS_T"}


def test_chunk_boundary_whitespace_is_preserved_in_peoplecode():
    rows = [
        {"progseq": "0", "pctext": "If &x = 1 "},
        {"progseq": "1", "pctext": " Then"},
    ]
    assert ex.concat_pctext(rows) == "If &x = 1  Then"


def test_raw_value_does_not_trim():
    assert ex.raw_value({"pctext": "  padded  "}, "pctext") == "  padded  "


def test_raw_value_handles_missing_and_none():
    assert ex.raw_value({}, "pctext") == ""
    assert ex.raw_value({"pctext": None}, "pctext") == ""


def test_row_value_still_trims_identifiers():
    assert ex.row_value({"ae_applid": "  AR_AGING  "}, "ae_applid") == "AR_AGING"


# ---------------------------------------------------------------------------
# PeopleCode market/platform variant selection
# ---------------------------------------------------------------------------

def test_prefers_gbl_default_variant():
    rows = [
        {"objectvalue3": "GBL", "objectvalue4": "default", "progseq": "0", "pctext": "LIVE"},
        {"objectvalue3": "USA", "objectvalue4": "default", "progseq": "0", "pctext": "USVARIANT"},
    ]
    assert ex.concat_pctext(ex.prefer_gbl_default(rows)) == "LIVE"


def test_pc_chunks_concat_in_progseq_order():
    rows = [
        {"objectvalue3": "GBL", "objectvalue4": "default", "progseq": "1", "pctext": "_END"},
        {"objectvalue3": "GBL", "objectvalue4": "default", "progseq": "0", "pctext": "START"},
    ]
    assert ex.concat_pctext(ex.prefer_gbl_default(rows)) == "START_END"


def test_progseq_sorts_numerically_not_lexically():
    """PROGSEQ 10 must come after 2, not before it."""
    rows = [
        {"progseq": "10", "pctext": "TEN"},
        {"progseq": "2", "pctext": "TWO"},
    ]
    assert ex.concat_pctext(rows) == "TWOTEN"


def test_app_package_concat_uses_object_key_not_market():
    """App Package OV3 is the class path, not a market — must not be treated as a variant."""
    rows = [
        {"objectvalue2": "MyClass", "objectvalue3": "OnExecute", "progseq": "1", "pctext": "_TAIL"},
        {"objectvalue2": "MyClass", "objectvalue3": "OnExecute", "progseq": "0", "pctext": "HEAD"},
    ]
    assert ex.concat_by_object_key(rows) == "HEAD_TAIL"


# ---------------------------------------------------------------------------
# Dependency scanning
# ---------------------------------------------------------------------------

def _scan(sql="", pc=""):
    return ex.scan_for_dependencies("MY_AE", sql, pc, [], [], [])


def test_detects_app_package_import():
    deps = _scan(pc="import EXAMPLE_PKG:FileHandler;\nLocal any &h;")
    assert "EXAMPLE_PKG:FILEHANDLER" in deps["app_classes"]


def test_detects_declare_function_funclib():
    deps = _scan(pc="Declare Function GetCfg PeopleCode FUNCLIB_AP.AP_FLD FieldFormula;")
    assert "FUNCLIB_AP.AP_FLD" in deps["funclibrary"]


def test_detects_named_sql_and_excludes_sql_keywords():
    deps = _scan(pc="&r = CreateSQL(SQL.MY_LOOKUP); &r.Close(); &r.Fetch();")
    assert "MY_LOOKUP" in deps["named_sql"]
    assert "CLOSE" not in deps["named_sql"]
    assert "FETCH" not in deps["named_sql"]


def test_detects_file_layout_ci_url():
    deps = _scan(pc="SetFileLayout(FileLayout.BANK_IN); GetCompIntfc(CompIntfc.VCHR_EXPRESS); GetURL(URL.SFTP_HOST);")
    assert "BANK_IN" in deps["file_layouts"]
    assert "VCHR_EXPRESS" in deps["component_interfaces"]
    assert "SFTP_HOST" in deps["urls"]


def test_detects_message_catalog_from_messagebox():
    """MessageBox(style, title, set, nbr, ...) — two args precede the set number."""
    deps = _scan(pc='MessageBox(0, "", 20000, 45, "fallback");')
    assert "20000:45" in deps["messages"]


def test_detects_message_catalog_from_msgget():
    """MsgGet(set, nbr, ...) — set number is the first arg."""
    deps = _scan(pc='&s = MsgGet(25000, 12, "default text");')
    assert "25000:12" in deps["messages"]


def test_detects_message_catalog_from_pct_msgget():
    deps = _scan(sql="%MsgGet(30000, 7, 'x')")
    assert "30000:7" in deps["messages"]


def test_detects_records_from_all_sql_verbs():
    deps = _scan(sql="""
        INSERT INTO PS_TGT_TAO SELECT A.X FROM PS_ITEM A
        JOIN PS_ITEM_ACTIVITY B ON B.K = A.K;
        UPDATE PS_CTL SET F = 1;
        DELETE FROM PS_OLD_TAO;
    """)
    for rec in ("PS_TGT_TAO", "PS_ITEM", "PS_ITEM_ACTIVITY", "PS_CTL", "PS_OLD_TAO"):
        assert rec in deps["records"], rec


def test_detects_strings_table_across_newlines():
    """The PROGRAM_ID predicate is usually on a different line than the table name."""
    deps = _scan(sql="""
        SELECT STRING_TEXT FROM PS_STRINGS_TBL
        WHERE PROGRAM_ID = 'AR_AGING'
          AND STRING_ID = 'HDR'
    """)
    assert "AR_AGING" in deps["strings_programs"]


def test_detects_call_app_engine():
    deps = _scan(pc='CallAppEngine("CHILD_AE", &rec);')
    assert "CHILD_AE" in deps["called_aes"]


def test_call_section_target_from_steps():
    steps = [{"ae_do_appl_id": "OTHER_AE", "ae_do_section": "MAIN"}]
    deps = ex.scan_for_dependencies("MY_AE", "", "", [], steps, [])
    assert "OTHER_AE" in deps["called_aes"]


def test_self_reference_is_not_a_called_ae():
    steps = [{"ae_do_appl_id": "MY_AE", "ae_do_section": "SUB"}]
    deps = ex.scan_for_dependencies("MY_AE", "", "", [], steps, [])
    assert "MY_AE" not in deps["called_aes"]


def test_enabled_plugin_is_collected():
    plugins = [{"enabled": "Y", "ptae_plug_applid": "CUSTOM_PLUG"}]
    deps = ex.scan_for_dependencies("MY_AE", "", "", [], [], plugins)
    assert "CUSTOM_PLUG" in deps["plugin_aes"]


def test_disabled_plugin_is_skipped():
    plugins = [{"enabled": "N", "ptae_plug_applid": "DEAD_PLUG"}]
    deps = ex.scan_for_dependencies("MY_AE", "", "", [], [], plugins)
    assert "DEAD_PLUG" not in deps["plugin_aes"]


# ---------------------------------------------------------------------------
# Markdown rendering safety
# ---------------------------------------------------------------------------

def test_md_table_escapes_pipes():
    out = ex.md_table(["A"], [["x|y"]])
    assert "x\\|y" in out


def test_md_table_flattens_newlines():
    out = ex.md_table(["A"], [["line1\nline2"]])
    assert "line1 line2" in out
    assert out.count("\n") == 3  # header, separator, one row


def test_md_table_handles_none():
    assert "|  |" in ex.md_table(["A"], [[None]])


# ---------------------------------------------------------------------------
# SQLcl output parsing
# ---------------------------------------------------------------------------

def test_parses_json_from_mixed_output():
    text = 'Connected.\n{"results":[{"items":[{"AE_APPLID":"X","DESCR":"Test"}]}]}\nSQL> exit'
    docs = ex.parse_json_documents(text)
    assert len(docs) == 1
    assert ex.rows_from_document(docs[0]) == [{"ae_applid": "X", "descr": "Test"}]


def test_column_keys_are_lowercased():
    doc = {"results": [{"items": [{"MiXeD_CaSe": 1}]}]}
    assert ex.rows_from_document(doc) == [{"mixed_case": 1}]


def test_ignores_json_without_results_key():
    docs = ex.parse_json_documents('{"unrelated": true}')
    assert docs == []


# ---------------------------------------------------------------------------
# Argument validation — SQL injection guards
# ---------------------------------------------------------------------------

def test_rejects_ae_with_sql_metacharacters():
    for bad in ["A'; DROP TABLE X--", "A B", "a-b", "1ABC", ""]:
        try:
            ex.parse_args(["--ae", bad, "--database", "TEST"])
            raise AssertionError(f"should have rejected {bad!r}")
        except SystemExit:
            pass


def test_accepts_valid_ae_and_uppercases():
    ns = ex.parse_args(["--ae", "ar_aging", "--database", "TEST"])
    assert ns.ae == "AR_AGING"


def test_rejects_database_with_metacharacters():
    try:
        ex.parse_args(["--ae", "AR_AGING", "--database", "T EST;"])
        raise AssertionError("should have rejected")
    except SystemExit:
        pass


# ---------------------------------------------------------------------------
# Query builders honour discovered schema
# ---------------------------------------------------------------------------

def test_skips_tables_absent_from_schema():
    queries = ex.build_core_queries("MY_AE", {"PSAEAPPLDEFN": {"AE_APPLID", "DESCR"}})
    names = [n for n, _ in queries]
    assert names == ["program_header"]


def test_skips_order_by_for_missing_columns():
    schema = {"PSAESTEPDEFN": {"AE_APPLID", "AE_SECTION"}}  # no AE_SEQ_NUM
    queries = dict(ex.build_core_queries("MY_AE", schema))
    assert "AE_SEQ_NUM" not in queries["steps"]
    assert "ORDER BY AE_SECTION" in queries["steps"]


def test_ae_name_is_escaped_in_generated_sql():
    queries = dict(ex.build_core_queries("MY_AE", {"PSAEAPPLDEFN": {"AE_APPLID"}}))
    assert "upper(AE_APPLID) = 'MY_AE'" in queries["program_header"]


def test_state_record_query_rejects_bad_recname():
    queries = ex.build_sql_and_state_queries({}, [], ["GOOD_AET", "bad; DROP--"])
    names = [n for n, _ in queries]
    assert names == ["aet_good_aet"]


def test_url_query_omits_credential_columns():
    schema = {"PSURLDEFN": {"URL_ID", "URL", "DESCR", "URLTEXT"}}
    queries = dict(ex.build_dep_queries(schema, {"urls": ["MY_URL"]}, "MY_AE", []))
    assert "URL_ID" in queries["url_defs"]
    assert "DESCR" in queries["url_defs"]
    assert " URL," not in queries["url_defs"] and "URLTEXT" not in queries["url_defs"]


# ---------------------------------------------------------------------------
# End-to-end render smoke test
# ---------------------------------------------------------------------------

def test_render_produces_all_sections():
    core = {
        "program_header": [{
            "ae_applid": "MY_AE", "descr": "Test program", "aeprogtype": "1",
            "ae_disable_restart": "N", "temptblinstances": "0", "message_set_nbr": "20000",
        }],
        "sections": [{"ae_section": "MAIN", "ae_section_type": "P", "ae_public_sw": "N"}],
        "steps": [{
            "ae_section": "MAIN", "ae_step": "Step01", "ae_seq_num": "1",
            "ae_commit_after": "Y", "ae_on_norows": "N",
        }],
        "actions": [{
            "ae_section": "MAIN", "ae_step": "Step01", "ae_stmt_type": "S",
            "sqlid": "MY_AE_1", "descr": "Load data",
        }],
        "state_records": [{"ae_state_recname": "MY_AE_AET", "ae_default_state": "Y"}],
        "process_def": [{"prcsname": "MY_AE", "restartenabled": "1", "maxconcurrent": "0"}],
        "peoplecode": [],
        "plugins": [],
        "job_callers": [],
    }
    phase2 = {
        "sql_text": [{
            "sqlid": "MY_AE_1", "sqltype": "0", "market": "GBL", "dbtype": "2",
            "effdt": "1900-01-01", "seqnum": "0",
            "sqltext": "UPDATE PS_CTL SET F = 1",
        }],
        "aet_my_ae_aet": [{"column_name": "PROCESS_INSTANCE", "data_type": "NUMBER", "data_length": "22"}],
    }
    deps = {"records": ["PS_CTL"], "plugin_aes": []}
    md = ex.render("MY_AE", {"db_name": "TEST", "session_user": "SYSADM"}, core, phase2, deps, {})

    for heading in [
        "# AE Review Package: MY_AE",
        "1. Program Header",
        "2. AE Action Plugins",
        "3. Process Definition",
        "4. State Records",
        "5. Section / Step Flow",
        "6. SQL Actions",
        "7. PeopleCode Actions",
        "8. Referenced Dependencies",
        "Reviewer Notes",
    ]:
        assert heading in md, f"missing: {heading}"

    # The assembled Oracle SQL must appear in a fenced block
    assert "UPDATE PS_CTL SET F = 1" in md
    assert "```sql" in md
    # Restart mismatch evidence must be visible (AE says N, scheduler says 1)
    assert "RESTARTENABLED" in md or "Restart Enabled" in md
    # State record column types must be present for the date-bind check
    assert "PROCESS_INSTANCE" in md


def test_render_survives_missing_program_header():
    md = ex.render("GHOST_AE", {"db_name": "TEST"}, {}, {}, {}, {})
    assert "No program header found" in md
