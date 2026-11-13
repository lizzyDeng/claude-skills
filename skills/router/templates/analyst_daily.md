# 分析 agent · daily（每日验证前一天的上线）

OUT=$out

- 项目 brief（取数入口和口径，先读）：$brief
- 时间范围（UTC 日期）：$since ～ $until

🔴 红线：会进 GitHub 的字段（title、segment、want、walls、metric、evidence、note、new_wall、text）不写 uid，也不写用户原话。只读：不写库、不动开关、不开票。

## 现有业务票

$state_summary

## 产出

对每张等判定的尝试按「覆盖 → 读对话 → 判定」给结论。判定词只能用：no_entry、pending_sample、useful、wall_broken_not_retained、useless、harmful。
把结果写成一个 JSON 文件，路径就是上面 `OUT=` 那一行，格式：

```json
{"verdicts": [{"ticket": "T1", "attempt": 1, "verdict": "pending_sample", "touched": 2, "hit_wall": 1, "continued": 0, "note": "示例：走到墙的人不到 3 个"}],
 "walls": [{"ticket": "T1", "text": "示例墙"}],
 "notes": "示例"}
```
