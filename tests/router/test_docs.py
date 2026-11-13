import json
import re
import subprocess
import sys

from conftest import SCRIPTS

SKILL = SCRIPTS.parent
CLI = SCRIPTS / "router.py"
CMD = re.compile(r"(?:router\.py|\$ROUTER_CLI)(?:\s+--root\s+\S+)?\s+([a-z][a-z-]+)")


def help_commands():
    out = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True).stdout
    return set(re.search(r"\{([a-z,-]+)\}", out).group(1).split(","))


ANALYST_MODES = ("daily", "weekly", "calibrate", "migrate")


def test_docs_reference_only_existing_subcommands():
    known = help_commands()
    used = set()
    docs = ("SKILL.md", "worker.md", "templates/brief.md", "templates/brief_business.md",
            *(f"templates/analyst_{m}.md" for m in ANALYST_MODES))
    for doc in docs:
        used |= set(CMD.findall((SKILL / doc).read_text(encoding="utf-8")))
    assert used, "文档里一个 router.py 子命令都没引用"
    assert used - known == set()
    # 协议依赖的关键子命令都要在文档里出现过（v2：分诊 draft 退场，分析 agent / 判定 / 维护 / 影响分进场）
    assert {"discover", "tick", "watch", "start", "answer", "submit-card", "decide", "tell",
            "emit", "progress", "inbox", "status",
            "analyst", "verdict", "maint", "impact", "released", "prod-batch", "redirect", "close", "absorb",
            "migrate-apply"} <= used
    assert not (SKILL / "templates" / "triage.md").exists(), "v1 分诊模板应已删除（由分析 agent 模板替代）"


def test_worker_doc_lists_exactly_three_stop_conditions():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = re.search(r"^##[^\n]*三种情况[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    assert sec, "worker.md 缺「只在三种情况停」一节"
    items = re.findall(r"^\d+\.\s", sec.group(1), re.M)
    assert len(items) == 3


def test_worker_doc_has_eight_item_card_template():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = re.search(r"^##[^\n]*方向卡模板[^\n]*\n(.*?)(?=^## )", text, re.S | re.M).group(1)
    assert [int(n) for n in re.findall(r"^(\d+)\.\s", sec, re.M)][:8] == list(range(1, 9))
    assert "$ROUTER_HOME/cards/" in text and "progress card_ready" in text


def test_skill_frontmatter_and_triggers():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: router$", fm, re.M)
    for trig in ("router", "派发周报", "看板", "start", "判真"):
        assert trig in fm
    assert "分诊" not in fm                                                    # v2：没有分诊了


def test_direction_review_output_schema():
    text = (SKILL / "templates" / "direction_review.md").read_text(encoding="utf-8")
    assert '"pass"' in text and '"challenges"' in text
    for q in ("派单时的那个问题", "修症状", "更简单"):
        assert q in text


# ---- R7：方向审的 codex 只走 review-pack（⑨） ------------------------------------------

def _section(text, head):
    m = re.search(r"^## " + re.escape(head) + r"[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, f"缺「## {head}」一节"
    return m.group(1)


def test_skill_codex_review_goes_through_review_pack():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    sec = _section(text, "4.")
    assert "review-pack" in sec and "review-canary" in sec
    assert "canary_stale" in sec and "canary_leak" in sec                  # 过期先补跑金丝雀；泄漏要告诉用户
    assert 'codex exec -s read-only "<同一份材料>"' not in text            # 手拼命令删掉


# ---- 上线验收（postlaunch）：方向卡一节、方向审第 4 问、通用模板、SKILL 协议 ---------------------

def test_docs_reference_postlaunch_commands():
    known = help_commands()
    used = set()
    for doc in ("SKILL.md", "templates/postlaunch.md"):
        used |= set(CMD.findall((SKILL / doc).read_text(encoding="utf-8")))
    assert used - known == set()
    assert {"postlaunch-result", "released"} <= used


def test_card_template_has_postlaunch_section_with_type_and_three_layers():
    """方向卡「上线验收」：先标类型（增长型 / 风险型），再分三层：① 入口 ② 机制 ③ 行为收益（必答，主体）。"""
    for doc in ("worker.md", "templates/brief.md"):
        text = (SKILL / doc).read_text(encoding="utf-8")
        i = text.find("上线验收")
        assert i >= 0, doc
        sec = text[i:]
        for mark, words in (("①", ("入口", "覆盖率")), ("②", ("机制",)),
                            ("③", ("行为收益", "必答", "触点", "接着发", "下一条长度", "后续轮数", "离开率",
                                   "上线前同类触点", "留存", "small"))):
            j = sec.find(mark)
            assert j >= 0, (doc, mark)
            seg = sec[j:j + 400]
            for w in words:
                assert w in seg, (doc, mark, w)
        assert sec.find("增长型") < sec.find("①") and sec.find("风险型") < sec.find("①"), doc   # 先标类型
        assert "flat" in sec and "没用" in sec and "代价" in sec, doc
    worker = (SKILL / "worker.md").read_text(encoding="utf-8")
    tpl = re.search(r"^##[^\n]*方向卡模板[^\n]*\n(.*?)(?=^## )", worker, re.S | re.M).group(1)
    assert "上线验收" in tpl and "postlaunch_brief" in worker
    assert "$postlaunch_brief" in (SKILL / "templates" / "brief.md").read_text(encoding="utf-8")


def test_worker_doc_explains_zero_code_tickets():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = _section(text, "第 3 段")
    assert "零代码" in sec and "impl_done" in sec


def test_direction_review_asks_about_postlaunch_discrimination():
    text = (SKILL / "templates" / "direction_review.md").read_text(encoding="utf-8")
    items = re.findall(r"^(\d+)\.\s\*\*(.*?)\*\*", text, re.M)
    assert [int(n) for n, _ in items] == [1, 2, 3, 4]
    q4 = text[text.index("4. "):]
    assert "上线验收" in q4 and "判别力" in q4 and "T+1" in q4 and "没生效" in q4
    assert "行为收益" in q4 and "触点" in q4 and "bug 不再出现" in q4                # 只证明 bug 没了也算挑战


POSTLAUNCH_SECTIONS = ("上线", "入口", "机制", "行为收益", "护栏", "结论和动作")
VERDICTS = ("生效", "没入口", "没用", "有害", "看不出来")


def test_postlaunch_template_has_six_sections_and_five_verdicts():
    import github
    text = (SKILL / "templates" / "postlaunch.md").read_text(encoding="utf-8")
    heads = re.findall(r"^\d+\.\s\*\*(.+?)\*\*", text, re.M)
    assert tuple(heads) == POSTLAUNCH_SECTIONS
    for v in VERDICTS:                                                       # 每类结论一行：判据 + 当天动作
        row = re.search(r"^\|\s*" + v + r"\s*\|(.+)\|(.+)\|\s*$", text, re.M)
        assert row and row.group(2).strip(), v
    assert "postlaunch_brief" in text and "只读" in text
    for w in ("增长型", "风险型", "--type", "--behavior", "代价", "small"):
        assert w in text, w
    good = re.search(r"^\|\s*生效\s*\|(.+)\|(.+)\|\s*$", text, re.M).group(1)
    assert "增长型" in good and "风险型" in good                                 # 生效的判据按类型分
    useless = re.search(r"^\|\s*没用\s*\|(.+)\|(.+)\|\s*$", text, re.M).group(1)
    assert "机制" in useless and "flat" in useless                               # 增长型：机制生效但行为 flat = 没用
    for word in re.findall(r"[0-9A-Za-z_-]{6,}", text):                      # 不出现项目专属的 uid
        assert not github._is_uid(word), word
    for banned in ("aifriends", "her-", "girlfriend", "supercli", "persona"):  # 不出现项目专属的名字
        assert banned not in text.lower(), banned


def test_skill_documents_postlaunch_protocol():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    s2 = _section(text, "2.")
    row = next(l for l in s2.splitlines() if l.startswith("| `postlaunch_due`"))
    assert "templates/postlaunch.md" in row and "postlaunch-result" in row and "postlaunch_brief" in row
    s5 = _section(text, "5.")
    assert "关开关" in s5 and "改配置" in s5 and "修复票" in s5
    s6 = _section(text, "6.")
    assert "postlaunch-result" in s6 and "--t0" in s6
    pl = re.search(r"^## [^\n]*上线验收[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert pl, "SKILL.md 缺「上线验收」一节"
    body = pl.group(1)
    assert "released" in body and "postlaunch" in body and "done" in body
    for x in ("T+1", "T+3", "T+7", *VERDICTS, "交周报", "增长型", "风险型", "--type", "--behavior", "flat"):
        assert x in body, x
    assert "票的终点是上线验收，不是 released" in text


# ---- v2：分析 agent 模板（每日 / 每周 / 校准 / 迁移）----------------------------------------------

GH_FIELDS = ("title", "segment", "want", "walls", "metric", "evidence")
PROBLEM_KEYS = {"key", "title", "segment", "want", "walls", "metric", "evidence", "impact", "absorbs", "new_evidence"}
VERDICT_KEYS = {"ticket", "attempt", "verdict", "touched", "hit_wall", "continued", "note", "new_wall"}


def _analyst_tpl(mode):
    return (SKILL / "templates" / f"analyst_{mode}.md").read_text(encoding="utf-8")


def _json_example(text, mode):
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) == 1, f"{mode}：要恰好一个 ```json 输出示例"
    return json.loads(blocks[0])


def _check_problems(mode, ex):
    import router
    assert ex.get("mode") == mode and ex["problems"], mode
    for i, p in enumerate(ex["problems"]):
        assert set(p) == PROBLEM_KEYS, (mode, set(p) ^ PROBLEM_KEYS)
        assert set(p["metric"]) == {"name", "now", "direction"} and set(p["impact"]) == {"size", "gap", "confidence"}
        router._check_problem(i, p)                     # 和 propose 的校验是同一个函数：示例照抄也收得进来


def test_analyst_templates_contract():
    import store
    for mode in ANALYST_MODES:
        text = _analyst_tpl(mode)
        assert re.search(r"^OUT=\$out$", text, re.M), f"{mode} 缺一行 OUT=$out"
        assert text.index("OUT=") == text.index("OUT=$out"), f"{mode}：第一个 OUT= 必须就是那一行（运行器按它找产出路径）"
        red = [line for line in text.splitlines() if "会进 GitHub 的字段" in line]
        assert red, f"{mode} 缺红线句"
        assert all(f in red[0] for f in GH_FIELDS) and "uid" in red[0] and "原话" in red[0], (mode, red[0])
        ex = _json_example(text, mode)
        if mode == "daily":
            assert set(ex) <= {"verdicts", "walls", "notes"} and ex["verdicts"], set(ex)
            for v in ex["verdicts"]:
                assert set(v) == VERDICT_KEYS, set(v) ^ VERDICT_KEYS
                assert v["verdict"] in store.BIZ_VERDICTS
                assert all(type(v[k]) is int for k in ("attempt", "touched", "hit_wall", "continued"))
                if v["verdict"] == "wall_broken_not_retained":
                    assert v["new_wall"]
            assert all(set(w) == {"ticket", "text"} for w in ex["walls"])
        else:
            _check_problems(mode, ex)
    # 校准：盲跑，只给数据入口和时间范围
    cal = _analyst_tpl("calibrate")
    assert "$state_summary" not in cal and "$home" not in cal
    assert "$brief" in cal and "$since" in cal and "$until" in cal
    assert all(p["absorbs"] == [] for p in _json_example(cal, "calibrate")["problems"])


def test_analyst_daily_three_steps_and_verdict_words():
    import store
    text = _analyst_tpl("daily")
    assert [h for h in re.findall(r"^\d+\.\s\*\*(.+?)\*\*", text, re.M)][:3] == ["覆盖", "读对话", "判定"]
    for v in store.BIZ_VERDICTS:                        # 判定词只能用这六个
        assert f"`{v}`" in text, v
    assert "走到墙" in text and "不到 3" in text and "pending_sample" in text
    assert "7 天" in text and "多数" in text            # 待样本最长 7 天；不一致按多数判
    for banned in ("显著", "对照组", "置信区间", "A/B"):  # 不做 A/B
        assert banned not in text, banned
    assert "$state_summary" in text


def test_analyst_weekly_method_impact_maint_and_rejected_memory():
    text = _analyst_tpl("weekly")
    for w in ("第一段对话", "第二天回来", "离开前", "漏斗", "墙还在不在"):   # spec §6 方法五条
        assert w in text, w
    assert "$home/analyst/rejected.jsonl" in text and "new_evidence" in text
    assert "size" in text and "gap" in text and "confidence" in text
    assert "`maint`" in text and "$home/surfaces.json" in text           # 维护项的代码面只能用 surfaces.json 里有的
    assert "同一群人想要的同一件东西" in text                               # 先归并再提新问题
    ex = _json_example(text, "weekly")
    assert set(ex) <= {"mode", "problems", "maint", "walls", "close", "diagnosis"}
    assert all(set(m) == {"title", "problem", "surfaces", "unblocks"} for m in ex["maint"]) and ex["maint"]
    assert all(set(w) == {"ticket", "text"} for w in ex["walls"])
    assert all(set(c) == {"ticket", "reason"} for c in ex["close"])
    for banned in ("显著", "对照组", "置信区间"):
        assert banned not in text, banned


def test_analyst_migrate_groups_by_same_people_same_want():
    text = _analyst_tpl("migrate")
    assert "$state_summary" in text and "同一群人想要的同一件东西" in text and "absorbs" in text
    ex = _json_example(text, "migrate")
    assert any(p["absorbs"] for p in ex["problems"])
    assert "attach" in text and all(set(x) == {"ticket", "absorbs"} for x in ex["attach"]) and ex["attach"]  # 9b f9


# ---- v2：SKILL.md 协议（票级授权 / 判真 / 唤醒键 / 分析 agent）、worker 的业务票循环 ---------------

V2_WAKE_ROWS = ("analyst_done", "rollback_needed", "no_entry", "return_to_user", "releases_unlabeled")


def test_skill_v2_sections():
    import tick
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "票级授权" in text and "判真" in text and "rollback_needed" in text
    assert "triage_needed" not in text and "triage.md" not in text
    # §2：每个唤醒键一行，写清 router 怎么做；表里的键和 tick 的 WAKE_KEYS 一一对上
    s2 = _section(text, "2.")
    rows = {m.group(1): line for line in s2.splitlines() for m in [re.match(r"^\|\s*`([a-z_]+)`", line)] if m}
    for k in tick.WAKE_KEYS:
        assert k in rows, f"第 2 节缺 `{k}` 一行"
    for k in V2_WAKE_ROWS:
        assert len(rows[k]) > 60, k
    assert "当天照 rollback 文本撤回" in rows["rollback_needed"] and "告诉用户" in rows["rollback_needed"]
    # §1 启动：分析 agent 的状态和校准状态
    s1 = _section(text, "1.")
    assert "分诊" not in s1 and "analyst" in s1 and "calibrated" in s1
    # §3 分析 agent：先盲跑 calibrate → 对照已知问题判 → analyst calibrated → migrate
    s3 = _section(text, "3.")
    order = [s3.find(x) for x in ("--mode calibrate", "已知问题", "analyst calibrated", "--mode migrate")]
    assert -1 not in order and order == sorted(order), order
    for m in ANALYST_MODES:
        assert m in s3, m
    # §4：业务票的方向卡照旧方向审，submit 后自动确认
    s4 = _section(text, "4.")
    assert "业务票" in s4 and "自动确认" in s4 and "票级授权" in s4
    # §5 谁定什么
    s5 = _section(text, "5.")
    for w in ("判真", "票内换方向", "上 prod", "票级授权", "花钱", "撤不回", "商店包"):
        assert w in s5, w
    # §6：判真 / 关票的五种回答
    s6 = _section(text, "6.")
    for say in ("「Qn 真」", "「Qn 真，先放着」", "「Qn 不是：", "「Qn 关」", "「Qn 不关」"):
        assert say in s6, say
    assert "router_ships" in text                       # prod-batch 分出来由 router 自己上的业务票
    # 决定单按业务影响排，不再按「挡住了几张票」排
    assert "按业务影响" in text and "按「挡住了几张票」排序" not in text


def test_worker_and_brief_business_section():
    brief = (SKILL / "templates" / "brief.md").read_text(encoding="utf-8")
    assert "$kind" in brief and "$business_section" in brief and "## 业务票" not in brief   # 业务票一节按 kind 代入
    for doc in ("worker.md", "templates/brief_business.md"):
        text = (SKILL / doc).read_text(encoding="utf-8")
        m = re.search(r"^## 业务票[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        assert m, f"{doc} 缺「## 业务票」一节"
        sec = m.group(1)
        for w in ("一个问题", "一个方向", "开关", "回放", "报价", "判定", "--rollback"):
            assert w in sec, (doc, w)
        for tag in ("spend", "irreversible", "store_release"):
            assert tag in text, (doc, tag)
    stop = re.search(r"^##[^\n]*三种情况[^\n]*\n(.*?)(?=^## )", (SKILL / "worker.md").read_text(encoding="utf-8"),
                     re.S | re.M).group(1)
    assert "irreversible" in stop and "store_release" in stop


# ---- Task 9b：协议跟代码对齐（换方向 / 代答 / 关票 / 并旧票 / 唤醒键投递 / 重启恢复） -----------------

ONCE_KEYS = ("rollback_needed", "no_entry", "return_to_user", "analyst_done", "releases_unlabeled")
REDELIVERED = ("cards_for_review", "slow_path_events", "postlaunch_due")


def test_skill_9b_commands_and_wording():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    s2 = _section(text, "2.")
    pre = s2.split("| 返回字段 |")[0]                                          # 表格前的说明：哪些只交一次、哪些会重投
    for k in ONCE_KEYS + REDELIVERED:
        assert f"`{k}`" in pre, k
    rows = {m.group(1): line for line in s2.splitlines() for m in [re.match(r"^\|\s*`([a-z_]+)`", line)] if m}
    for k in ("rollback_needed", "return_to_user"):
        assert "router.py redirect" in rows[k] and "--resume" not in rows[k], k   # 换方向一律 redirect
    assert 'answer <Qn> "router 决定' not in text                               # 代答走 --as-router，不冒充原话
    assert "answer <Qn>" in _section(text, "5.") and "--as-router" in _section(text, "5.")
    s6 = _section(text, "6.")
    assert "「关 T" in s6 and "router.py close" in s6 and "router.py redirect" in s6
    assert "router.py absorb" in text
    s1 = _section(text, "1.")
    assert "`harm`" in s1 and "`return`" in s1 and "没有入口" in s1             # 重启恢复：查挂着的撤回 / 退回 / 等入口
    for doc in ("SKILL.md", "templates/analyst_daily.md"):                     # 新墙挂在末尾，下一次对准第一道没拆的墙
        assert "下一次尝试拆它" not in (SKILL / doc).read_text(encoding="utf-8"), doc
    assert "第一道没拆的墙" in text
    mig = (SKILL / "templates" / "analyst_migrate.md").read_text(encoding="utf-8")
    assert "absorb" in mig and "做完算一次" not in mig and "做完算一次" not in text


def test_skill_9b_round2_migrate_confirm_and_branch_per_attempt():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    s3 = _section(text, "3.")
    assert "router.py migrate-apply" in s3 and "--said" in s3 and "--skip" in s3 and "--hold" in s3
    assert "propose --from <out>`（weekly）" in s3                               # migrate 不再走 propose
    rows = {m.group(1): line for line in _section(text, "2.").splitlines()
            for m in [re.match(r"^\|\s*`([a-z_]+)`", line)] if m}
    assert "migrate-apply" in rows["analyst_done"]
    assert "router/<T>-a<n>" in text and "重排" in _section(text, "6.")
    mig = (SKILL / "templates" / "analyst_migrate.md").read_text(encoding="utf-8")
    assert "逐条判真" not in mig and "确认" in mig
    worker = (SKILL / "worker.md").read_text(encoding="utf-8")
    assert "router/$ROUTER_TICKET" not in worker                                 # 分支以派单为准（业务票每次尝试一条新分支）
    assert "$branch" in (SKILL / "templates" / "brief.md").read_text(encoding="utf-8")


def test_skill_branch_suffix_not_promised_equal_to_attempt_n():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "n = 这次尝试的序号" not in text and "不算并进来的历史尝试" in text
    assert "stale_checkouts" in text                                             # 留着的 checkout 不删、记下来


# ---- 终审修复：协议口令和代码对齐 -------------------------------------------------------------------

def test_skill_final_fix_wording():
    import tick
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    s2 = _section(text, "2.")
    pre = s2.split("| 返回字段 |")[0]
    assert "`prod_ready`" in pre and "prod_ready" in tick.WAKE_KEYS                 # #1：只交一次的唤醒键
    rows = {m.group(1): line for line in s2.splitlines() for m in [re.match(r"^\|\s*`([a-z_]+)`", line)] if m}
    pr = rows["prod_ready"]
    assert "router.py prod-batch" in pr and "router_ships" in pr and "放行" in pr and "released" in pr
    assert "--as-user" in rows["return_to_user"] and "--as-router" in rows["return_to_user"]   # #4
    assert "tell <T>" in rows["slow_path_events"] and "--as-router" in rows["slow_path_events"]  # #14
    assert "topic=next" in rows["new_queue"] and "redirect" in rows["new_queue"]       # #3
    s4 = _section(text, "4.")
    assert '--resume --as-router' in s4 and "surfaces <T> --set" in s4                  # #14 方向审挑战 / #7 代码面
    s6 = _section(text, "6.")
    tell = next(l for l in s6.splitlines() if l.startswith("| 「告诉 A"))
    assert 'tell <T> "<原话>"' in tell and "不加 `--as-router`" in tell                # 用户的话照旧用原话
    assert "「Qn 放行」" in s6 and "topic=next" in s6 and "--surfaces" in s6
    s7 = re.search(r"^### 业务票[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M).group(1)
    for w in ("发版批次", "staging 领先 main", "待走查", "待放行", "router_ships", "batch", "已放行"):  # #6
        assert w in s7, w
    assert "核对过" in s7 and "不关票" in s7 and "router.py close" in s7                 # #5
    assert "Refs #" in s7 and "Closes #" in s7                                         # #15
    assert "已试" in s7 and "stale_checkouts" in s7 and "status" in s7 and "旧尝试" in s7  # #8 / #9
    assert "没有在途尝试" in text and "满 7 天" in s7 and "no_entry" in s7               # #3 / #13
    assert "已关的票不复活" in s7                                                       # #2


def test_worker_and_brief_business_pr_refs_not_closes():
    for doc in ("worker.md", "templates/brief_business.md"):
        text = (SKILL / doc).read_text(encoding="utf-8")
        sec = re.search(r"^## 业务票[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M).group(1)
        assert "Refs #<issue>" in sec and "Closes #" in sec, doc                       # #15：业务票写 Refs，不写 Closes
    worker = (SKILL / "worker.md").read_text(encoding="utf-8")
    assert "Refs #<issue>" in _section(worker, "第 3 段")
    assert "router 转述 / 决定" in worker                                               # #14：worker 认得 router 的话
    daily = (SKILL / "templates" / "analyst_daily.md").read_text(encoding="utf-8")
    assert "满 7 天" in daily and "`hit_wall` = 0" in daily and "判 `no_entry`" in daily   # #13
