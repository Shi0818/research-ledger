#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""科研账本 / research-ledger —— append-only 实验登记与指标派生。

stdlib only，Python 3.9+。

六条纪律写死在代码里，不靠自觉：

  D1  只 append。本文件没有 update / delete 子命令，故意的。
  D2  n_total 只来自 manifest（查询集的性质），不接受脚本或人手填报。
      脚本报什么就是什么，分母会随 bug 自动缩小 —— 这是最隐蔽的一类污染。
  D3  n_completed / n_success / fail_reason 一律由本脚本从原始输出计算。
      不存在 --n-success 这类参数，防止 agent 把编造值写进真值源。
  D4  对外数字一律经 Measurement 输出，返回带分母、口径、完整性标记的字符串，
      不返回裸 float —— 裸 float 一定会被手抄进 docx。
  D5  聚合口径写进函数名：rate_weighted / rate_mean_of_parts /
      rate_over_completed / completion_rate。没有叫 rate() 的模糊入口。
  D6  过滤参数不给默认值，缺一个就报错。默认值会变成没人知道的隐性口径。

用法见 SKILL.md。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LEDGER = "ledger.jsonl"
MANIFEST = "manifest.json"
CONFLICTS = "conflicts.jsonl"
CHANGELOG = "CHANGELOG.md"

REQUIRED = [
    "run_id", "split", "code_version", "script_sha", "prompt_cond",
    "judge_version", "pooling", "model", "top_k", "n_total", "n_completed",
    "n_success", "fail_reason", "queries_sha", "ran_at", "host",
]

METRICS = ("rate_weighted", "rate_mean_of_parts", "rate_over_completed", "completion_rate")

KEY_FOR_DUP = ("split", "code_version", "prompt_cond", "judge_version", "pooling", "model")

SUCCESS_KEYS = ("success", "passed", "is_success", "attack_success", "hit", "ok")
REASON_KEYS = ("fail_reason", "reason", "error_type", "fail_type", "failure")


# ---------------------------------------------------------------- 基础设施

def _out(msg=""):
    print(msg)


def _die(msg, code=2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def _root(p):
    r = Path(p).expanduser().resolve()
    if not r.is_dir():
        _die("账本目录不存在：%s（先跑 init）" % r)
    return r


def _read_jsonl(path):
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                _die("%s 第 %d 行不是合法 JSON：%s" % (path.name, i, e))
    return rows


def _append_jsonl(path, rec):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=False) + "\n")


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()[:16]


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _git_sha(hint_dir=None):
    cmd = ["git", "rev-parse", "--short", "HEAD"]
    try:
        d = str(hint_dir) if hint_dir else None
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=d)
        if r.returncode == 0:
            return r.stdout.strip(), "git"
    except (FileNotFoundError, OSError):
        pass
    return "unknown", "unknown"


def _read_raw(path):
    p = Path(path)
    if not p.exists():
        _die("原始输出不存在：%s" % p)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".jsonl", ".ndjson"):
        recs = [json.loads(x) for x in text.splitlines() if x.strip()]
    else:
        data = json.loads(text)
        if isinstance(data, dict):
            for k in ("results", "records", "runs", "data", "items"):
                if isinstance(data.get(k), list):
                    recs = data[k]
                    break
            else:
                _die("原始输出是 dict，但找不到列表字段（results/records/runs/data/items）。"
                     "请写一个 adapter。")
        elif isinstance(data, list):
            recs = data
        else:
            _die("原始输出既不是 list 也不是含列表的 dict。请写一个 adapter。")
    if not recs:
        _die("原始输出里 0 条记录，拒绝登记空批。")
    return recs


def _load_adapter(path):
    p = Path(path)
    if not p.exists():
        _die("adapter 不存在：%s" % p)
    spec = importlib.util.spec_from_file_location("ledger_adapter", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "load"):
        _die("adapter 必须定义 load(path) -> list[dict]")
    return mod.load


def _classify(recs, raw_path=None, adapter_path=None):
    """把原始记录归成 (n_completed, n_success, fail_reason{}, dup_ids)。"""
    loader = _load_adapter(adapter_path) if adapter_path else None
    if loader:
        norm = loader(Path(raw_path).resolve() if raw_path else None)
    else:
        norm = []
        for r in recs:
            sk = next((k for k in SUCCESS_KEYS if k in r), None)
            if sk is None or not isinstance(r[sk], bool):
                _die("无法识别成功标记。记录里的键：%s。请写 adapter："
                     "load(path) -> [{'success': bool, 'fail_reason': str|None, 'query_id': str}]"
                     % ",".join(sorted(r.keys())[:12]))
            rk = next((k for k in REASON_KEYS if k in r), None)
            norm.append({
                "success": r[sk],
                "fail_reason": (r.get(rk) if rk else None),
                "query_id": str(r.get("query_id") or r.get("qid") or r.get("id") or ""),
            })

    n_completed = len(norm)
    n_success = sum(1 for x in norm if x.get("success") is True)
    dist = {}
    for x in norm:
        if x.get("success") is True:
            continue
        key = (x.get("fail_reason") or "unclassified").strip() or "unclassified"
        dist[key] = dist.get(key, 0) + 1

    ids = [x.get("query_id") for x in norm if x.get("query_id")]
    dup_ids = len(ids) - len(set(ids)) if ids else 0
    return n_completed, n_success, dist, dup_ids


# ---------------------------------------------------------------- Measurement

class Measurement:
    """派生结果。故意不是 float —— 裸 float 会被手抄。"""

    def __init__(self, metric, value, k, d, filters, complete, missing, dup=None):
        self.metric = metric
        self.value = value
        self.k = k
        self.d = d
        self.filters = filters
        self.complete = complete
        self.missing = missing
        self.dup = dup or []

    def label(self):
        f = self.filters
        return "cond=%s judge=%s pooling=%s code=%s metric=%s" % (
            f.get("prompt_cond", "*"), f.get("judge_version", "*"),
            f.get("pooling", "*"), f.get("code_version", "*"), self.metric)

    def render(self, allow_partial=False):
        """重复计数一律拒绝（分母本身就是错的，不是缺一点）；
        缺 split 时默认拒绝，--allow-partial 可出数但打 PARTIAL 标。"""
        if self.dup:
            return "REFUSED  (重复计数 %s)  %s" % (",".join(self.dup), self.label())
        if not self.missing:
            return "%.2f%%  (%d/%d)  %s  complete" % (
                self.value * 100.0, self.k, self.d, self.label())
        if allow_partial:
            return "%.2f%%  (%d/%d)  PARTIAL(缺 %s)  %s" % (
                self.value * 100.0, self.k, self.d, ",".join(self.missing), self.label())
        return "REFUSED  (缺 %s)  %s" % (",".join(self.missing), self.label())

    def __str__(self):
        return self.render(False)

    def __float__(self):
        return self.value


GROUP_KEYS = ("prompt_cond", "judge_version", "pooling", "code_version")


def _by_group(rows):
    g = {}
    for r in rows:
        key = tuple(r.get(k) for k in GROUP_KEYS)
        g.setdefault(key, []).append(r)
    return g


def _dedupe(group, resolve):
    """同一 split 出现多行 → 分母会被重复计数。默认拒绝，除非显式 --resolve。"""
    per = {}
    for r in group:
        per.setdefault(r["split"], []).append(r)
    dups = sorted(s for s, rs in per.items() if len(rs) > 1)
    if dups and not resolve:
        return group, dups
    if not dups:
        return group, []
    picked = []
    for s, rs in per.items():
        if len(rs) == 1:
            picked.append(rs[0])
        else:
            rs = sorted(rs, key=lambda x: str(x.get("ran_at", "")))
            picked.append(rs[-1] if resolve == "latest" else rs[0])
    return picked, []


def _metric_value(rows, metric):
    ks = [int(r["n_success"]) for r in rows]
    ds = [int(r["n_total"]) for r in rows]
    cs = [int(r["n_completed"]) for r in rows]
    if metric == "rate_weighted":
        d = sum(ds)
        return (sum(ks) / d if d else 0.0), sum(ks), d
    if metric == "rate_mean_of_parts":
        vals = [k / t for k, t in zip(ks, ds) if t]
        return (sum(vals) / len(vals) if vals else 0.0), sum(ks), sum(ds)
    if metric == "rate_over_completed":
        c = sum(cs)
        return (sum(ks) / c if c else 0.0), sum(ks), c
    if metric == "completion_rate":
        d = sum(ds)
        return (sum(cs) / d if d else 0.0), sum(cs), d
    _die("未知 metric：%s" % metric)


def _measurements(rows, manifest, metric, include_backfilled=False,
                  only=None, resolve=None):
    if not include_backfilled:
        rows = [r for r in rows if not r.get("backfilled")]
    all_splits = set(manifest["splits"].keys())
    only = only or {}
    res = []
    for key, group in sorted(_by_group(rows).items(), key=lambda kv: str(kv[0])):
        row_key = dict(zip(GROUP_KEYS, key))
        if any(only.get(k) not in (None, row_key[k]) for k in GROUP_KEYS):
            continue
        group, dups = _dedupe(group, resolve)
        present = set(r["split"] for r in group)
        missing = sorted(all_splits - present)
        val, k, d = _metric_value(group, metric)
        res.append(Measurement(metric, val, k, d, row_key,
                               not missing and not dups, missing, dups))
    return res


# ---------------------------------------------------------------- 子命令

def cmd_init(args):
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    mpath = root / MANIFEST
    if mpath.exists() and not args.force:
        _die("%s 已存在。要重建加 --force（会覆盖分母锚定，慎用）。" % mpath)

    splits = {}
    dataset_sha = None
    if args.queries:
        qp = Path(args.queries).expanduser().resolve()
        raw = qp.read_bytes()
        dataset_sha = _sha256_bytes(raw)
        lines = [x for x in raw.decode("utf-8").splitlines() if x.strip()]
        if args.split_field:
            buckets = {}
            for ln in lines:
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    _die("--split-field 需要每行是 JSON")
                s = str(obj.get(args.split_field))
                buckets.setdefault(s, []).append(ln)
            for s, ls in buckets.items():
                splits[s] = {"n_total": len(ls),
                             "sha": _sha256_bytes("\n".join(ls).encode("utf-8"))}
        else:
            splits["all"] = {"n_total": len(lines), "sha": dataset_sha}
        source = str(qp)
    elif args.splits:
        for pair in args.splits.split(","):
            k, v = pair.split("=")
            splits[k.strip()] = {"n_total": int(v), "sha": "unknown"}
        source = "manual --splits"
    else:
        _die("init 需要 --queries <文件> 或 --splits part-01=200,part-02=200,...")

    manifest = {
        "created_at": _now(),
        "dataset_sha": dataset_sha or "unknown",
        "total": sum(v["n_total"] for v in splits.values()),
        "splits": splits,
        "source": source,
        "verified_by": args.verified_by or "未声明（分母可信度未标注）",
    }
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / LEDGER).touch()
    (root / CONFLICTS).touch()
    if not (root / CHANGELOG).exists():
        (root / CHANGELOG).write_text(
            "# 决策记录\n\n每改一次 judge_version / prompt_cond / 判据阈值 / 数据切片，"
            "在这里写一行：日期、改了什么、为什么。\n\n", encoding="utf-8")
    _out("manifest 写入 %s" % mpath)
    _out("  total = %d，splits = %s" % (manifest["total"], ", ".join(
        "%s:%d" % (k, v["n_total"]) for k, v in splits.items())))
    _out("  dataset_sha = %s，source = %s" % (manifest["dataset_sha"], source))
    _out("  分母可信度：%s" % manifest["verified_by"])


def cmd_ingest(args):
    root = _root(args.root)
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if args.split not in manifest["splits"]:
        _die("split=%s 不在 manifest 里（现有：%s）" % (
            args.split, ",".join(manifest["splits"])))
    n_total = manifest["splits"][args.split]["n_total"]
    queries_sha = manifest["splits"][args.split]["sha"]

    recs = _read_raw(args.raw)
    n_completed, n_success, dist, dup_ids = _classify(
        recs, raw_path=args.raw, adapter_path=args.adapter)

    # ---- 入库前自检（E2/E3 的入口版，防止解析错位）
    if n_completed > n_total:
        _die("n_completed=%d 超过 manifest 的 n_total=%d。"
             "要么 split 标错，要么原始输出混了别的批次。拒绝入库。" % (n_completed, n_total))
    if sum(dist.values()) != n_completed - n_success:
        _die("失败分布对不上：sum(fail_reason)=%d，应为 %d。解析器可能错位。"
             % (sum(dist.values()), n_completed - n_success))
    if dup_ids:
        _die("原始输出里有 %d 条 query_id 重复，分母会被重复计数。先查重。" % dup_ids)
    if n_success > n_completed:
        _die("n_success > n_completed，解析器有 bug。")

    script_sha, sha_src = (args.script_sha, "manual") if args.script_sha else _git_sha(
        Path(args.adapter).parent if args.adapter else Path.cwd())

    rec = {
        "run_id": args.run_id,
        "split": args.split,
        "code_version": args.code_version,
        "script_sha": script_sha,
        "prompt_cond": args.prompt_cond,
        "judge_version": args.judge_version,
        "pooling": args.pooling,
        "model": args.model,
        "top_k": int(args.top_k),
        "n_total": n_total,
        "n_completed": n_completed,
        "n_success": n_success,
        "fail_reason": dist,
        "queries_sha": queries_sha,
        "ran_at": args.ran_at or _now(),
        "host": args.host or socket.gethostname(),
        "patch_of": args.patch_of,
        "rerun_of": args.rerun_of,
        "backfilled": bool(args.backfilled),
        "evidence": str(Path(args.raw).resolve()),
        "ext": {"script_sha_source": sha_src,
                "raw_records": len(recs),
                "ingested_at": _now()},
    }

    existing = _read_jsonl(root / LEDGER)
    if any(r.get("run_id") == rec["run_id"] for r in existing):
        _die("run_id=%s 已存在。账本只许 append，不许覆盖。换一个 run_id 或加 rerun_of。"
             % rec["run_id"])

    if args.backfilled:
        _out("注意：这是一条回填行（backfilled=true）。derive 默认会排除它。")
        _out("回填只许照抄原始输出；数字与旧报告冲突时，跑 note 记一条 conflict，不要协调。")

    _append_jsonl(root / LEDGER, rec)
    _out("已登记 %s  split=%s  n_success=%d/%d  n_completed=%d" % (
        rec["run_id"], rec["split"], n_success, n_total, n_completed))
    if dist:
        _out("  fail_reason: " + ", ".join("%s=%d" % kv for kv in sorted(dist.items())))
    unclass = dist.get("unclassified", 0)
    if unclass and unclass / max(1, n_completed - n_success) > 0.10:
        _out("  WARN: unclassified 占失败的 %.0f%%，判定器可能有 bug，或失败没被分类。"
             % (100.0 * unclass / (n_completed - n_success)))
    _out("  下一步：跑 audit 复核八条不变式。")


def cmd_audit(args):
    root = _root(args.root)
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    rows = _read_jsonl(root / LEDGER)
    fails, warns = [], []

    if not rows:
        _out("账本为空。先 ingest。")
        return 0

    # E7 字段完备
    for r in rows:
        miss = [f for f in REQUIRED if f not in r or r[f] is None]
        if miss:
            fails.append("E7 %s 缺字段 %s" % (r.get("run_id", "?"), ",".join(miss)))

    # E4 run_id 唯一
    seen = {}
    for r in rows:
        rid = r.get("run_id")
        if rid in seen:
            fails.append("E4 run_id 重复：%s" % rid)
        seen[rid] = 1

    # E1 分母锚定
    for r in rows:
        s = r.get("split")
        if s not in manifest["splits"]:
            fails.append("E1 %s 的 split=%s 不在 manifest 里" % (r.get("run_id"), s))
            continue
        want = manifest["splits"][s]["n_total"]
        if int(r["n_total"]) != want:
            fails.append("E1 %s 的 n_total=%s，manifest 是 %d（分母被改过）"
                         % (r.get("run_id"), r["n_total"], want))
    present = set(r["split"] for r in rows if r.get("split") in manifest["splits"])
    missing = sorted(set(manifest["splits"]) - present)
    if missing:
        warns.append("E1 缺 split：%s（合计 %d 条分母未覆盖）" % (
            ",".join(missing),
            sum(manifest["splits"][m]["n_total"] for m in missing)))

    # E2 关系
    for r in rows:
        k, c, t = int(r["n_success"]), int(r["n_completed"]), int(r["n_total"])
        if not (0 <= k <= c <= t):
            fails.append("E2 %s 关系非法：n_success=%d n_completed=%d n_total=%d"
                         % (r.get("run_id"), k, c, t))

    # E3 分布守恒
    for r in rows:
        dist = r.get("fail_reason") or {}
        if not isinstance(dist, dict):
            fails.append("E3 %s 的 fail_reason 不是字典" % r.get("run_id"))
            continue
        want = int(r["n_completed"]) - int(r["n_success"])
        got = sum(int(v) for v in dist.values())
        if got != want:
            fails.append("E3 %s 分布不守恒：sum=%d，应为 %d" % (r.get("run_id"), got, want))
        unclass = int(dist.get("unclassified", 0))
        if want and unclass / want > 0.10:
            warns.append("E3 %s unclassified 占 %.0f%%，查判定器" % (
                r.get("run_id"), 100.0 * unclass / want))

    # E5 同条件重复缺标注
    groups = {}
    for r in rows:
        key = tuple(r.get(k) for k in KEY_FOR_DUP)
        groups.setdefault(key, []).append(r)
    for key, grp in groups.items():
        if len(grp) > 1:
            unmarked = [r for r in grp if not r.get("rerun_of") and not r.get("patch_of")]
            if len(unmarked) > 1:
                fails.append("E5 同条件多行且未标注 rerun_of/patch_of：%s（%d 行）"
                             % ("/".join(str(x) for x in key), len(unmarked)))

    # E6 指纹锚定
    for s, meta in manifest["splits"].items():
        shas = set(r["queries_sha"] for r in rows if r.get("split") == s)
        if len(shas) > 1:
            fails.append("E6 split=%s 出现多个 queries_sha：%s" % (s, ",".join(map(str, shas))))
        if shas and meta["sha"] != "unknown" and shas.pop() != meta["sha"]:
            fails.append("E6 split=%s 的 queries_sha 与 manifest 不一致（数据集变过）" % s)

    # E8 时间
    for r in rows:
        ts = r.get("ran_at", "")
        if "T" not in str(ts) or not (str(ts).endswith("Z") or "+" in str(ts)[10:]):
            warns.append("E8 %s 的 ran_at 没有时区" % r.get("run_id"))
        if r.get("script_sha") == "unknown":
            warns.append("E8 %s 的 script_sha 未知（无法定位跑它的代码版本）" % r.get("run_id"))
    byid = {r.get("run_id"): r for r in rows}
    for r in rows:
        for rel in ("patch_of", "rerun_of"):
            p = r.get(rel)
            if p and p not in byid:
                fails.append("E5 %s 的 %s=%s 指向不存在的行" % (r.get("run_id"), rel, p))

    _out("账本：%s" % (root / LEDGER))
    _out("行数 = %d，覆盖 split = %d/%d，回填行 = %d" % (
        len(rows), len(present), len(manifest["splits"]),
        sum(1 for r in rows if r.get("backfilled"))))
    _out("")
    for w in warns:
        _out("WARN  " + w)
    for f in fails:
        _out("FAIL  " + f)
    if not fails:
        _out("八条不变式：全部通过%s。" % ("（有警告）" if warns else ""))
        return 0
    _out("")
    _out("FAIL 共 %d 条。账本不完整时，任何从这里派生的数字都要带 INCOMPLETE 标记。" % len(fails))
    return 1


def cmd_show(args):
    root = _root(args.root)
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    rows = _read_jsonl(root / LEDGER)
    only = {"prompt_cond": args.cond, "judge_version": args.judge,
            "pooling": args.pooling, "code_version": args.code_version}
    ms = _measurements(rows, manifest, args.metric,
                       include_backfilled=args.include_backfilled, only=only,
                       resolve=args.resolve)
    if not ms:
        _out("没有匹配的组。检查 --cond/--judge/--pooling/--code-version 或先 ingest。")
        return 3
    bad = hard = 0
    for m in ms:
        print(m.render(args.allow_partial))
        if m.dup:
            hard += 1
        elif not m.complete:
            bad += 1
    if hard:
        _out("")
        _out("%d 个组重复计数，分母本身就是错的，不接受 --allow-partial。"
             "用 --code-version 或 --resolve latest/first 消歧。" % hard)
        return 4
    if bad and not args.allow_partial:
        _out("")
        _out("%d 个组不可出数。缺 split 就补齐；重复计数用 --code-version 或 "
             "--resolve latest 消歧。" % bad)
        return 3
    return 0


def cmd_verify(args):
    """从报告文本里抽数字，回到账本里找。找不到 = 这个数字没有出处。"""
    root = _root(args.root)
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    rows = _read_jsonl(root / LEDGER)
    text = Path(args.file).read_text(encoding="utf-8")

    cands = []
    incomplete = 0
    for metric in METRICS:
        for m in _measurements(rows, manifest, metric,
                               include_backfilled=args.include_backfilled,
                               resolve="latest"):
            if m.complete and m.d:
                cands.append((m.value * 100.0, str(m)))
            elif not m.complete:
                incomplete += 1

    if not cands:
        _out("账本覆盖不全（%d 个分组不可出数），无法核对。" % incomplete)
        _out("先补齐缺失的 split 或跑 audit，再拿这份报告来比 —— "
             "现在报 MISS 只会冤枉你抄错了数。")
        return 2
    if incomplete:
        _out("提示：%d 个分组因覆盖不全未参与核对，核对范围小于账本全量。" % incomplete)

    ratios = []
    vals = [c[0] for c in cands]
    for a in vals:
        for b in vals:
            if b and a != b:
                ratios.append((a / b, "%.2f×  = %.2f%% / %.2f%%" % (a / b, a, b)))

    hits, miss = 0, 0
    _out("核对 %s" % args.file)
    for mt in re.finditer(r"(\d+(?:\.\d+)?)\s*%", text):
        v = float(mt.group(1))
        best = min(cands, key=lambda c: abs(c[0] - v)) if cands else None
        if best and abs(best[0] - v) <= args.tol:
            _out("  OK    %-8s ← %s" % (mt.group(0), best[1]))
            hits += 1
        else:
            near = "；最近的是 %.2f%%（差 %.2f）" % (best[0], abs(best[0] - v)) if best else ""
            _out("  MISS  %-8s 账本里没有%s" % (mt.group(0), near))
            miss += 1
    for mt in re.finditer(r"(\d+(?:\.\d+)?)\s*[×xX倍]", text):
        v = float(mt.group(1))
        best = min(ratios, key=lambda c: abs(c[0] - v)) if ratios else None
        if best and abs(best[0] - v) <= args.tol:
            _out("  OK    %-8s ← %s" % (mt.group(0), best[1]))
            hits += 1
        else:
            _out("  MISS  %-8s 无法从账本复现这个倍数" % mt.group(0))
            miss += 1
    _out("")
    _out("命中 %d，未命中 %d。" % (hits, miss))
    if miss:
        _out("未命中的数字要么抄错，要么来自账本之外的算法（心算、旧版本、别人的数）。")
        _out("在写进报告/论文之前，逐个确认它的出处。")
        return 1
    return 0


def cmd_note(args):
    root = _root(args.root)
    rec = {"noted_at": _now(), "kind": args.kind, "about": args.about, "detail": args.detail}
    _append_jsonl(root / CONFLICTS, rec)
    _out("已记入 conflicts.jsonl：%s" % args.about)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="ledger", description="科研账本 —— append-only 实验登记")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="建立分母锚定（manifest）")
    i.add_argument("--root", required=True)
    i.add_argument("--queries")
    i.add_argument("--split-field")
    i.add_argument("--splits")
    i.add_argument("--verified-by", help="你怎么确认这个总数的（如 wc -l = 1150）")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    g = sub.add_parser("ingest", help="登记一次运行")
    g.add_argument("--root", required=True)
    g.add_argument("--run-id", required=True)
    g.add_argument("--split", required=True)
    g.add_argument("--raw", required=True)
    g.add_argument("--adapter", help="自定义解析：定义 load(path) -> list[dict]")
    g.add_argument("--code-version", required=True)
    g.add_argument("--prompt-cond", required=True)
    g.add_argument("--judge-version", required=True)
    g.add_argument("--pooling", required=True)
    g.add_argument("--model", required=True)
    g.add_argument("--top-k", required=True)
    g.add_argument("--script-sha", help="不给则从 git 取")
    g.add_argument("--ran-at")
    g.add_argument("--host")
    g.add_argument("--rerun-of")
    g.add_argument("--patch-of")
    g.add_argument("--backfilled", action="store_true")
    g.set_defaults(func=cmd_ingest)

    a = sub.add_parser("audit", help="八条不变式复核")
    a.add_argument("--root", required=True)
    a.set_defaults(func=cmd_audit)

    s = sub.add_parser("show", help="派生指标")
    s.add_argument("--root", required=True)
    s.add_argument("--metric", required=True, choices=METRICS)
    s.add_argument("--cond")
    s.add_argument("--judge")
    s.add_argument("--pooling")
    s.add_argument("--code-version")
    s.add_argument("--resolve", choices=("latest", "first"),
                   help="同一 split 有多行时如何取一行；不给则拒绝出数")
    s.add_argument("--include-backfilled", action="store_true")
    s.add_argument("--allow-partial", action="store_true")
    s.set_defaults(func=cmd_show)

    v = sub.add_parser("verify", help="核对报告里的数字有没有出处")
    v.add_argument("--root", required=True)
    v.add_argument("--file", required=True)
    v.add_argument("--include-backfilled", action="store_true")
    v.add_argument("--tol", type=float, default=0.02)
    v.set_defaults(func=cmd_verify)

    n = sub.add_parser("note", help="记一条矛盾/决策")
    n.add_argument("--root", required=True)
    n.add_argument("--kind", default="conflict",
                   choices=("conflict", "decision", "incident"))
    n.add_argument("--about", required=True)
    n.add_argument("--detail", required=True)
    n.set_defaults(func=cmd_note)

    args = p.parse_args()
    rc = args.func(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
