"""
评测集：改提示词、模板或编剧技能之后，用同一批真实产品跑一遍，对比分数，而不是凭感觉判断好坏。

  cd pipeline
  python -m evals.run_eval                 # 全部用例：生成剧本 + 硬校验（每条 1 次 LLM 调用）
  python -m evals.run_eval --judge         # 再加 LLM 按评分标准打分（每条多 1 次调用）
  python -m evals.run_eval --case cat-douyin --self-review
  python -m evals.run_eval --ecom          # 主图 / 详情页用例：同一产品在拼多多 / 微信小店的定位、屏序和合规

结果写入 evals/results/<时间>.json，终端打印汇总表。
分数只是相对参考：同一份提示词跑两次分数也会波动，看多条用例的平均变化，不看单条。
LLM 打分不能代替真实投放的完播率和转化数据。
"""

import argparse
import json
import sys
import time
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))

import category_playbook as cp          # noqa: E402
import layer1_planning as l1            # noqa: E402
import product_brief as brief_mod       # noqa: E402
import quality_check as qc              # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
CASES_PATH = EVAL_DIR / "cases.json"
RESULTS_DIR = EVAL_DIR / "results"
SKILL_PATH = PIPELINE_DIR / "skills" / "short-video-selling" / "SKILL.md"
RUBRIC_SIZE = len([ln for ln in qc.VIDEO_RUBRIC.splitlines() if ln.strip()])

JUDGE_SYSTEM = f"""\
你是资深短视频带货操盘手，负责审核带货视频的剧本和分镜。按下面的评分标准逐条打分（共 {RUBRIC_SIZE} 条，
每条 1-5 分，5 分最好），每条用一句话说明扣分原因，最后给出最该改的一个问题。

【评分标准】
{qc.VIDEO_RUBRIC}

只输出 JSON：{{"scores": [每条一个整数], "reasons": [每条一句话], "top_fix": "..."}}"""


def load_cases(path: Path = CASES_PATH, key: str = "cases") -> list[dict]:
    """读取用例（key="ecom_cases" 读主图 / 详情页用例）；same_brief_as 复用视频用例的产品信息"""
    data = json.loads(path.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in data["cases"]}
    cases = data.get(key) or []
    for c in cases:
        if "same_brief_as" in c:
            c["brief"] = by_id[c["same_brief_as"]]["brief"]
    return cases


def storyboard_text(sb: dict) -> str:
    """给评委看的剧本摘要：节拍、口播、画面"""
    lines = [f"片名：{(sb.get('script') or {}).get('title', '')}"]
    for s in sb.get("shots", []):
        lines.append(f"镜头 {s.get('id')}（{s.get('emotion_beat')}，{s.get('duration_sec')} 秒）"
                     f"\n  口播：{s.get('voiceover', '')}\n  画面：{s.get('visual_intent', '')}")
    return "\n".join(lines)


def parse_judge(data: dict) -> dict:
    """清洗评委输出；分数个数不对时返回 error"""
    scores = [min(max(int(x), 1), 5) for x in data.get("scores", [])]
    if len(scores) != RUBRIC_SIZE:
        return {"error": f"评委给了 {len(scores)} 个分数，应为 {RUBRIC_SIZE} 个"}
    return {"scores": scores, "reasons": data.get("reasons", []), "top_fix": data.get("top_fix", ""),
            "avg": round(sum(scores) / len(scores), 2)}


def judge(case: dict, sb: dict) -> dict:
    """LLM 按评分标准打分；失败时返回 {"error"}，不影响硬校验结果"""
    brief = case["brief"]
    user = (f"商品：{brief['name']}（{brief.get('category', '')}）\n"
            f"核心卖点：{'；'.join(brief_mod.lines_of(brief.get('usp')))}\n"
            f"平台：{case['platform']}\n模板：{case['template']}\n\n{storyboard_text(sb)}")
    try:
        return parse_judge(brief_mod.chat_json(JUDGE_SYSTEM, user, temperature=0.0, max_tokens=1500))
    except Exception as e:
        return {"error": str(e)[:200]}

def run_case(case: dict, use_judge: bool = False, self_review: bool = False, skill: bool = True) -> dict:
    brief = case["brief"]
    playbook = cp.resolve(brief)
    skill_text = SKILL_PATH.read_text(encoding="utf-8") if skill and SKILL_PATH.exists() else ""
    t0 = time.time()
    try:
        sb = l1.generate_storyboard(
            product_name=brief["name"], usp=brief_mod.lines_of(brief.get("usp")),
            brand_color=brief.get("brand_color", "#000000"),
            story_template=case["template"], target_platform=case["platform"],
            agent_skill_content=skill_text, product_context=brief_mod.brief_context(brief),
            playbook=playbook, self_review=self_review,
        )
    except Exception as e:
        return {"id": case["id"], "error": str(e)[:300], "seconds": round(time.time() - t0, 1)}
    issues = sb.get("quality_issues") or []
    result = {
        "id": case["id"], "playbook": playbook, "seconds": round(time.time() - t0, 1),
        "errors": sum(i.startswith(qc.ERR) for i in issues),
        "warnings": sum(i.startswith(qc.WARN) for i in issues),
        "issues": issues, "self_reviewed": sb["meta"].get("self_reviewed"),
        "storyboard": storyboard_text(sb),
    }
    if use_judge:
        result["judge"] = judge(case, sb)
    return result

def run_ecom_case(case: dict, self_review: bool = False) -> dict:
    """
    主图 / 详情页用例：生成图文策划（1 次 LLM 调用，不生图），检查定位是否按渠道生效、
    高端定位的主图没有价格、类目标准包的屏序，再加硬校验的 ❌ / ⚠️ 计数。
    素材库按空库处理，结果与本机上传的照片无关
    """
    import ecom_images as ecom        # 只在跑图文用例时导入
    brief = case["brief"]
    playbook = cp.resolve(brief)
    t0 = time.time()
    try:
        plan = ecom.generate_ecom_plan(
            brief["name"], brief_mod.lines_of(brief.get("usp")), brief.get("brand_color", "#000000"),
            case["platform"], main_count=5, product_context=brief_mod.brief_context(brief),
            playbook=playbook, self_review=self_review, library=[], positioning=case.get("positioning", ""))
    except Exception as e:
        return {"id": case["id"], "error": str(e)[:300], "seconds": round(time.time() - t0, 1)}
    issues = list(plan.get("quality_issues") or [])
    position = plan["meta"]["positioning"]
    if case.get("expect_positioning") and position != case["expect_positioning"]:
        issues.insert(0, f"{qc.ERR} 定位是「{position}」，期望「{case['expect_positioning']}」")
    if position == "premium" and any(m.get("price") for m in plan["main_images"]):
        issues.insert(0, f"{qc.ERR} 高端定位的主图出现了价格")
    expected = (cp.get(playbook).get("positioning_plans") or {}).get(position)
    if expected and [d["role"] for d in plan["detail_sections"]] != expected:
        issues.insert(0, f"{qc.ERR} 详情页屏序与类目标准包不一致")
    return {
        "id": case["id"], "playbook": playbook, "positioning": position, "seconds": round(time.time() - t0, 1),
        "errors": sum(i.startswith(qc.ERR) for i in issues), "warnings": sum(i.startswith(qc.WARN) for i in issues),
        "issues": issues, "self_reviewed": plan["meta"].get("self_reviewed"),
        "plan": [f"{it['id']} {it['role'][:16]}｜{it.get('headline') or it.get('title', '')}"
                 for it in plan["main_images"] + plan["detail_sections"]],
    }




def summarize(results: list[dict]) -> str:
    rows = [f"{'用例':<20}{'类目手册':<10}{'❌':>4}{'⚠️':>4}{'评分':>6}{'用时':>8}"]
    for r in results:
        if "error" in r:
            rows.append(f"{r['id']:<20}生成失败：{r['error'][:60]}")
            continue
        avg = (r.get("judge") or {}).get("avg", "-")
        rows.append(f"{r['id']:<20}{cp.label(r['playbook'])[:6]:<10}{r['errors']:>4}{r['warnings']:>4}"
                    f"{avg!s:>6}{r['seconds']:>7}s")
    ok = [r for r in results if "error" not in r]
    judged = [r["judge"]["avg"] for r in ok if "avg" in (r.get("judge") or {})]
    rows.append(f"合计：{len(ok)}/{len(results)} 条成功，❌ {sum(r['errors'] for r in ok)}，"
                f"⚠️ {sum(r['warnings'] for r in ok)}"
                + (f"，平均评分 {sum(judged) / len(judged):.2f}" if judged else ""))
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="剧本生成评测")
    ap.add_argument("--case", action="append", help="只跑指定用例 id，可重复")
    ap.add_argument("--judge", action="store_true", help="加 LLM 评分（每条用例多 1 次调用）")
    ap.add_argument("--self-review", action="store_true", help="生成时开启模型自检")
    ap.add_argument("--no-skill", action="store_true", help="不注入 short-video-selling 技能（对比技能的作用）")
    ap.add_argument("--ecom", action="store_true", help="跑主图 / 详情页用例（ecom_cases，每条 1 次 LLM 调用，不生图）")

    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    cases = [c for c in load_cases(key="ecom_cases" if args.ecom else "cases")
             if not args.case or c["id"] in args.case]
    if not cases:
        sys.exit(f"没有匹配的用例：{args.case}")
    results = []
    for c in cases:
        print(f"▶ {c['id']} ...", flush=True)
        results.append(run_ecom_case(c, args.self_review) if args.ecom
                       else run_case(c, args.judge, args.self_review, skill=not args.no_skill))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"args": vars(args), "results": results}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("\n" + summarize(results) + f"\n\n详细结果：{out}")


if __name__ == "__main__":
    main()