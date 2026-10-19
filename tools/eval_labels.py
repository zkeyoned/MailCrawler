# -*- coding: utf-8 -*-
"""用你自己标注过的邮件，评估「重要程度」判断准不准。

不写死任何路径，也不往仓库里写任何东西：数据库路径从命令行传入，详细结果只写到 --out 指定的文件
（请放在仓库外面——里面有邮件主题）。对标注过的邮件跑一遍当前方案（和 app 里完全同一套问题），然后报告：

  1. Jev 重要度和你给的星级一致不一致（完全一致、差 1 星以内）
  2. 你标「垃圾」的邮件里，Jev 给 ≤2 星的比例
  3. 你标「有用 / 错过可惜」的邮件里，Jev 给 ≥4 星的比例
  4. 「系统自动通知」类别的识别率（用主题正则当参照，可用 --auto-pattern 换成你自己的）

用法（用装好依赖的虚拟环境来跑）：
  TYPESAFE_API_KEY=apikey_... .venv/bin/python tools/eval_labels.py LABELS_DB MAIL_DB \\
      [--prefs-file prefs.txt] [--ignore-file ignore.txt] [--out results.json] [--from-results results.json]

参数说明：
  LABELS_DB   你的标注库（SQLite）。需要的表：labels(uid, stars, feeling)、feeling_tags(id, name)、label_tags(uid, tag_id)；
              「有标注」指星级、感觉文字、标签、类别任意一项不为空的邮件。
  MAIL_DB     邮件库（SQLite）。需要表 emails(uid, date, sender, subject, body)；MailCrawler 自己的数据库就符合。
  --prefs-file  「我的偏好」文本文件，不给 = 无偏好（只用通用标准）
  --ignore-file 「总是忽略的发件人」文本文件，每行一个关键字。不给就不评估这一项。
                注意：评估时每封邮件照样会送去 Jev（这样一次运行能同时看到有、无忽略名单的数字），
                命中名单的邮件在「应用忽略名单」那一栏里按 1 星算。
  --from-results  不再调用 Jev，直接读上一次 --out 写出的结果重新出报告（改了名单/正则时免费重算）

费用：每封约 1–2 千 token，几百封不到几美分。API key 从环境变量 TYPESAFE_API_KEY 读（不进命令行历史）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_AUTO = (r"^(you have submitted|发件人已撤回邮件|撤回邮件)|one[- ]?time code|verification code|验证码|"
                r"登录提醒|login (alert|reminder)|system maintenance|系统维护")
USD_PER_M_TOKENS = 0.042


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).resolve(), uri=True)


def load_labels(path):
    """返回 [{uid, stars, tags:set, feeling}]，只含有标注内容的邮件。"""
    c = ro(path)
    tags = {}
    for uid, name in c.execute("SELECT lt.uid, ft.name FROM label_tags lt JOIN feeling_tags ft ON ft.id=lt.tag_id"):
        tags.setdefault(uid, set()).add(name)
    out = []
    for uid, stars, feeling in c.execute("SELECT uid, stars, COALESCE(feeling,'') FROM labels"):
        if stars is None and not feeling.strip() and uid not in tags:
            continue
        out.append({"uid": uid, "stars": stars, "tags": sorted(tags.get(uid, ())), "feeling": bool(feeling.strip())})
    return out


def load_mail(path, uids):
    c = ro(path)
    out = {}
    for uid in uids:
        r = c.execute("SELECT uid,date,sender,subject,body FROM emails WHERE uid=?", (uid,)).fetchone()
        if r:
            out[uid] = {"uid": r[0], "date": r[1], "sender": r[2], "subject": r[3], "body": r[4]}
    return out


def run_jev(items, mails, prefs, workers):
    from mailcrawler import jev
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        sys.exit("请先设置环境变量 TYPESAFE_API_KEY（TypeSafe 的 API key）")

    def one(it):
        e = mails.get(it["uid"])
        if not e:
            return dict(it, error="邮件库里没有这封")
        try:
            r = jev.analyze(e, prefs=prefs, ignore_text="", key=key, count=False)
        except Exception as ex:   # 单封失败不拖垮整批
            return dict(it, error="%s: %s" % (type(ex).__name__, ex))
        return dict(it, subject=e["subject"] or "", sender=e["sender"] or "", cat=r["cat"], cat_conf=r["cat_conf"],
                    importance=r["importance"], imp_conf=r["imp_conf"],
                    tokens=int(r["usage"].get("input_tokens") or 0) + int(r["usage"].get("output_tokens") or 0),
                    in_tokens=int(r["usage"].get("input_tokens") or 0))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as ex:
        return list(ex.map(one, items))


def star(x):
    return max(1, min(5, int(x + 0.5)))


def pct(a, b):
    return "%d/%d = %.0f%%" % (a, b, 100.0 * a / b) if b else "0/0 = n/a"


def report(rows, ignore_text, auto_pat, junk_tags, useful_tags, label):
    from mailcrawler import jev
    ok = [r for r in rows if "importance" in r]
    err = len(rows) - len(ok)
    print("=" * 60)
    print(label)
    print("=" * 60)
    print("评估邮件 %d 封（失败 %d 封）" % (len(ok), err))
    hit = 0
    for r in ok:
        if ignore_text is not None and jev.ignored_by(r.get("sender"), ignore_text):
            r["_imp"] = 1.0
            hit += 1
        else:
            r["_imp"] = r["importance"]
    if ignore_text is not None:
        print("忽略名单命中 %d 封（按 1 星算，实际使用时这些不会送去 Jev）" % hit)
    # 1. 星级一致率
    st = [r for r in ok if r.get("stars")]
    exact = sum(1 for r in st if star(r["_imp"]) == r["stars"])
    near = sum(1 for r in st if abs(star(r["_imp"]) - r["stars"]) <= 1)
    mae = sum(abs(r["_imp"] - r["stars"]) for r in st) / len(st) if st else 0
    print("\n[1] 和你给的星级比（有星级的 %d 封）" % len(st))
    print("    完全一致：%s" % pct(exact, len(st)))
    print("    差 1 星以内：%s" % pct(near, len(st)))
    print("    平均绝对误差：%.2f 星" % mae)
    # 2. 垃圾
    junk = [r for r in ok if set(r["tags"]) & set(junk_tags)]
    print("\n[2] 你标「%s」的 %d 封里，Jev ≤2 星：%s" % ("/".join(junk_tags), len(junk), pct(sum(1 for r in junk if star(r["_imp"]) <= 2), len(junk))))
    # 3. 有用
    use = [r for r in ok if set(r["tags"]) & set(useful_tags)]
    print("[3] 你标「%s」的 %d 封里，Jev ≥4 星：%s（≥3 星：%s）" % (
        "/".join(useful_tags), len(use), pct(sum(1 for r in use if star(r["_imp"]) >= 4), len(use)),
        pct(sum(1 for r in use if star(r["_imp"]) >= 3), len(use))))
    # 4. 系统自动通知
    pat = re.compile(auto_pat, re.I)
    autos = [r for r in ok if pat.search(r.get("subject") or "")]
    got = [r for r in autos if r["cat"] == "automated"]
    jev_auto = [r for r in ok if r["cat"] == "automated"]
    outside = [r for r in jev_auto if r not in autos]
    print("[4] 系统自动通知：主题符合参照正则的 %d 封里，Jev 判成「系统自动通知」：%s" % (len(autos), pct(len(got), len(autos))))
    print("    Jev 一共判了 %d 封为系统自动通知，其中 %d 封在参照正则之外" % (len(jev_auto), len(outside)))
    # 费用
    toks = sum(r.get("in_tokens", 0) for r in ok)
    print("\n本次输入 token %d，约 $%.4f" % (toks, toks * USD_PER_M_TOKENS / 1e6))
    # 典型错例
    bad = sorted((r for r in st if abs(star(r["_imp"]) - r["stars"]) >= 2), key=lambda r: -abs(r["_imp"] - r["stars"]))
    print("\n差 ≥2 星的典型例子（%d 封，最多列 10 封；只列主题）：" % len(bad))
    for r in bad[:10]:
        print("  你 %d 星 / Jev %.1f 星 | %s" % (r["stars"], r["_imp"], (r.get("subject") or "")[:70]))
    wrong_j = [r for r in junk if star(r["_imp"]) >= 3]
    print("你标垃圾但 Jev ≥3 星（%d 封，最多列 10 封）：" % len(wrong_j))
    for r in wrong_j[:10]:
        print("  Jev %.1f 星 | %s" % (r["_imp"], (r.get("subject") or "")[:70]))
    miss = [r for r in use if star(r["_imp"]) <= 2]
    print("你标有用但 Jev ≤2 星（%d 封，最多列 10 封）：" % len(miss))
    for r in miss[:10]:
        print("  Jev %.1f 星 | %s" % (r["_imp"], (r.get("subject") or "")[:70]))
    dist = {k: 0 for k in range(1, 6)}
    for r in ok:
        dist[star(r["_imp"])] += 1
    print("Jev 星级分布：" + "  ".join("%d★:%d" % (k, v) for k, v in dist.items()))


def main():
    ap = argparse.ArgumentParser(description="评估重要程度判断（详见文件开头说明）")
    ap.add_argument("labels_db")
    ap.add_argument("mail_db")
    ap.add_argument("--prefs-file")
    ap.add_argument("--ignore-file")
    ap.add_argument("--out", help="把每封的详细结果写到这个 JSON 文件（放在仓库外面）")
    ap.add_argument("--from-results", help="读上次 --out 的结果重新出报告，不调用 Jev")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="只评估前 N 封（试跑用）")
    ap.add_argument("--auto-pattern", default=DEFAULT_AUTO, help="参照用的「系统自动通知」主题正则")
    ap.add_argument("--junk-tags", default="垃圾", help="哪些标签算「垃圾」，逗号分隔")
    ap.add_argument("--useful-tags", default="有用,错过可惜", help="哪些标签算「有用」，逗号分隔")
    a = ap.parse_args()

    prefs = Path(a.prefs_file).read_text("utf-8").strip() if a.prefs_file else ""
    ignore = Path(a.ignore_file).read_text("utf-8") if a.ignore_file else None
    if a.from_results:
        rows = json.loads(Path(a.from_results).read_text("utf-8"))
    else:
        labels = load_labels(a.labels_db)
        if a.limit:
            labels = labels[:a.limit]
        mails = load_mail(a.mail_db, [x["uid"] for x in labels])
        print("标注 %d 封，邮件库里找到 %d 封；偏好文本 %d 字" % (len(labels), len(mails), len(prefs)), file=sys.stderr)
        rows = run_jev(labels, mails, prefs, a.workers)
        if a.out:
            Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    junk, useful = split(a.junk_tags), split(a.useful_tags)
    title = "偏好：%s" % ("有（%d 字）" % len(prefs) if prefs else "无") if not a.from_results else "（读自已有结果）"
    report(rows, None, a.auto_pattern, junk, useful, "不用忽略名单 · " + title)
    if ignore is not None:
        report(rows, ignore, a.auto_pattern, junk, useful, "应用忽略名单 · " + title)


if __name__ == "__main__":
    main()
