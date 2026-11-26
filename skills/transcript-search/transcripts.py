"""Claude Code 대화 원본(.jsonl) 검색 도구.

압축(compaction) 전 원본과 지난 세션 기록에서 필요한 부분만 찾아 읽기 위한 도구.
원본은 ~/.claude/projects/<프로젝트>/<세션ID>.jsonl 에 있다.
색인과 목록 캐시는 ~/.claude/transcript-archive/ 에 저장한다 (환경변수 TRANSCRIPT_ARCHIVE_DIR 로 변경 가능).
Python 3.8+ 표준 라이브러리만 사용한다.

검색은 SQLite 전문검색 색인(transcripts.db, trigram)을 쓴다. 검색할 때마다 새로 생기거나
늘어난 기록만 색인에 추가하므로 기록이 몇 년치 쌓여도 검색이 빠르다. 원본이 지워져도
색인에 들어간 대화 텍스트는 남는다.

사용법:
  python transcripts.py list   [--days N] [--project 문자열] [--max N]
  python transcripts.py search 검색어 [--regex] [--case] [--project 문자열] [--session ID앞자리]
                               [--days N] [--roles user,assistant,...] [--context N]
                               [--max N] [--full] [--scan]
  python transcripts.py show   세션ID앞자리 [--from N] [--to N] [--roles ...] [--maxlen N]
  python transcripts.py index  [--rebuild]      (색인 갱신/재작성. search가 자동으로 갱신함)
  python transcripts.py hook-compact            (SessionStart compact 훅 전용, stdin JSON)

roles: user, assistant, summary(압축 요약), tool_use, tool_result, meta
--scan: 색인 대신 원본 파일을 직접 훑는다(느림, 색인 점검용).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS = os.path.join(CLAUDE_HOME, "projects")
DATA = os.environ.get("TRANSCRIPT_ARCHIVE_DIR") or os.path.join(CLAUDE_HOME, "transcript-archive")
os.makedirs(DATA, exist_ok=True)
INDEX = os.path.join(DATA, "transcripts-index.json")
DB = os.path.join(DATA, "transcripts.db")
ALL_ROLES = ["user", "assistant", "summary", "tool_use", "tool_result", "meta"]
MSG_MARKERS = ('"type":"user"', '"type":"assistant"', '"type": "user"', '"type": "assistant"')

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def local_time(ts):
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16]


def all_files():
    out = []
    if not os.path.isdir(PROJECTS):
        return out
    for proj in os.listdir(PROJECTS):
        d = os.path.join(PROJECTS, proj)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".jsonl"):
                out.append(os.path.join(d, f))
    return out


def find_session(prefix, must_exist=True):
    hits = [p for p in all_files() if os.path.basename(p).startswith(prefix)]
    if len(hits) > 1:
        sys.exit("여러 세션이 일치함. 더 길게 입력:\n" + "\n".join(hits))
    if hits:
        return hits[0]
    if must_exist:
        sys.exit(f"세션을 찾을 수 없음: {prefix}")
    return None


def tool_result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_line(line):
    """기록 한 줄 -> 메시지가 아니면 None, 메시지면 [(역할, 시각, 텍스트, 서브에이전트), ...]."""
    if not any(m in line for m in MSG_MARKERS):
        return None
    try:
        o = json.loads(line)
    except Exception:
        return None
    t = o.get("type")
    if t not in ("user", "assistant"):
        return None
    ts = o.get("timestamp")
    side = bool(o.get("isSidechain"))
    content = (o.get("message") or {}).get("content")
    out = []
    if t == "user":
        if o.get("isCompactSummary"):
            role = "summary"
        elif o.get("isMeta"):
            role = "meta"
        else:
            role = "user"
        if isinstance(content, str):
            out.append((role, ts, content, side))
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    out.append((role, ts, b.get("text", ""), side))
                elif b.get("type") == "tool_result":
                    out.append(("tool_result", ts, tool_result_text(b.get("content")), side))
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(("assistant", ts, b.get("text", ""), side))
            elif b.get("type") == "tool_use":
                inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                out.append(("tool_use", ts, f"[{b.get('name')}] {inp}", side))
    return out


def iter_messages(path):
    """(번호, 역할, 시각, 텍스트, 서브에이전트 여부)를 순서대로 돌려준다."""
    n = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            recs = parse_line(line)
            if recs is None:
                continue
            n += 1
            for role, ts, text, side in recs:
                yield n, role, ts, text, side


# ---------------------------------------------------------------- full-text index (sqlite)

def open_db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INT, mtime REAL,"
                " offset INT, msgno INT)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5(text, path UNINDEXED,"
                " n UNINDEXED, role UNINDEXED, ts UNINDEXED, side UNINDEXED, tokenize='trigram')")
    return con


def update_index(con, verbose=False):
    """새로 생기거나 늘어난 기록만 색인에 추가한다. 원본이 지워진 세션의 색인은 남겨둔다."""
    known = {r[0]: r for r in con.execute("SELECT path, size, mtime, offset, msgno FROM files")}
    touched = 0
    added = 0
    for p in all_files():
        try:
            st = os.stat(p)
        except OSError:
            continue
        row = known.get(p)
        if row and row[1] == st.st_size and row[2] == st.st_mtime:
            continue
        if row and st.st_size >= row[3]:
            start, n = row[3], row[4]          # 뒤에 이어 붙은 부분만
        else:
            start, n = 0, 0                     # 새 파일 또는 다시 쓰인 파일
            con.execute("DELETE FROM msgs WHERE path = ?", (p,))
        offset = start
        batch = []
        with open(p, "rb") as fh:
            fh.seek(start)
            for raw in fh:
                if not raw.endswith(b"\n"):     # 기록 중인 마지막 줄은 다음에
                    break
                offset += len(raw)
                recs = parse_line(raw.decode("utf-8", errors="replace"))
                if recs is None:
                    continue
                n += 1
                for role, ts, text, side in recs:
                    if text:
                        batch.append((text, p, n, role, ts or "", int(side)))
        con.executemany("INSERT INTO msgs(text, path, n, role, ts, side) VALUES (?,?,?,?,?,?)", batch)
        con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (p, st.st_size, st.st_mtime, offset, n))
        con.commit()
        touched += 1
        added += len(batch)
        if verbose:
            print(f"  색인: {os.path.basename(p)[:8]}  +{len(batch)}건", file=sys.stderr)
    return touched, added


def cmd_index(a):
    if a.rebuild and os.path.exists(DB):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(DB + suf)
            except OSError:
                pass
    t0 = time.time()
    con = open_db()
    touched, added = update_index(con, verbose=True)
    total = con.execute("SELECT count(*) FROM msgs").fetchone()[0]
    nfiles = con.execute("SELECT count(*) FROM files").fetchone()[0]
    con.close()
    size = sum(os.path.getsize(DB + s) for s in ("", "-wal") if os.path.exists(DB + s))
    print(f"갱신 파일 {touched}개, 추가 {added}건 ({time.time() - t0:.1f}초)\n"
          f"색인 전체: 세션 {nfiles}개, 기록 {total}건, 크기 {size / 1048576:.0f}MB  ({DB})")


# ---------------------------------------------------------------- index (list)

def load_index():
    try:
        with open(INDEX, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_index(idx):
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False)
    os.replace(tmp, INDEX)


def scan_meta(path):
    meta = {"title": None, "first": None, "cwd": None, "start": None, "end": None,
            "compacted": False, "messages": 0}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"ai-title"' in line:
                try:
                    meta["title"] = json.loads(line).get("aiTitle") or meta["title"]
                except Exception:
                    pass
                continue
            if not any(m in line for m in MSG_MARKERS):
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") not in ("user", "assistant"):
                continue
            meta["messages"] += 1
            ts = o.get("timestamp")
            if ts:
                meta["start"] = meta["start"] or ts
                meta["end"] = ts
            meta["cwd"] = meta["cwd"] or o.get("cwd")
            if o.get("isCompactSummary"):
                meta["compacted"] = True
            if (meta["first"] is None and o.get("type") == "user" and not o.get("isMeta")
                    and not o.get("isSidechain")):
                c = (o.get("message") or {}).get("content")
                txt = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                txt = re.sub(r"<[^>]+>.*?</[^>]+>", " ", txt, flags=re.S).strip()
                if txt:
                    meta["first"] = re.sub(r"\s+", " ", txt)[:120]
    return meta


def cmd_list(a):
    idx = load_index()
    changed = False
    rows = []
    cutoff = time.time() - a.days * 86400 if a.days else 0
    for p in all_files():
        st = os.stat(p)
        if st.st_mtime < cutoff:
            continue
        ent = idx.get(p)
        if not ent or ent.get("mtime") != st.st_mtime or ent.get("size") != st.st_size:
            ent = scan_meta(p)
            ent.update(mtime=st.st_mtime, size=st.st_size)
            idx[p] = ent
            changed = True
        if a.project and not (a.project.lower() in p.lower() or a.project.lower() in (ent.get("cwd") or "").lower()):
            continue
        rows.append((p, ent))
    if changed:
        save_index(idx)
    rows.sort(key=lambda r: r[1].get("end") or "", reverse=True)
    for p, e in rows[: a.max]:
        sid = os.path.basename(p)[:-6]
        flag = " [압축됨]" if e.get("compacted") else ""
        st_s, en_s = local_time(e.get("start")), local_time(e.get("end"))
        if en_s[:10] == st_s[:10]:
            en_s = en_s[11:]
        print(f"{st_s} ~ {en_s}  {sid[:8]}  "
              f"{e.get('size', 0) / 1048576:6.1f}MB  메시지 {e.get('messages')}{flag}")
        print(f"    폴더: {e.get('cwd')}")
        print(f"    제목: {e.get('title') or e.get('first') or '-'}")
        print(f"    파일: {p}")
    print(f"\n총 {len(rows)}개 세션 (표시 {min(len(rows), a.max)}개)")


# ---------------------------------------------------------------- search

def compile_pattern(a):
    flags = 0 if a.case else re.IGNORECASE
    return re.compile(a.pattern if a.regex else re.escape(a.pattern), flags)


def print_hit(a, pat, path, n, role, ts, text, side, last_path):
    m = pat.search(text)
    if not m:
        return last_path, False
    if path != last_path:
        print(f"\n=== {os.path.basename(path)[:-6]}  ({path})")
    tag = f"#{n} {role}{' (subagent)' if side else ''} {local_time(ts)}"
    if a.full:
        body = text
    else:
        s = max(0, m.start() - a.context)
        e = min(len(text), m.end() + a.context)
        body = ("…" if s else "") + text[s:e] + ("…" if e < len(text) else "")
    print(f"--- {tag}\n{body.strip()}")
    return path, True


def cmd_search(a):
    if a.scan:
        return search_scan(a)
    pat = compile_pattern(a)
    con = open_db()
    t0 = time.time()
    touched, added = update_index(con)
    if touched:
        print(f"(색인 갱신: 파일 {touched}개, {added}건 추가, {time.time() - t0:.1f}초)", file=sys.stderr)

    where, params = [], []
    if a.regex:
        con.create_function("REGEXP", 2, lambda p, t: 1 if t is not None and re.search(p, t, 0 if a.case else re.I) else 0,
                            deterministic=True)
        where.append("text REGEXP ?")
        params.append(a.pattern)
    elif len(a.pattern) >= 3:
        where.append("msgs MATCH ?")
        params.append('"' + a.pattern.replace('"', '""') + '"')
    else:
        esc = a.pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("text LIKE ? ESCAPE '\\'")
        params.append(f"%{esc}%")
    roles = a.roles.split(",") if a.roles else ALL_ROLES
    where.append(f"role IN ({','.join('?' * len(roles))})")
    params += roles
    if a.session:
        p = find_session(a.session, must_exist=False)
        if p:
            where.append("path = ?")
            params.append(p)
        else:
            where.append("path LIKE ?")
            params.append(f"%{a.session}%")
    if a.project:
        where.append("path LIKE ?")
        params.append(f"%{a.project}%")
    if a.days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%S")
        where.append("ts >= ?")
        params.append(cutoff)
    sql = ("SELECT path, n, role, ts, text, side FROM msgs WHERE " + " AND ".join(where)
           + " ORDER BY ts DESC")
    total = 0
    last = None
    for path, n, role, ts, text, side in con.execute(sql, params):
        last, ok = print_hit(a, pat, path, n, role, ts, text, side, last)
        if ok:
            total += 1
            if total >= a.max:
                print(f"\n(최신순 {a.max}건에서 멈춤. --max 로 늘리거나 --session/--roles/--days 로 좁힐 것)")
                break
    con.close()
    if total < a.max:
        print(f"\n총 {total}건 ({time.time() - t0:.1f}초)" if total else "일치하는 내용 없음")


def search_scan(a):
    """색인 없이 원본을 직접 훑는 검색 (점검용)."""
    pat = compile_pattern(a)
    roles = set(a.roles.split(",")) if a.roles else set(ALL_ROLES)
    if a.session:
        files = [find_session(a.session)]
    else:
        cutoff = time.time() - a.days * 86400 if a.days else 0
        files = [p for p in all_files() if os.stat(p).st_mtime >= cutoff
                 and (not a.project or a.project.lower() in p.lower())]
        files.sort(key=lambda p: os.stat(p).st_mtime, reverse=True)
    total = 0
    last = None
    for p in files:
        for n, role, ts, text, side in iter_messages(p):
            if role not in roles or not text:
                continue
            last, ok = print_hit(a, pat, p, n, role, ts, text, side, last)
            if ok:
                total += 1
                if total >= a.max:
                    print(f"\n(최대 {a.max}건에서 멈춤)")
                    return
    print(f"\n총 {total}건" if total else "일치하는 내용 없음")


# ---------------------------------------------------------------- show

def cmd_show(a):
    roles = set(a.roles.split(",")) if a.roles else {"user", "assistant", "summary", "tool_use", "tool_result"}
    p = find_session(a.session, must_exist=False)

    def emit(n, role, ts, text, side):
        if n < a.frm or (a.to and n > a.to) or role not in roles or not text:
            return
        body = text if len(text) <= a.maxlen else text[: a.maxlen] + f"\n…(이하 {len(text) - a.maxlen}자 생략)"
        print(f"--- #{n} {role}{' (subagent)' if side else ''} {local_time(ts)}\n{body.strip()}")

    if p:
        print(f"=== {p}")
        for rec in iter_messages(p):
            emit(*rec)
        return
    # 원본이 지워진 세션: 색인에 남은 텍스트로 보여준다
    con = open_db()
    rows = con.execute("SELECT path, n, role, ts, text, side FROM msgs WHERE path LIKE ? ORDER BY n",
                       (f"%{a.session}%",)).fetchall()
    con.close()
    if not rows:
        sys.exit(f"세션을 찾을 수 없음: {a.session}")
    print(f"=== {rows[0][0]}  (원본 없음, 색인에 남은 텍스트)")
    for _p, n, role, ts, text, side in rows:
        emit(n, role, ts, text, side)


# ---------------------------------------------------------------- hook

def cmd_hook_compact(_a):
    raw = ""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        data = {}
        for k in ("transcript_path", "session_id"):
            m = re.search(r'"%s"\s*:\s*"([^"]+)"' % k, raw)
            if m:
                data[k] = m.group(1).replace("\\\\", "\\")
    path = data.get("transcript_path") or "(경로 미확인)"
    sid = (data.get("session_id") or "")[:8]
    me = os.path.abspath(__file__)
    py = sys.executable or "python3"
    print(
        "[압축 직후 안내] 방금 대화가 압축되었다. 압축 요약은 원본의 축약본이라 수치·파일 경로·"
        "사용자 결정이 빠지거나 바뀌었을 수 있다.\n"
        f"- 원본: {path}\n"
        "- 요약에 나온 수치·경로·결정을 계산이나 결과물에 쓰기 전에 원본에서 확인한다 "
        "(context-archive:transcript-search 스킬).\n"
        f"- 검색: \"{py}\" -I \"{me}\" search \"검색어\" --session {sid}\n"
        f"- 앞뒤 맥락: \"{py}\" -I \"{me}\" show {sid} --from N --to M"
    )


def main():
    ap = argparse.ArgumentParser(description="Claude Code 대화 원본 검색")
    sub = ap.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", help="세션 목록")
    l.add_argument("--days", type=int, default=0)
    l.add_argument("--project")
    l.add_argument("--max", type=int, default=30)
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("search", help="원본 검색 (색인 사용)")
    s.add_argument("pattern")
    s.add_argument("--regex", action="store_true")
    s.add_argument("--case", action="store_true", help="대소문자 구분")
    s.add_argument("--project")
    s.add_argument("--session")
    s.add_argument("--days", type=int, default=0)
    s.add_argument("--roles")
    s.add_argument("--context", type=int, default=150)
    s.add_argument("--max", type=int, default=30)
    s.add_argument("--full", action="store_true")
    s.add_argument("--scan", action="store_true", help="색인 대신 원본 직접 검색")
    s.set_defaults(fn=cmd_search)

    w = sub.add_parser("show", help="세션 일부 보기")
    w.add_argument("session")
    w.add_argument("--from", dest="frm", type=int, default=1)
    w.add_argument("--to", type=int, default=0)
    w.add_argument("--roles")
    w.add_argument("--maxlen", type=int, default=3000)
    w.set_defaults(fn=cmd_show)

    i = sub.add_parser("index", help="색인 갱신")
    i.add_argument("--rebuild", action="store_true")
    i.set_defaults(fn=cmd_index)

    h = sub.add_parser("hook-compact")
    h.set_defaults(fn=cmd_hook_compact)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
