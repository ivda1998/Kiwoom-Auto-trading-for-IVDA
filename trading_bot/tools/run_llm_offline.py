# -*- coding: utf-8 -*-
"""LLM 에이전트 오프라인 평가 — 벤치 거래 진입 시점마다 LLM이 사이즈 배수를 정한다.

  python tools/run_llm_offline.py build              # 스냅샷 생성(API 호출 없음) + 토큰 추정
  python tools/run_llm_offline.py submit             # 배치 제출(50% 가격) — ANTHROPIC_API_KEY 필요
  python tools/run_llm_offline.py collect            # 결과 수거 → llm_results.jsonl
  python tools/run_llm_offline.py eval               # agent_lab과 같은 정규화 paired-t로 평가
  python tools/run_llm_offline.py consistency        # 10% 표본 재제출(일관성 확인), 이후 collect/eval

누설 방지: 날짜·절대가격 제거, 리포트/메시지 속 날짜 표기 마스킹. 그래도 모델 학습기간(~2026-06)과
겹치는 구간은 참고용 — 주 판정은 진입일 2026-07-01 이후(clean).
"""
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_lab as al          # noqa: E402
import agent_context as ac      # noqa: E402

MODEL = ac.LLM_MODEL
SNAP = os.path.join(al.BASE, "llm_snapshots.jsonl")
STATE = os.path.join(al.BASE, "llm_batches.json")
RESULTS = os.path.join(al.BASE, "llm_results.jsonl")

SYSTEM, SCHEMA, scrub = ac.SYSTEM, ac.SCHEMA, ac.scrub


def snapshot(ctx, cb, trade):
    d, et = trade["ed"], trade["et"]
    i0, ie = ctx.day_first[d], trade["ei"]
    pc = ctx.prev_close.get(d) or ctx.bars[i0]["open"]
    path = [(b["t"], ((b["close"] / pc) - 1) * 100) for b in ctx.bars[i0:ie + 1]]
    return ac.format_snapshot(trade["dir"], et, ac.regime(ctx.prior_closes(d)),
                              ctx.prior_closes(d)[-6:], path, cb.build(d))


def cmd_build():
    ctx, trades, cb = al.load("index")
    n = 0
    with open(SNAP, "w", encoding="utf-8") as f:
        for k, t in enumerate(trades):
            f.write(json.dumps({"id": f"t{k:04d}", "ed": t["ed"], "dir": t["dir"], "ret": t["ret"],
                                "text": snapshot(ctx, cb, t)}, ensure_ascii=False) + "\n")
            n += 1
    sizes = [len(json.loads(x)["text"]) for x in open(SNAP, encoding="utf-8")]
    # 한국어는 대략 1.0~1.5자/토큰 — 보수적으로 1자=1토큰
    est_in = sum(sizes) + n * len(SYSTEM)
    print(f"스냅샷 {n}건 → {SNAP}")
    print(f"평균 {sum(sizes) // n}자, 최대 {max(sizes)}자. 입력 추정 ≤ {est_in / 1e6:.2f}M 토큰"
          f" → 배치가($1/M 입력) 약 ${est_in / 1e6 * 1.0:.2f} + 출력 소액")


def _client():
    import anthropic
    return anthropic.Anthropic()


def _request(rec):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(custom_id=rec["id"], params=MessageCreateParamsNonStreaming(
        model=MODEL, max_tokens=8000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": rec["text"]}]))


def _submit(recs, tag):
    batch = _client().messages.batches.create(requests=[_request(r) for r in recs])
    st = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {"batches": []}
    st["batches"].append({"id": batch.id, "tag": tag, "n": len(recs), "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"배치 제출: {batch.id} ({tag}, {len(recs)}건) 상태={batch.processing_status}")


def cmd_submit():
    recs = [json.loads(x) for x in open(SNAP, encoding="utf-8")]
    _submit(recs, "main")


def cmd_consistency():
    recs = [json.loads(x) for x in open(SNAP, encoding="utf-8")]
    random.Random(7).shuffle(recs)
    _submit(recs[: max(10, len(recs) // 10)], "repeat")


def cmd_collect():
    st = json.load(open(STATE, encoding="utf-8"))
    client = _client()
    have = {}
    if os.path.exists(RESULTS):
        for x in open(RESULTS, encoding="utf-8"):
            r = json.loads(x)
            have[(r["batch"], r["id"])] = r
    pending = 0
    for b in st["batches"]:
        info = client.messages.batches.retrieve(b["id"])
        if info.processing_status != "ended":
            pending += 1
            print(f"{b['id']} ({b['tag']}) 진행 중: {info.request_counts}")
            continue
        for res in client.messages.batches.results(b["id"]):
            key = (b["id"], res.custom_id)
            if key in have:
                continue
            row = {"batch": b["id"], "tag": b["tag"], "id": res.custom_id, "type": res.result.type}
            if res.result.type == "succeeded":
                msg = res.result.message
                row["stop"] = msg.stop_reason
                txt = next((blk.text for blk in msg.content if blk.type == "text"), "")
                try:
                    j = json.loads(txt)
                    row["size"], row["reason"] = float(j["size"]), j["reason"]
                except (ValueError, KeyError):
                    row["size"], row["reason"] = None, txt[:200]
                u = msg.usage
                row["usage"] = {"in": u.input_tokens, "out": u.output_tokens,
                                "cache_r": getattr(u, "cache_read_input_tokens", 0) or 0}
            have[key] = row
    with open(RESULTS, "w", encoding="utf-8") as f:
        for r in have.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok = sum(1 for r in have.values() if r.get("size") is not None)
    print(f"수거 {len(have)}건(유효 {ok}), 진행 중 배치 {pending}")


def cmd_eval():
    snaps = [json.loads(x) for x in open(SNAP, encoding="utf-8")]
    res = [json.loads(x) for x in open(RESULTS, encoding="utf-8")]
    main = {r["id"]: r for r in res if r["tag"] == "main"}
    _, trades, _ = al.load("index")
    assert len(trades) == len(snaps), "스냅샷 이후 벤치가 바뀜 — build부터 다시"
    # 거부·파싱실패는 1.0(개입 안 함)으로
    weights = [(main.get(s["id"], {}).get("size") if main.get(s["id"], {}).get("size") is not None else 1.0) for s in snaps]
    bad = sum(1 for s in snaps if main.get(s["id"], {}).get("size") is None)
    out = {w: al.evaluate(trades, weights, w) for w in al.WINDOWS}
    rows = {"LLM_sonnet": {"desc": f"{MODEL} 사이즈 판단 (결측·거부 {bad}건=1.0)", "main": "clean",
                           "verdict": al.verdict(out, "clean"), "res": out}}
    dist = {}
    for w_ in weights:
        dist[w_] = dist.get(w_, 0) + 1
    print("배수 분포:", dict(sorted(dist.items())))
    al.print_rows(rows)
    # 배수별 실제 성과(거꾸로 읽는 모델인지 확인)
    for v in sorted(dist):
        ts = [t for t, w_ in zip(trades, weights) if w_ == v]
        if ts:
            s = al.lab.trade_stats(ts)
            print(f"  배수 {v}: {s['n']}건 건당 {s['net_ret'] / s['n'] * 100:+.2f}% 승률 {s['win_rate'] * 100:.0f}%")
    rep = [r for r in res if r["tag"] == "repeat" and r.get("size") is not None]
    if rep:
        same = sum(1 for r in rep if main.get(r["id"], {}).get("size") == r["size"])
        print(f"일관성: 재호출 {len(rep)}건 중 같은 배수 {same}건 ({same / len(rep) * 100:.0f}%)")
    u = [r["usage"] for r in res if r.get("usage")]
    if u:
        print(f"토큰: 입력 {sum(x['in'] for x in u):,} (캐시읽기 {sum(x['cache_r'] for x in u):,}) / 출력 {sum(x['out'] for x in u):,}")
    al.ledger_append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "result", "universe": "index",
                      "n_bench": len(trades), "rows": rows, "dist": {str(k): v for k, v in dist.items()}})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmds = {"build": cmd_build, "submit": cmd_submit, "collect": cmd_collect, "eval": cmd_eval,
            "consistency": cmd_consistency}
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    if c not in cmds:
        print(__doc__)
    else:
        cmds[c]()
