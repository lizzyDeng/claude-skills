# 分析 agent · migrate（把现有票归并成业务问题）

OUT=$out

- 项目 brief（取数入口和口径，先读）：$brief
- 时间范围（UTC 日期）：$since ～ $until

按「同一群人想要的同一件东西」归并现有票，归并关系写进 absorbs。

🔴 红线：会进 GitHub 的字段（title、segment、want、walls、metric、evidence）不写 uid，也不写用户原话。只读：不写库、不动开关、不开票。

## 现有票

$state_summary

## 产出

把结果写成一个 JSON 文件，路径就是上面 `OUT=` 那一行，格式：

```json
{"mode": "migrate",
 "problems": [{"key": "示例键", "title": "示例问题", "segment": "示例人群", "want": "示例想要", "walls": ["示例墙"],
               "metric": {"name": "示例指标", "now": "示例现值", "direction": "up"}, "evidence": ["示例证据"],
               "impact": {"size": 0.3, "gap": 0.5, "confidence": "mid"}, "absorbs": ["T1"], "new_evidence": ""}],
 "maint": [],
 "walls": [],
 "close": []}
```
