# -*- coding: utf-8 -*-
"""一次性回填：用新的「活动 / 截止」两道题，重算库里已分类邮件的时间字段。

背景：以前只有一道「哪个日期是截止或活动日期」，活动（讲座、比赛）会被当成「截止」，并且只取开始时间。
现在 triage 表有 event_start / event_end / event_text / event_place，截止仍放 deadline。这个脚本对
「已分类、没被发件人规则忽略、主题+正文里有候选日期」的邮件重新问 Jev（只问日期那两题），只更新时间相关字段
（deadline*、ddl_*、event_*，并把本次 token 累加进 input_tokens/output_tokens），类别、星级、是否要动手都不动。

用法（用装好依赖的虚拟环境跑；先退出 MailCrawler，并自己备份数据库）：
  TYPESAFE_API_KEY=apikey_... .venv/bin/python tools/backfill_times.py [--data-dir DIR] [--dry-run] [--limit N]
  .venv/bin/python tools/backfill_times.py --keychain          # key 改从系统钥匙串取（会弹钥匙串授权）

不写死任何路径：--data-dir 默认是 app 自己的数据目录（macOS：~/Library/Application Support/MailCrawler）。
并发 ≤ 8，429/5xx 自动指数退避重试（jev.jev_call 里已有）。单封失败不影响整批，最后列出失败的 UID。
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def app_running():
    """MailCrawler 进程还在不在（在就会和脚本抢着写库）。"""
    r = subprocess.run(["pgrep", "-f", "MailCrawler.app/Contents/MacOS/MailCrawler"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="数据目录（里面有 mailcrawler.sqlite），默认 app 自己的数据目录")
    ap.add_argument("--keychain", action="store_true", help="API key 从系统钥匙串取，而不是环境变量 TYPESAFE_API_KEY")
    ap.add_argument("--workers", type=int, default=8, help="并发数，最多 8")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 封（试跑用）")
    ap.add_argument("--dry-run", action="store_true", help="只列出要处理多少封，不调用 Jev、不写库")
    ap.add_argument("--force", action="store_true", help="即使检测到 MailCrawler 在运行也继续（不建议）")
    a = ap.parse_args()
    if a.data_dir:
        os.environ["MAILCRAWLER_DATA_DIR"] = a.data_dir

    from mailcrawler import ddl, jev, paths, secrets, store

    if app_running() and not a.force and not a.dry_run:
        sys.exit("MailCrawler 还在运行，会和脚本抢着写库。请先退出它（osascript -e 'quit app \"MailCrawler\"'）再跑。")
    db = paths.db_path()
    if not db.is_file():
        sys.exit("找不到数据库：%s" % db)
    store.init_db()    # 老库补 event_* 列

    key = secrets.jev_key() if a.keychain else os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key and not a.dry_run:
        sys.exit("没有 API key：设环境变量 TYPESAFE_API_KEY，或加 --keychain")

    with store.DB_LOCK, store.db() as c:
        rows = c.execute("SELECT e.uid,e.date,e.sender,e.subject,e.body FROM emails e JOIN triage t ON e.uid=t.uid "
                         "WHERE COALESCE(t.ignored,0)=0 ORDER BY e.date DESC, e.uid DESC").fetchall()
    todo = []
    for r in rows:
        e = dict(r)
        sent = store.parse_dt(e["date"] or "")
        if ddl.extract_candidates((e["subject"] or "") + " ¶ " + (e["body"] or ""), sent):
            todo.append(e)
    if a.limit:
        todo = todo[:a.limit]
    print("数据库：%s" % db)
    print("已分类邮件 %d 封（不含被忽略的），其中主题+正文有候选日期的 %d 封" % (len(rows), len(todo)))
    if a.dry_run or not todo:
        return

    def one(e):
        r = jev.analyze(e, key=key, count=False, dates_only=True)
        return e["uid"], r

    done = fail = 0
    in_tok = out_tok = 0
    kinds = {"只有活动": 0, "只有截止": 0, "活动+截止": 0, "都没有": 0}
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, min(a.workers, 8))) as ex:
        futs = {ex.submit(one, e): e["uid"] for e in todo}
        for f in as_completed(futs):
            uid = futs[f]
            try:
                _, r = f.result()
            except Exception as ex_:     # 单封失败不拖垮整批
                fail += 1
                errors.append((uid, "%s: %s" % (type(ex_).__name__, ex_)))
                continue
            store.update_times(uid, r["deadline"], r["deadline_text"], r["ddl_conf"], r["event_start"], r["event_end"],
                               r["event_text"], r["event_place"], r["event_conf"], usage=r["usage"])
            done += 1
            in_tok += int(r["usage"].get("input_tokens") or 0)
            out_tok += int(r["usage"].get("output_tokens") or 0)
            has_e, has_d = bool(r["event_start"]), bool(r["deadline"])
            kinds["活动+截止" if has_e and has_d else "只有活动" if has_e else "只有截止" if has_d else "都没有"] += 1
            if done % 20 == 0:
                print("  已处理 %d/%d" % (done, len(todo)))
    print("完成：回填 %d 封，失败 %d 封" % (done, fail))
    print("结果分布：%s" % "，".join("%s %d" % kv for kv in kinds.items()))
    print("Jev 用量：输入 %d token，输出 %d token，约 $%.4f（按 $%.3f / 百万输入 token）" % (
        in_tok, out_tok, in_tok * jev.USD_PER_M_TOKENS / 1e6, jev.USD_PER_M_TOKENS))
    for uid, msg in sorted(errors):
        print("  失败 uid=%s：%s" % (uid, msg))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
