"""步骤 1：IO 出口的超时与非交互（plan v4 §3 第 1 步、§4②）。"""
import sys
import time

from ctx import Ctx


def test_run_timeout_returns_124_instead_of_hanging():
    t0 = time.time()
    r = Ctx().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert time.time() - t0 < 10                       # 弱网 / 挂死的子进程不能把整轮巡检拖住
    assert r.returncode == 124                          # 超时归入 unknown，和 git 的 rc 0/2/128 区分开


def test_run_sets_git_terminal_prompt_0():
    r = Ctx().run([sys.executable, "-c", "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'))"])
    assert r.stdout.strip() == "0"                      # git 要凭据时直接失败，不在后台等输入


def test_run_timeout_defaults_cover_lark_post():
    import inspect
    assert inspect.signature(Ctx.run).parameters["timeout"].default == 600         # 默认上界，谁都不会无限挂
    assert inspect.signature(Ctx.post_json).parameters["timeout"].default <= 10     # Lark 失败下一轮重试，不必等 30 秒
