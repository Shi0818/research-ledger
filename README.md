# 科研账本 / research-ledger

一个 Agent Skill：把实验数值从"记忆"搬到一个你能自己打开的文件夹。

> 它不让你少犯错，它让错误**可定位**。

## 它解决什么

一次实验会产出两种东西：结果，和关于结果的记忆。结果留在文件里，记忆留在 agent 手里——而 agent 的记忆每过一轮对话就被压缩一次，压缩会悄悄丢掉限定条件（"这批只有 150 条""这个数是严格判据下的"）。丢掉的正是被追问的那一层。

于是常见结局是：四个数字散落在 stdout、报告、聊天记录里，而它们其实各有三个坐标（**口径 / 判据 / 分母**），只写数值根本无法比较。

这个 skill 只做一件事：让"手填数字"在结构上做不到。

## 它做什么

- **`ledger.jsonl`**：append-only 的运行记录，唯一真值源。没有 update / delete 子命令。
- **`n_total` 只来自 manifest**：分母是数据集的性质，不由跑批脚本报告——否则脚本 bug 掉几条，分母会自动缩小，命中率自动变高。
- **`ingest` 不收人手填的数值**：没有 `--n-success` 这种参数，`n_success` 只能由脚本从原始输出算。
- **四个 metric 名字即口径**，没有模糊入口：`rate_weighted` / `rate_mean_of_parts` / `rate_over_completed` / `completion_rate`。
- **`audit`**：八条不变式（分母锚定、分布守恒、指纹一致、同条件重复……）。
- **`verify`**：把报告里的每个 `xx.xx%` 和 `n×` 抽出来回账本找，找不到就是没有出处。

## 安装

放到 agent 的 skills 目录即可，例如：

```
~/.workbuddy/skills/research-ledger/
```

纯标准库，Python 3.9+，无第三方依赖。

## 快速开始

```bash
# 1. 建账：锚定分母（只做一次）
python scripts/ledger.py init --root ~/work/ledger \
  --queries data/evalset.jsonl --split-field part \
  --verified-by "wc -l = 1150；part-01..05 各 200，part-06 是 150"

# 2. 登记：每跑完一批
python scripts/ledger.py ingest --root ~/work/ledger \
  --run-id v2-plain-part-01 --split part-01 --raw runs/part-01/results.json \
  --code-version v2 --prompt-cond plain --judge-version strict-v1 \
  --pooling mean --model model-a --top-k 10

# 3. 复核八条不变式
python scripts/ledger.py audit --root ~/work/ledger

# 4. 派生（数字自带分母和口径）
python scripts/ledger.py show --root ~/work/ledger --metric rate_weighted --code-version v2

# 5. 交出去之前，核一遍报告里的数字
python scripts/ledger.py verify --root ~/work/ledger --file report.md
```

## 它管不到什么（写在明处）

- 只管**经过 agent** 的路径。你在服务器上手改 `results.json`，它没办法。
- 不能验证自己是否被如实登记——记录系统无法验证自己的输入诚实与否。
- **不防不合理**。判定器有 bug 时它会忠实地记下一个很高的数，而且比手写更权威。

## 文档

| 文件 | 内容 |
|---|---|
| [SKILL.md](SKILL.md) | 触发条件、六条硬纪律、工作流、反模式 |
| [references/schema.md](references/schema.md) | 三条漂移轴、字段表、八条不变式、三层结构 |
| [references/pitfalls.md](references/pitfalls.md) | 十三条风险：哪些能设计死、哪些只能留痕、哪些碰不到 |

> 文中示例的数据规模、分片命名与数值均为虚构，仅用于说明机制。

## License

MIT
