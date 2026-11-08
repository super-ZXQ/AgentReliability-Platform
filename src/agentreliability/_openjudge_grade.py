"""在 OpenJudge 独立环境中执行语义评分。

由 OpenJudge checkout 的虚拟环境解释器启动。`offline` 模式使用词面重叠的
确定性测试替身模型，仅用于验证适配器与计分链路；`model` 模式使用
OpenAI 兼容的真实模型配置。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_TOKEN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|\d+(?:\.\d+)?")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "than",
    "while", "because", "which", "have", "has", "was", "were", "are", "not",
    "but", "its", "their", "there", "also", "does", "did", "can", "may",
    "likely", "about", "according", "observed", "show", "shows", "showed",
}


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN.findall(text or "")
        if token.lower() not in _STOP
    }


def _extract_turn(prompt: str) -> tuple[str, str]:
    observation = ""
    reasoning = ""
    for line in prompt.splitlines():
        stripped = line.strip()
        for prefix in ("Observation:", "观察：", "观察:"):
            if stripped.startswith(prefix):
                observation = stripped[len(prefix):].strip()
        for prefix in ("Reasoning:", "推理：", "推理:"):
            if stripped.startswith(prefix):
                reasoning = stripped[len(prefix):].strip()
    return observation, reasoning


class LexicalGroundednessModel:
    """离线词面覆盖测试替身；不是语义模型，输出需标注 test-double。"""

    model = "lexical-overlap-test-double"
    stream = False

    async def achat(self, messages, structured_model=None, callback=None, **kwargs):
        from openjudge.models.schema.oai.response import ChatResponse

        prompt = ""
        for message in messages or []:
            role = getattr(message, "role", None) or (
                message.get("role") if isinstance(message, dict) else None
            )
            content = getattr(message, "content", None) or (
                message.get("content") if isinstance(message, dict) else None
            )
            if role == "user" and isinstance(content, str):
                prompt = content
        observation, reasoning = _extract_turn(prompt)
        obs = _tokens(observation)
        claim = _tokens(reasoning)
        ratio = len(claim & obs) / len(claim) if claim else 0.0
        score = 1.0 if ratio >= 0.5 else 0.0
        reason = (
            f"offline lexical baseline: {len(claim & obs)}/{len(claim)} claim terms "
            f"appear in the observation (ratio={ratio:.2f}, threshold=0.50)"
        )
        parsed = {"reason": reason, "score": score}
        return ChatResponse(role="assistant", content=json.dumps(parsed), parsed=parsed)


def _real_model():
    from openjudge.models.openai_chat_model import OpenAIChatModel

    api_key = os.environ.get("OPENJUDGE_API_KEY") or os.environ.get("LLM_API_KEY")
    model = os.environ.get("OPENJUDGE_MODEL") or os.environ.get("LLM_MODEL")
    base_url = os.environ.get("OPENJUDGE_BASE_URL") or os.environ.get("LLM_BASE_URL")
    if not api_key or not model:
        raise RuntimeError("model configuration missing")
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAIChatModel(
        model=model,
        api_key=api_key,
        stream=False,
        temperature=0.1,
        **kwargs,
    )


async def grade(samples: list[dict], model) -> list[dict]:
    from openjudge.graders.agent.reasoning.reasoning_groundedness import (
        ReasoningGroundednessGrader,
    )
    from openjudge.models.schema.prompt_template import LanguageEnum

    grader = ReasoningGroundednessGrader(model=model, language=LanguageEnum.EN)
    results = []
    for sample in samples:
        item = {
            "id": sample["id"],
            "split": sample["split"],
            "label": sample["label"],
            "preferred_label": sample["preferred_label"],
        }
        try:
            score = await grader.aevaluate(
                observation=sample["observation"], reasoning=sample["diagnosis"]
            )
            item["raw_score"] = score.score
            item["score"] = 1.0 if score.score >= 0.5 else 0.0
            item["reason"] = score.reason
            item["predicted"] = "supported" if item["score"] >= 0.5 else "unsupported"
        except Exception as exc:  # noqa: BLE001 - 逐样本失败需保留
            item["score"] = None
            item["predicted"] = None
            item["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        results.append(item)
    return results


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: _openjudge_grade.py <mode> <samples.json> <output.json>", file=sys.stderr)
        return 2
    mode, samples_path, output_path = argv
    samples = json.loads(Path(samples_path).read_text(encoding="utf-8"))
    payload: dict = {"status": "ERROR", "mode": mode, "grader": "reasoning_groundedness"}
    try:
        if mode == "offline":
            model = LexicalGroundednessModel()
            payload["model"] = model.model
        else:
            model = _real_model()
            payload["model"] = model.model
            payload["model_config_present"] = True
        payload["predictions"] = asyncio.run(grade(samples, model))
        payload["status"] = "PASS"
        payload["execution_mode"] = "test-double" if mode == "offline" else "real-service"
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
