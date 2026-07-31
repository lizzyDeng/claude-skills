"""打优先级 label 的写能力 —— 和展示（读 label）完全分开。

优先级是讨论后的产品决策，radar 不猜；讨论定了之后：

    radar.py --set-priority "36=P0,28=P1,14=P2"

对每张票：先删掉已有的 P<n> label（一张票只允许一个），再打新的。
label 不存在时自动在仓库里建（P0 红 / P1 橙 / P2 蓝 / 其余灰）。
"""

import re

ASSIGN_RE = re.compile(r"^\s*#?(\d+)\s*=\s*[Pp](\d)\s*$")
P_LABEL_RE = re.compile(r"^[Pp]\d$")

LABEL_COLORS = {0: "b60205", 1: "d93f0b", 2: "1d76db"}
DEFAULT_COLOR = "97a0aa"


def parse_assignments(spec):
    """"36=P0, 28=P1" -> [(36, 0), (28, 1)]。格式错直接抛，不半途写一半。"""
    out = []
    for part in spec.split(","):
        if not part.strip():
            continue
        match = ASSIGN_RE.match(part)
        if not match:
            raise ValueError(
                "看不懂 %r；格式：--set-priority \"36=P0,28=P1\"" % part.strip())
        out.append((int(match.group(1)), int(match.group(2))))
    if not out:
        raise ValueError("--set-priority 是空的")
    return out


def repo_from_config(config):
    for source in config.get("sources", []):
        if source.get("repo"):
            return source["repo"]
    raise SystemExit("配置里没有带 repo 的 source，不知道往哪个仓库打 label")


def set_priorities(ctx, repo, assignments):
    """执行打标。返回 [(number, old_label|None, new_label)] 摘要。"""
    ensured = set()
    summary = []
    for number, priority in assignments:
        new_label = "P%d" % priority
        if new_label not in ensured:
            ctx.gh_run(["label", "create", new_label, "--repo", repo,
                        "--color", LABEL_COLORS.get(priority, DEFAULT_COLOR),
                        "--description", "roadmap-radar 泳道优先级",
                        "--force"])
            ensured.add(new_label)
        issue = ctx.gh_json(["issue", "view", str(number), "--repo", repo,
                             "--json", "labels"])
        old = [label["name"] for label in issue.get("labels", [])
               if P_LABEL_RE.match(label["name"])]
        edit = ["issue", "edit", str(number), "--repo", repo,
                "--add-label", new_label]
        for stale in old:
            if stale != new_label:
                edit += ["--remove-label", stale]
        ctx.gh_run(edit)
        replaced = next((s for s in old if s != new_label), None)
        summary.append((number, replaced, new_label))
    return summary
