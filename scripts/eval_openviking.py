"""Measure synthetic A/B/C memory retrieval using the real Lumen context preparation path.

Run with uv run python scripts/eval_openviking.py --output result.json.
Add --endpoint ... --embedding-model ... for C against an isolated OpenViking 0.5.6
deployment. --answer-url and --answer-model optionally score real final answers.
All network inputs are generated here; no user memories or sessions are read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic_ai.models.test import TestModel

from lumen.config import ContextConfig, ModelContextOverride, ModelSettingsConfig
from lumen.context import (
    AgentRef,
    ContextEngine,
    ContextRequest,
    ContextZone,
    RuntimeContextSnapshot,
    SessionRef,
    TaskSnapshot,
)
from lumen.context.memory import MemoryManager, SQLiteMemoryRepository
from lumen.context.memory.openviking import OpenVikingService
from lumen.context.memory.projection import render_memory_index
from lumen.context.memory.records import MemoryScope, Sensitivity
from lumen.plan import PlanState

# Statement, answer, Chinese lexical alternative, English paraphrase. Each row
# also gets an exact identifier probe and an unrelated, deliberately unanswerable probe.
FACTS = [
    ("线上变更先放给", "内部账号", "发布的第一批受众是谁", "Who gets the rollout first"),
    ("账单只开给", "财务组", "谁能收到付款凭证", "Who receives invoices"),
    ("流水线失败通知", "值班员", "构建红了应该找谁", "Who handles a failed build"),
    ("故障报告放在", "事件库", "事故经过保存到哪里", "Where do we keep incident reports"),
    ("生产缓存的有效期为", "四小时", "线上缓存多久更新一次", "How long is cached data valid"),
    ("历史日志保存在", "冷存储", "哪里能找老的运行记录", "Where are archived logs stored"),
    ("发布冻结日是", "星期五", "哪一天不能上线", "On which day are deployments frozen"),
    ("设计稿交给", "交互组", "谁接收界面原型", "Who owns interface drafts"),
    ("夜间告警使用", "短信", "半夜出事怎么提醒", "How are overnight alerts delivered"),
    ("缺陷回归由", "质量组", "谁验证修复的问题", "Who retests bug fixes"),
    ("交付文档采用", "中文", "给客户的说明用哪种语言", "Which language is used for handover"),
    ("扫描报告保存", "三十天", "安全扫描结果留多久", "How long do scan reports stay"),
    ("会议纪要交给", "项目秘书", "谁归档例会记录", "Who archives meeting minutes"),
    ("每批数据上限是", "百条", "一次最多导入多少项", "What is the batch size limit"),
    ("只读副本位于", "上海", "读请求打到哪个城市", "Where is the read replica"),
    ("内部版本命名用", "星座", "测试迭代用哪类名字", "How are internal builds named"),
    ("采购单由", "行政组", "谁处理订货申请", "Who processes purchase orders"),
    ("离线备份放在", "磁带库", "哪里存放断网副本", "Where are offline backups kept"),
    ("失败任务重试", "三次", "作业报错最多再跑几遍", "How often are failed jobs retried"),
    ("数据库维护窗口是", "凌晨两点", "几点可以更新数据库", "When is database maintenance"),
    ("发票币种为", "人民币", "结算凭据用什么货币", "Which currency is used for invoices"),
    ("紧急联系渠道是", "电话", "遇到紧急情况怎么联络", "How do we contact people urgently"),
    ("归档文件格式为", "压缩包", "保存历史文件时要转成什么", "Which format is used for archives"),
    ("客户培训安排在", "星期二", "哪天给客户上课", "On which day is customer training"),
    ("演示环境部署到", "测试集群", "产品展示在哪套机器运行", "Where are product demos deployed"),
]


class LegacyEngine(ContextEngine):
    """eca45ba recall excluded every eligible index ID before the assembler cropped its text."""

    async def _memory_context(self, prompt: str, turn: object) -> tuple[str, str, frozenset[str]]:
        assert self.memory is not None
        records = self.memory.index()
        hits = self.memory.recall(prompt, exclude_ids={r.id for r in records}, mark_used=False)
        recalled = "\n".join(
            f"- [{r.id}] ({r.scope.value}, {r.source_kind.value}, confidence={r.confidence:.2f}) {r.content}"
            for r in hits
        )
        return render_memory_index(records), recalled, frozenset(r.id for r in hits)


async def ignore_event(_event: object) -> None:
    pass


def probes(facts: list[tuple[str, str, str, str]]):
    for i, (_, answer, zh, en) in enumerate(facts):
        yield i, "paraphrase_zh", zh + "?", answer
        yield i, "paraphrase_en", en + "?", answer
        yield i, "identifier", f"POLICY_{i:03d}", answer
        yield i, "no_answer", f"缺少资料的资产 UNLISTED_{i:03d} 的采购金额是多少?", None


async def settle(service: OpenVikingService, *, purging: bool, seconds: float) -> None:
    async with asyncio.timeout(seconds):
        while True:
            status = await service.control("status")
            if status.get("last_error") in {
                "authorization_failed",
                "identity_changed",
                "unsupported_service_identity",
            }:
                raise RuntimeError(f"Projection failed: {status['last_error']}")
            if status.get("pending") == 0 and (not purging or status.get("cleanup") == 0):
                return
            await asyncio.sleep(0.2)


def pct(values: list[float], q: float) -> float:
    return round(sorted(values)[round((len(values) - 1) * q)], 4)


async def answer(args: argparse.Namespace, query: str, context: str) -> tuple[str, dict[str, Any], float]:
    key = os.environ.get(args.answer_key_env, "")
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        start = time.perf_counter()
        async with client.stream(
            "POST",
            args.answer_url,
            headers={"Authorization": f"Bearer {key}"} if key else {},
            json={
                "model": args.answer_model,
                "temperature": 0,
                "max_tokens": 20,
                "messages": [
                    {
                        "role": "system",
                        "content": "只根据资料回答。仅返回简短答案原词。资料没有答案时只回答 UNKNOWN。",
                    },
                    {"role": "user", "content": f"资料\n{context}\n问题\n{query}"},
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            response.raise_for_status()
            parts: list[str] = []
            usage: dict[str, Any] = {}
            first_token = None
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    data = json.loads(line[6:])
                    usage.update(data.get("usage") or {})
                    for choice in data.get("choices", []):
                        token = choice.get("delta", {}).get("content")
                        if token:
                            if first_token is None:
                                first_token = time.perf_counter()
                            parts.append(token)
            if first_token is None:
                raise RuntimeError("Answer model returned no content tokens")
            return "".join(parts).strip(), usage, first_token - start


async def evaluate(args: argparse.Namespace, variant: str, state: Path, size: str) -> list[dict[str, Any]]:
    facts = FACTS[:3] if size == "small" else FACTS
    repo = SQLiteMemoryRepository(state / f"{variant}-{size}.sqlite3")
    service = (
        OpenVikingService(
            repo, endpoint=args.endpoint, api_key=os.environ[args.api_key_env], project_id="synthetic"
        )
        if variant == "C"
        else None
    )
    memory = MemoryManager(repo, project_id="synthetic", semantic=service)
    cls = LegacyEngine if variant == "A" else ContextEngine
    engine = cls(
        ContextConfig(enabled=False),
        TestModel(),
        memory=memory,
        model_config=ModelSettingsConfig(
            id="test:pilot",
            context=ModelContextOverride(window_tokens=2048, max_output_tokens=192),
        ),
    )
    rows: list[dict[str, Any]] = []
    try:
        if service:
            await service.control("enable")
        ids: list[str] = []
        superseded = "原定全量同时上线"
        for i, (statement, value, _, _) in enumerate(facts):
            fact = f"POLICY_{i:03d} {statement}{value}。"
            if i == 0:
                existing = memory.remember(superseded)
                record = memory.edit_content(existing.id, fact)
            else:
                record = memory.remember(fact)
            ids.append(record.id)
        hidden = memory.remember("试点中受限的个人记录。", sensitivity=Sensitivity.RESTRICTED)
        stale = memory.remember("旧流程使用旧渠道。")
        memory.forget(stale.id)
        expired = memory.remember("过期的试点里程碑。")
        repo.expire(expired.id)
        other = MemoryManager(repo, project_id="another-project").remember(
            "另一项目使用另一个负责人。", scope=MemoryScope.PROJECT
        )
        if service:
            await settle(service, purging=False, seconds=args.sync_timeout)
            async with httpx.AsyncClient(timeout=args.sync_timeout, trust_env=False) as client:
                wait = await client.post(
                    args.endpoint.rstrip("/") + "/api/v1/system/wait",
                    headers={"X-API-Key": os.environ[args.api_key_env]},
                    json={},
                )
                wait.raise_for_status()
                if wait.json().get("status") != "ok":
                    raise RuntimeError("OpenViking index queue did not settle")
        for n, (i, group, query, expected) in enumerate(probes(facts)):
            request = ContextRequest(
                SessionRef(str(n)),
                AgentRef("pilot"),
                query,
                TaskSnapshot(PlanState()),
                RuntimeContextSnapshot("answer from confirmed facts"),
                (),
                memory_query=query,
            )
            start = time.perf_counter()
            envelope = await engine.prepare(request, ignore_event)
            duration = 1000 * (time.perf_counter() - start)
            blocks = [
                b
                for b in envelope.blocks
                if b.zone in {ContextZone.MEMORY_INDEX, ContextZone.RECALLED_MEMORY}
            ]
            context = "\n".join(b.payload.text or "" for b in blocks)
            recall = "\n".join(b.payload.text or "" for b in blocks if b.zone == ContextZone.RECALLED_MEMORY)
            index = "\n".join(b.payload.text or "" for b in blocks if b.zone == ContextZone.MEMORY_INDEX)
            recalled = re.findall(r"\[(mem-[0-9a-f]+)\]", recall)[:5]
            if superseded in context or any(r.id in context for r in (hidden, stale, expired, other)):
                raise AssertionError("Private, forgotten or unrelated record entered prepared context")
            row: dict[str, Any] = {
                "id": f"{size}/{i}/{group}",
                "variant": variant,
                "size": size,
                "group": group,
                "elapsed_ms": round(duration, 2),
                "tokens": sum(b.token_estimate for b in blocks),
                "recall_at_5": None if expected is None else ids[i] in recalled,
                "target_visible": None if expected is None else ids[i] in context and expected in context,
                "in_index": None if expected is None else ids[i] in index,
                "dynamic_false_positive": bool(recalled) if expected is None else None,
            }
            if args.answer_url and i % args.answer_stride == 0:
                text, usage, elapsed = await answer(args, query, context)
                row.update(
                    answer_correct=text == (expected or "UNKNOWN"),
                    usage=usage,
                    first_token_ms=duration + elapsed * 1000,
                )
            rows.append(row)
            engine.discard_unpersisted(str(n), envelope.fingerprint)
    finally:
        try:
            if service:
                await service.control("purge")
                await settle(service, purging=True, seconds=args.sync_timeout)
        finally:
            await engine.close()
            await memory.close()
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for v, size, group in sorted({(r["variant"], r["size"], r["group"]) for r in rows}):
        sample = [r for r in rows if (r["variant"], r["size"], r["group"]) == (v, size, group)]
        result = {"count": len(sample), "prepare_p95_ms": pct([r["elapsed_ms"] for r in sample], 0.95)}
        for metric in (
            "recall_at_5",
            "target_visible",
            "in_index",
            "dynamic_false_positive",
            "answer_correct",
        ):
            measured = [r[metric] for r in sample if r.get(metric) is not None]
            if measured:
                result[metric] = sum(measured) / len(measured)
        answer_times = [r["first_token_ms"] for r in sample if "first_token_ms" in r]
        if answer_times:
            result["answers"] = len(answer_times)
            result["first_token_p95_ms"] = pct(answer_times, 0.95)
        summary[f"{v}/{size}/{group}"] = result
    return summary


def paired(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_variant = {(r["variant"], r["id"]): r for r in rows}
    rng = random.Random(42)
    output: dict[str, dict[str, Any]] = {}
    for size, group in sorted({(r["size"], r["group"]) for r in rows}):
        for metric in ("target_visible", "recall_at_5", "dynamic_false_positive", "answer_correct"):
            delta = [
                int(r[metric]) - int(by_variant["B", r["id"]][metric])
                for r in rows
                if r["variant"] == "C"
                and r["size"] == size
                and r["group"] == group
                and r.get(metric) is not None
            ]
            if delta:
                bootstrap = [statistics.mean(rng.choices(delta, k=len(delta))) for _ in range(2000)]
                output[f"{size}/{group}/{metric}"] = {
                    "paired_count": len(delta),
                    "mean_delta": statistics.mean(delta),
                    "bootstrap_95": [pct(bootstrap, 0.025), pct(bootstrap, 0.975)],
                }
    return output


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--endpoint", help="Isolated v0.5.6 server for variant C")
    parser.add_argument("--embedding-model", help="Deployed embedding model/version for evidence")
    parser.add_argument("--api-key-env", default="LUMEN_OPENVIKING_API_KEY")
    parser.add_argument("--answer-url", help="Explicit OpenAI-compatible chat completions URL for final QA")
    parser.add_argument("--answer-model")
    parser.add_argument(
        "--answer-stride",
        type=int,
        default=1,
        help="Score every Nth fact in each set; retrieval still covers all probes",
    )
    parser.add_argument("--answer-key-env", default="LUMEN_PILOT_ANSWER_KEY")
    parser.add_argument("--sync-timeout", type=float, default=600)
    args = parser.parse_args()
    if args.answer_stride <= 0:
        parser.error("--answer-stride must be positive")
    if args.endpoint and (not args.embedding_model or not os.environ.get(args.api_key_env)):
        parser.error("C requires --embedding-model and a tenant key in --api-key-env")
    if bool(args.answer_url) != bool(args.answer_model):
        parser.error("--answer-url and --answer-model must be supplied together")
    state = Path(tempfile.mkdtemp(prefix="lumen-openviking-eval-"))
    print(f"Synthetic recovery ledger: {state}", flush=True)
    rows: list[dict[str, Any]] = []
    for variant in "ABC" if args.endpoint else "AB":
        for size in ("small", "overflow"):
            print(f"{variant} {size}", flush=True)
            rows.extend(await evaluate(args, variant, state, size))
            args.output.write_text(
                json.dumps(
                    {
                        "service_version": "0.5.6" if args.endpoint else None,
                        "embedding_model": args.embedding_model,
                        "answer_model": args.answer_model,
                        "window_tokens": 2048,
                        "summary": summarize(rows),
                        "paired_c_minus_b": paired(rows),
                        "rows": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
