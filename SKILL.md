---
name: research-ledger
description: "科研实验账本：用 append-only 的 ledger.jsonl 做实验数值的唯一真值源，把『成功率 / 命中率 / 分母 / 倍数比』从记忆和对话摘要里移出来。任何涉及已报告数值的追问、报告刷新、论文写表、答辩准备、审稿回复，必须先读账本再回答；禁止凭记忆给数，禁止心算派生。也用于登记新跑批、复核账本完整性（八条不变式）、交叉核对报告里的每个数字有没有出处。Triggers on: 成功率, 命中率, 通过率, 分母, 多少条, 几倍, 账本, ledger, 登记这批, audit, 对不上, 哪个口径, 报告里的数字, refresh the numbers."
metadata:
  version: "1.0.0"
  last_updated: "2026-11-01"
  status: active
  data_access_level: raw
  task_type: open-ended
  related_skills:
    - academic-paper
---

# 科研账本 — 让数字有出处

> 本文示例中的数据集规模、批次命名、模型名与所有数值**均为虚构**，只用于说明机制。

一次实验会产出两种东西：**结果**和**关于结果的记忆**。结果留在文件里，记忆留在我这里，而我的记忆每过一轮对话就被压缩一次——压缩会悄悄丢掉限定条件，比如"这 1150 条里最后一片只有 150 条""这个数是严格判据下的"。

丢掉的正是被追问的那一层。

这个 skill 只做一件事：**把数字从我的记忆里搬到一个你能自己打开的文件夹**。它不提高实验质量，不加快跑批，也不让你少犯错。它让错误可定位。

## 硬纪律（违反任何一条，这个 skill 就等于没装）

**R1 任何涉及已报告数值的问题，先读账本再回答。**
包括：成功率多少、分母多少、为什么和上次不一样、报告里的数怎么来的、审稿人质疑的那个数。触发后第一动作是 `tree` 账本目录 + `show`，不是回忆。**回忆起来的数字和我算出来的数字，对你来说必须长得不一样。**

**R2 只 append，不改已有行。**
`ledger.jsonl` 是日志，不是工作簿。发现旧行写错了，做法是新增一行带 `patch_of` 的更正行，不是编辑旧行。要改历史必须由人手动做，并同时在 `CHANGELOG.md` 留一行原因。

**R3 不接手填的数值字段。**
`n_completed` / `n_success` / `fail_reason` / `n_total` 一律由脚本从原始输出和 manifest 算。我没有 `--n-success` 这个参数可用。若原始格式解析不了，**写 adapter，不要口算后填进去**。

**R4 不心算派生。**
任何除法、求和、求比都必须经 `show`。包括"大约是它的十倍"这种口头比较——那也是一个数字，也要能指到一行。

**R5 数字必须带分母和口径出现。**
写 `23.39% (269/1150, plain/strict-v1/mean)`，不写 `23.39%`。不写"约"。`约` 字是精度丢失的标记，出现它说明当时没查。

**R6 覆盖不全就报 REFUSED。**
缺任何一个 split，`show` 会拒绝出数。要出数得显式 `--allow-partial`，输出会打 `PARTIAL(缺 …)` 标。**在最后一片还没跑完的时候先算一个数发出去，是这个 skill 要防的头号事故。**
重复计数（同一 split 两行）**不给这个豁免**——缺一点和分母翻倍不是一类问题，后者必须先用 `--code-version` 或 `--resolve` 消歧。

## 什么时候必须触发

- 被问"这个数怎么来的 / 分母是多少 / 和上次为什么不一样"
- 刷新报告主口径、写论文主结果表、回审稿意见
- 汇报或答辩前自查
- 想把两个不同来源的数字做比较之前（先确认它们三个坐标一致，见 `references/schema.md`）
- 跑完一批新实验，准备收工

## 工作流

### 0. 建账（只做一次）

```bash
python scripts/ledger.py init --root ~/work/ledger \
  --queries ~/work/data/evalset.jsonl --split-field part \
  --verified-by "wc -l evalset.jsonl = 1150；part-01..part-05 各 200，part-06 是 150"
```

`--verified-by` 不是形式主义：它是你对分母的举证。写不出举证，说明分母还没确认过。

没有带分片字段的查询集时，用 `--splits part-01=200,part-02=200,...`，但这会把分母变成人手输入，属于风险项，要在 CHANGELOG 记明来源。

### 1. 登记（每跑完一批）

```bash
python scripts/ledger.py ingest --root ~/work/ledger \
  --run-id v2-plain-part-01 --split part-01 \
  --raw runs/v2/part-01/results.json \
  --code-version v2 --prompt-cond plain --judge-version strict-v1 \
  --pooling mean --model model-a --top-k 10
```

`--run-id` 用 `代码版本-口径-split`，**不要用日期**。日期会让"同一条件的第二次运行"看起来像新条件。

原始格式认识不了就写 adapter（十行）：

```python
def load(path):
    import json
    return [{"success": x["hit"] == 1, "fail_reason": x.get("why"), "query_id": x["qid"]}
            for x in json.load(open(path, encoding="utf-8"))["items"]]
```

### 2. 复核

```bash
python scripts/ledger.py audit --root ~/work/ledger
```

八条不变式见 `references/schema.md`。**FAIL 不为 0 时，从这里派生的任何数字都要带 INCOMPLETE。**

### 3. 派生

```bash
python scripts/ledger.py show --root ~/work/ledger --metric rate_weighted --cond plain --code-version v2
```

**`--code-version` 不能省。** v1 和 v2 是两套代码，混在一个分组里会把同一分片的两次运行加进同一个分母。同理，同一版本同一 split 出现两行时（rerun），`show` 会报 `REFUSED (重复计数 part-01)`，要么用 `--resolve latest` 显式选一行，要么回去补 `rerun_of`。

四个 metric 名字本身就是口径，没有模糊入口：

| metric | 算法 | 什么时候用 |
|---|---|---|
| `rate_weighted` | Σsuccess / Σtotal | **默认报告值**，最后一片少几条也不会被高估 |
| `rate_mean_of_parts` | 各片命中率的算术平均 | 只在"每片等权"确实有意义时用 |
| `rate_over_completed` | Σsuccess / Σcompleted | 回答"跑通的样本里成功多少" |
| `completion_rate` | Σcompleted / Σtotal | 基础设施健康度，不是方法效果 |

`rate_weighted` 和 `rate_mean_of_parts` 会因为分片大小不等而给出不同的数，**两者都叫"平均值"是这个领域最常见的口径事故**。

### 4. 核报告

```bash
python scripts/ledger.py verify --root ~/work/ledger --file report.md
```

把报告里所有 `xx.xx%` 和 `n×` 抽出来回账本找。MISS 的数字要么抄错，要么来自账本之外（心算、旧版本、别人的数）。**交出去之前跑一次，这是唯一能抓住"我编的"的机制。**

## 三条铁律（建账时没定死，后面改不动）

1. **聚合方式写进函数名**——存在一个叫 `rate()` 的模糊入口，所有数字之间就不可比。
2. **补跑永远是新行**——带 `patch_of`。把补跑的几条并回原片，分母就悄悄变了。"跑挂了就作废整片重跑"比"设计补跑语义"更省事。
3. **回填只许照抄**——从旧日志逆推历史行时，数字与旧报告冲突就用 `note` 记一条 conflict，**不要协调**。为了让数据"更一致"而微调，是伪造的起点，而且动机是善意的，最难自查。

## 反模式

- ❌ 对方催"就估一下"→ 仍然走 `show`，或明说"账本里没有这一格"。**估算不是免责理由。**
- ❌ 凭 `show` 的一个数字在正文里推出第二个数字（"那旧版本大概是新的十分之一"）。
- ❌ 账本缺字段时用"应该是……"补全。缺就是缺，报缺。
- ❌ 把账本当结论写进报告。账本是原料，报告是渲染结果，见 `references/schema.md` 的分层。
- ❌ 只有我一个人在读账本。三个月没人查 = 账本已死，先跑 `audit` 再信它。

## 失效边界（必须主动承认）

- **只管经过我的路径。** 你在服务器上直接改 `results.json`、在表格里改个数，这个 skill 一点办法都没有。它保护的是"经过 agent 的操作"。
- **不能验证自己是否被如实登记。** 一个记录系统无法验证自己的输入诚实与否。最多做到"账本 ↔ 原始输出"可交叉核对。
- **不防不合理。** 如果判定器有 bug 把所有样本判成功，账本会忠实地记下一个很高的数，而且比手写更权威。它防不可追溯，不防错误本身。
- **只覆盖已知维度。** 显存不足导致 batch size 变化、缓存命中率不同——这些不在账本里，但影响结果。

完整的十三条风险与对应防法见 `references/pitfalls.md`。
