"""
Stale-file detection + grep fallback (live, per-query; no zone/cache).

Two tests:
  1. read_source_by_grep live fallback (finds symbol, reflects mutations).
  2. End-to-end MCP path (subprocess-isolated so env + server import are clean):
     index a symbol, set index_timestamp in the past, write the source file
     (now stale) → get_function_body returns a "modified since index" banner +
     LIVE grep content; get_symbol appends a stale banner.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_read_source_by_grep(tmp_path):
    import importlib.util as ilu

    spec = ilu.spec_from_file_location("sr", str(REPO / "mcp" / "source_reader.py"))
    sr = ilu.module_from_spec(spec)
    spec.loader.exec_module(sr)

    f = tmp_path / "x.c"
    f.write_text("int foo(void){ return 1; }\nint bar(void){ return 2; }\n")
    out = sr.read_source_by_grep(tmp_path, "x.c", "bar")
    assert out and "bar" in out and "return 2" in out
    # mutation is reflected (live, not cached)
    f.write_text("int bar(void){ return 999; }\n")
    out2 = sr.read_source_by_grep(tmp_path, "x.c", "bar")
    assert out2 and "return 999" in out2
    # missing symbol → None
    assert sr.read_source_by_grep(tmp_path, "x.c", "nope") is None


def test_stale_file_triggers_banner_and_grep_fallback(tmp_path):
    db = tmp_path / ".kgraph" / "kgraph.db"
    db.parent.mkdir()
    (tmp_path / "fs").mkdir()
    (tmp_path / "fs" / "x.c").write_text("int my_func(void){ return 42; }\n")

    env = dict(os.environ, PYTHONPATH=f"{REPO / 'src'}:{REPO / 'scripts'}")

    # 1. build a minimal DB: one symbol my_func @ fs/x.c, index_timestamp in the past
    setup = textwrap.dedent(f"""
        import time
        from storage import SQLiteStore
        s = SQLiteStore({str(db)!r}); s.create_schema()
        s.conn.execute("INSERT OR IGNORE INTO files(path,language) VALUES('fs/x.c','C')")
        fid = s.conn.execute("SELECT id FROM files WHERE path='fs/x.c'").fetchone()[0]
        s.conn.execute(
            "INSERT OR IGNORE INTO symbols(scip_symbol,name,kind,def_file_id,def_start_line,def_end_line) "
            "VALUES('cxx . . $ my_func().','my_func','function',?,0,0)", (fid,))
        s.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('index_timestamp',?)",
                       (str(int(time.time()) - 100),))
        s.conn.commit(); s.close()
    """)
    subprocess.run([sys.executable, "-c", setup], cwd=str(tmp_path),
                   env=env, check=True)

    # 2. query via the MCP server (fresh import, env set) — file mtime > index_timestamp → stale
    query = textwrap.dedent(f"""
        import os, sys
        os.environ['KGRAPH_DB'] = {str(db)!r}
        os.environ['KGRAPH_ROOT'] = {str(tmp_path)!r}
        import importlib.util as ilu
        spec = ilu.spec_from_file_location('kgraph_server', {str(REPO / 'mcp' / 'server.py')!r})
        srv = ilu.module_from_spec(spec); spec.loader.exec_module(srv)
        print('BODY>>>'); print(srv.get_function_body('my_func'))
        print('SYM>>>'); print(srv.get_symbol('my_func'))
    """)
    out = subprocess.run([sys.executable, "-c", query], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True).stdout
    assert "BODY>>>" in out and "SYM>>>" in out, out
    body = out.split("BODY>>>")[1].split("SYM>>>")[0]
    sym = out.split("SYM>>>")[1]

    # get_function_body: stale → banner + LIVE grep content (not stale indexed lines)
    assert "modified since index" in body, body
    assert "LIVE content" in body, body
    assert "return 42" in body, body

    # get_symbol: live stale banner appended
    assert "modified since index" in sym, sym


def test_git_status_refines_reason_to_working_tree(tmp_path):
    """In a git repo, an uncommitted modification → reason 'working_tree', and
    get_function_body returns the LIVE (modified) content via grep fallback."""
    db = tmp_path / ".kgraph" / "kgraph.db"
    db.parent.mkdir()
    (tmp_path / "fs").mkdir()
    xf = tmp_path / "fs" / "x.c"
    xf.write_text("int my_func(void){ return 1; }\n")

    # git init + commit the file (tracked, clean)
    subprocess.run(["git", "init"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(["git", "add", "fs/x.c"], cwd=str(tmp_path),
                   capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-m", "i"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )

    env = dict(os.environ, PYTHONPATH=f"{REPO / 'src'}:{REPO / 'scripts'}")

    # build DB: symbol my_func @ fs/x.c, index_timestamp in the past
    setup = textwrap.dedent(f"""
        import time
        from storage import SQLiteStore
        s = SQLiteStore({str(db)!r}); s.create_schema()
        s.conn.execute("INSERT OR IGNORE INTO files(path,language) VALUES('fs/x.c','C')")
        fid = s.conn.execute("SELECT id FROM files WHERE path='fs/x.c'").fetchone()[0]
        s.conn.execute(
            "INSERT OR IGNORE INTO symbols(scip_symbol,name,kind,def_file_id,def_start_line,def_end_line) "
            "VALUES('cxx . . $ my_func().','my_func','function',?,0,0)", (fid,))
        s.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('index_timestamp',?)",
                       (str(int(time.time()) - 100),))
        s.conn.commit(); s.close()
    """)
    subprocess.run([sys.executable, "-c", setup], cwd=str(tmp_path),
                   env=env, check=True)

    # modify the file WITHOUT committing → git status ' M' (worktree)
    xf.write_text("int my_func(void){ return 999; }\n")

    query = textwrap.dedent(f"""
        import os, sys
        os.environ['KGRAPH_DB'] = {str(db)!r}
        os.environ['KGRAPH_ROOT'] = {str(tmp_path)!r}
        import importlib.util as ilu
        spec = ilu.spec_from_file_location('kgraph_server', {str(REPO / 'mcp' / 'server.py')!r})
        srv = ilu.module_from_spec(spec); spec.loader.exec_module(srv)
        print(srv.get_function_body('my_func'))
    """)
    out = subprocess.run([sys.executable, "-c", query], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True).stdout

    assert "working_tree" in out, out        # git status refined the reason
    assert "return 999" in out, out          # LIVE modified content via grep fallback
