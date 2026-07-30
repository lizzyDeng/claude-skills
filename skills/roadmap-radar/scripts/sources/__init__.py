"""Source 注册表。新增 source 只加一行，不碰任何 if-else。"""

from . import forge, github_issues, wayfinder

REGISTRY = {
    "forge": forge.collect,
    "github-issues": github_issues.collect,
    "wayfinder": wayfinder.collect,
}
