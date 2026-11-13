# 分析 agent · calibrate（校准：盲跑一次）

OUT=$out

- 项目 brief（只看取数入口和口径）：$brief
- 时间范围（UTC 日期）：$since ～ $until

只从原始数据里找问题。盲跑：当前目录是一个空的临时目录；不要去找 router 的状态目录、已有的票或已有结论。

🔴 红线：会进 GitHub 的字段（title、segment、want、walls、metric、evidence）不写 uid，也不写用户原话。只读：不写库、不动开关、不开票。

## 产出

把结果写成一个 JSON 文件，路径就是上面 `OUT=` 那一行，格式：

```json
{"mode": "calibrate",
 "problems": [{"key": "示例键", "title": "示例问题", "segment": "示例人群", "want": "示例想要", "walls": ["示例墙"],
               "metric": {"name": "示例指标", "now": "示例现值", "direction": "up"}, "evidence": ["示例证据"],
               "impact": {"size": 0.3, "gap": 0.5, "confidence": "mid"}, "absorbs": [], "new_evidence": ""}]}
```
