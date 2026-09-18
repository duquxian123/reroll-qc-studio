#!/usr/bin/env python
"""自检：在视频资源池上跑一遍质检，输出误杀 / 漏检。

为什么直接测资源池而不是测"抽卡结果"
------------------------------------
抽卡是随机的，每次抽到的片段不同，测出来的数字会飘。
直接对**资源池里的每个片段**逐个质检，结果稳定、可复现，适合当回归测试。

用法
----
    python tools/selftest.py
    python tools/selftest.py --only sharpness,flicker
    python tools/selftest.py --verbose
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from checks import all_checks  # noqa: E402
from app import config  # noqa: E402
from app.dataset import Project, load_pool  # noqa: E402
from app.models import Candidate, CheckConfig, Shot  # noqa: E402
from app.orchestrator import Orchestrator, summarize  # noqa: E402
from app.runtime import Runtime  # noqa: E402
from app.store import SessionState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="只跑指定检测项，逗号分隔")
    parser.add_argument("--verbose", action="store_true", help="打印逐项分数")
    args = parser.parse_args()

    clips = load_pool()
    if not clips:
        print("资源池是空的。先运行 tools/make_dataset.py")
        return 1

    enabled = [c.id for c in all_checks()]
    if args.only:
        enabled = [c.strip() for c in args.only.split(",") if c.strip()]
    configs = [CheckConfig(check_id=cid, order=i) for i, cid in enumerate(enabled)]

    # 一个片段一个"分镜"，这样能复用真实的编排逻辑
    shots: list[Shot] = []
    for clip in clips:
        stem = clip.name.rsplit(".", 1)[0]
        shots.append(
            Shot(
                shot_id=stem,
                scene="",
                prompt=clip.note or stem,
                expected_duration_s=5.0,
                aspect_ratio="16:9",
                candidates=[
                    Candidate(
                        candidate_id=stem,
                        file=clip.file,
                        shot_id=stem,
                        defects=clip.defects,
                        severity=clip.severity,
                        expected_verdict=clip.verdict,
                    )
                ],
            )
        )

    project = Project(project_id="selftest", name="自检", shots=shots)
    runtime = Runtime(SessionState())
    runtime.set_project(project)

    print(f"资源池：{len(clips)} 个片段（好片 {sum(1 for c in clips if c.is_good)}）")
    print(f"检测项：{', '.join(enabled)}\n")

    started = time.time()
    results = Orchestrator(runtime).run_shots(shots, configs)
    summary = summarize(results)
    elapsed = time.time() - started

    for shot_result in results:
        cand = shot_result.candidates[0]
        truth = cand.expected_verdict or "?"
        agree = "一致" if cand.passed == (truth == "accept") else "**不一致**"
        mark = "✓" if cand.passed else "✗"
        print(
            f"  {mark} {cand.candidate_id:28s} {cand.total_score:6.1f}  "
            f"标注={truth:6s} {agree}"
        )
        if args.verbose:
            for c in cand.checks:
                flag = "✓" if c.passed else "✗"
                print(f"      {flag} {c.check_id:12s} {c.score:5.1f}  {c.notes}")

    s = summary.to_dict()
    print("\n" + "=" * 68)
    print(f"耗时 {elapsed:.1f}s")
    print(f"片段 {s['candidates']}  通过 {s['passed']}  不通过 {s['failed']}")
    print(
        f"正确通过 {s['true_pass']}  误杀 {s['false_reject']}  "
        f"正确拒绝 {s['true_reject']}  漏检 {s['false_accept']}"
    )
    print(f"误杀率 {s['false_reject_rate']}%   漏检率 {s['false_accept_rate']}%")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
