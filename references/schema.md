# 账本字段与不变式

> 示例中的分片命名、规模与数值均为虚构。

## 一个数字有三个坐标

不同来源的数字不可比，几乎总是因为下面三条轴上有一处没对齐。**三条轴都写全，两个数字才可比。**

| 轴 | 字段 | 典型取值 | 不写的后果 |
|---|---|---|---|
| 口径 | `prompt_cond` | plain / templated | 两种口径的效果差一个量级，混在一起会得出"方法无效"或"方法极强"的假结论 |
| 判据 | `judge_version` | strict-v1 / lenient-v1 | 同一批数据能同时算出 61% 和 22%，看起来像自相矛盾 |
| 分母 | `n_total` / `n_completed` | 1150 / 实际完成数 | 分母随 bug 自动缩小，命中率自动变高 |

判据轴的裁决方式：**给两个指标分别命名**（`rate_strict` / `rate_lenient`），不要让它们共用一个名字。共用一个名字，每次引用都要在脑子里重新推导语义，那是最容易出错的地方。

## 字段表（16 必填）

| 字段 | 来源 | 说明 |
|---|---|---|
| `run_id` | 人 | 主键，格式 `代码版本-口径-split` |
| `split` | 人 | part-01…part-06，数据切片 |
| `code_version` | 人 | v1 / v2 / commit |
| `script_sha` | git 自动 | 拿不到时记 `unknown`，audit 报警告 |
| `prompt_cond` | 人 | 口径轴 |
| `judge_version` | 人 | 判据轴 |
| `pooling` | 人 | mean / max。**独立成维**，不要塞进模型超参 |
| `model` / `top_k` | 人 | 基础条件 |
| `n_total` | **manifest** | 该片应有几条。**不许由脚本报**——脚本报就随 bug 缩小 |
| `n_completed` | 脚本算 | 实际拿到模型输出的条数 |
| `n_success` | 脚本算 | 判定为成功的条数 |
| `fail_reason` | 脚本算 | 分布字典，必须能加回 `n_completed - n_success` |
| `queries_sha` | manifest | 数据集指纹 |
| `ran_at` / `host` | 自动 | 带时区；多机跑批必填 |

可选：`patch_of`（补跑指向原行）、`rerun_of`（对照重跑）、`backfilled` + `evidence`（回填行）、`ext`（新维度先放这里）、`evidence`（原始输出路径）。

## 八条不变式

`audit` 逐条查。FAIL 不为 0 时所有派生数字必须带 INCOMPLETE。

| 编号 | 检查 | 拦住什么 |
|---|---|---|
| **E1** | 每行 `n_total` == manifest 该 split 的值；缺 split 要报出来 | 分母被改过；最后一片没跑完就先算数 |
| **E2** | `0 ≤ n_success ≤ n_completed ≤ n_total` | 合并 rerun 时的越界 |
| **E3** | `sum(fail_reason) == n_completed - n_success`；`unclassified` 占比 >10% 警告 | 解析器错位；判定器有 bug |
| **E4** | `run_id` 唯一 | 重复登记导致分母翻倍 |
| **E5** | 同 `(split, code_version, prompt_cond, judge_version, pooling, model)` 出现多行时必须带 `rerun_of` / `patch_of`；指向的行必须存在 | 同条件的两次运行被当成两次独立实验 |
| **E6** | 同 split 的 `queries_sha` 全一致且等于 manifest | 数据集被改过而旧数字还在用 |
| **E7** | 16 个必填字段齐全 | 无法归类的数字（比错的数字更危险） |
| **E8** | `ran_at` 带时区；`script_sha` 不为 unknown | 服务器时区是 UTC 而你以为 +08:00；代码版本定位不到 |

## 三层结构：原料 / 派生 / 渲染

```
原始输出 results.json     ← 证据，改了就没救
        ↓ ingest（脚本算，人不碰）
ledger.jsonl              ← 原料，append-only
        ↓ show（唯一算术出口）
Measurement 对象           ← 派生，自带分母和口径
        ↓ 模板渲染
报告 / 论文表              ← 结论，可以被覆盖重写
```

**跨层是事故来源**：从原始输出直接抄数进报告（跳过账本），或者从报告反推账本（结论当原料）。层与层之间只许单向流动。

## 目录结构

```
ledger/
  manifest.json      # 分母锚定：splits、n_total、dataset_sha、verified_by
  ledger.jsonl       # 运行记录，只 append
  conflicts.jsonl    # 回填矛盾、事故（不协调，只记录）
  CHANGELOG.md       # 判据/口径/切片变更的原因
```

`CHANGELOG.md` 不能省。账本记"运行"，不记"决策"——半年后你看到 `judge_version` 从 strict-v1 变成 strict-v2，账本不会告诉你是谁决定的、为什么。

## 一个健康的账本长什么样

- 几行汇总能背下来（每个分片一行）。
- 任何被引用的数字，`show` 三十秒内能重现。
- 有三行以上的 `conflicts.jsonl`——一条都没有，通常说明回填时你偷偷协调过。
