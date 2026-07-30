#!/usr/bin/env python3
"""roadmap-radar CLI —— goal 拓扑与进度可视化。

    radar.py                     # collect + render + 开浏览器
    radar.py --out radar.html
    radar.py --graph graph.json  # 只 collect
    radar.py --from graph.json   # 只 render（离线迭代 UI，不打 API）
    radar.py --doctor            # 只报告断链，零写入
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collect as collect_mod  # noqa: E402
import render as render_mod  # noqa: E402
from sources import REGISTRY  # noqa: E402

CONFIG_NAME = ".radar.json"


class Ctx:
    """给 source 用的 IO 出口。测试注入替身，生产打 gh。"""

    def __init__(self, root):
        self.root = root

    def resolve(self, rel):
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    def gh_json(self, args):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("gh %s failed: %s" % (" ".join(args), proc.stderr.strip()))
        text = proc.stdout.strip()
        return json.loads(text) if text else []


def load_config(root, explicit):
    path = explicit or os.path.join(root, CONFIG_NAME)
    if not os.path.isfile(path):
        raise SystemExit(
            "找不到 %s。在项目根建一个，例如：\n"
            '{"sources": [\n'
            '  {"type": "forge", "path": "project-roadmap/roadmap.json"},\n'
            '  {"type": "wayfinder", "repo": "owner/name"}\n'
            "]}" % (explicit or CONFIG_NAME)
        )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def print_doctor(report):
    order = (("dangling_parents", "父节点找不到（断链）"),
             ("orphans", "父节点已消失（被断开）"),
             ("no_goal", "没挂到任何 goal"),
             ("empty_groups", "空的 goal / 地图"),
             ("unmapped_status", "状态词没映射"))
    total = 0
    for key, label in order:
        rows = report.get(key) or []
        total += len(rows)
        print("\n%s（%d）" % (label, len(rows)))
        for row in rows:
            title = row.get("title") or row.get("id")
            extra = row.get("missing_parent") or row.get("state") or ""
            print("  - %s%s" % (title, (" → %s" % extra) if extra else ""))
    print("\n共 %d 项。只报告不修 —— 修法是项目专属的。" % total)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="radar")
    parser.add_argument("--config")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--out", default="radar.html")
    parser.add_argument("--graph")
    parser.add_argument("--from", dest="from_graph")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    if args.from_graph:
        with open(args.from_graph, encoding="utf-8") as handle:
            graph = json.load(handle)
    else:
        config = load_config(args.root, args.config)
        graph = collect_mod.build(config, Ctx(args.root), REGISTRY)

    if args.doctor:
        print_doctor(graph.get("doctor") or {})
        return 0

    if args.graph:
        with open(args.graph, "w", encoding="utf-8") as handle:
            json.dump(graph, handle, ensure_ascii=False, indent=2)
        print("graph → %s" % args.graph)
        if not args.out:
            return 0

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = render_mod.render(graph, generated_at=stamp)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(html)
    print("radar → %s" % os.path.abspath(args.out))
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
