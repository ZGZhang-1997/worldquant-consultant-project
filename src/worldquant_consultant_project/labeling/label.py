"""Labeling orchestration: prompt the model, validate, persist."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from tqdm import tqdm

from . import storage
from .client import DeepSeekClient, DeepSeekError
from .config import LABEL_DIMENSIONS, FieldInput
from .prompt import build_retry_prompt, build_user_prompt

logger = logging.getLogger(__name__)

MAX_VALIDATION_RETRIES = 1
MAX_INVALID_RAW_CHARS = 2000


@dataclass
class LabelConfig:
    model: str
    prompt_sha: str
    workers: int = 8
    thinking: bool = False
    batch_size: int = 50


@dataclass
class LabelStats:
    requested: int = 0
    ok: int = 0
    invalid: int = 0
    error: int = 0
    rows_updated: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)


class ValidationError(ValueError):
    """Model output did not match the required schema."""


def parse_labels(content: str) -> dict[str, str]:
    """Extract and validate the five labels from a model response."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("输出必须是 JSON 对象")

    expected = set(LABEL_DIMENSIONS)
    got = set(payload)
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        raise ValidationError(f"键不匹配，缺少 {missing}，多余 {extra}")

    labels: dict[str, str] = {}
    for dimension, allowed in LABEL_DIMENSIONS.items():
        value = payload[dimension]
        if not isinstance(value, str) or value not in allowed:
            raise ValidationError(f"{dimension} 取值非法: {value!r}，可选 {list(allowed)}")
        labels[dimension] = value
    return labels


def label_field(
    client: DeepSeekClient,
    field: FieldInput,
    *,
    system_prompt: str,
    config: LabelConfig,
) -> dict[str, Any]:
    """Label one field, retrying once when the output violates the schema."""
    user_prompt = build_user_prompt(field)
    record: dict[str, Any] = {
        "dataset_id": field.dataset_id,
        "field_id": field.field_id,
        "attempts": 0,
        "status": "error",
        "error": None,
        "raw_json": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    for dimension in LABEL_DIMENSIONS:
        record[dimension] = None

    prompt = user_prompt
    last_problem = ""
    last_raw = ""
    for attempt in range(MAX_VALIDATION_RETRIES + 1):
        record["attempts"] = attempt + 1
        try:
            completion = client.complete(
                model=config.model,
                system_prompt=system_prompt,
                user_prompt=prompt,
                thinking=config.thinking,
            )
        except DeepSeekError as exc:
            record["status"] = "error"
            record["error"] = str(exc)[:1000]
            return record

        record["prompt_tokens"] += completion.prompt_tokens or 0
        record["completion_tokens"] += completion.completion_tokens or 0
        record["raw_json"] = json.dumps(completion.raw, ensure_ascii=False)[:MAX_INVALID_RAW_CHARS]

        try:
            labels = parse_labels(completion.content)
        except ValidationError as exc:
            last_problem = str(exc)
            last_raw = (completion.content or "")[:500]
            logger.warning("%s/%s invalid attempt %d: %s", field.dataset_id, field.field_id, attempt + 1, exc)
            prompt = build_retry_prompt(field, last_raw, last_problem)
            continue

        record.update(labels)
        record["status"] = "ok"
        record["error"] = None
        return record

    record["status"] = "invalid"
    record["error"] = f"{last_problem} | last output: {last_raw}"[:1000]
    return record


def run_labeling(
    client: DeepSeekClient,
    connection: sqlite3.Connection,
    fields: Sequence[FieldInput],
    *,
    system_prompt: str,
    config: LabelConfig,
    show_progress: bool = True,
) -> LabelStats:
    stats = LabelStats(requested=len(fields))
    if not fields:
        return stats

    buffer: list[dict[str, Any]] = []
    progress = tqdm(
        total=len(fields), desc=config.model, unit="field", disable=not show_progress
    )
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        futures = {
            pool.submit(
                label_field, client, f, system_prompt=system_prompt, config=config
            ): f
            for f in fields
        }
        for future in as_completed(futures):
            field = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad field must not stop the run
                logger.exception("unexpected failure on %s/%s", field.dataset_id, field.field_id)
                record = {
                    "dataset_id": field.dataset_id,
                    "field_id": field.field_id,
                    "attempts": 0,
                    "status": "error",
                    "error": str(exc)[:1000],
                    "raw_json": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    **{d: None for d in LABEL_DIMENSIONS},
                }

            buffer.append(record)
            stats.prompt_tokens += record.get("prompt_tokens") or 0
            stats.completion_tokens += record.get("completion_tokens") or 0
            if record["status"] == "ok":
                stats.ok += 1
            elif record["status"] == "invalid":
                stats.invalid += 1
            else:
                stats.error += 1
            if record["status"] != "ok":
                stats.failures.append((record["dataset_id"], record["field_id"], record["status"]))

            if len(buffer) >= config.batch_size:
                stats.rows_updated += storage.write_labels(
                    connection,
                    model=config.model,
                    prompt_sha=config.prompt_sha,
                    results=buffer,
                )
                buffer.clear()
            progress.update(1)
            elapsed = time.monotonic() - started
            rate = progress.n / elapsed if elapsed > 0 else 0
            progress.set_postfix_str(f"{rate:.1f} f/s ok={stats.ok}", refresh=False)
    progress.close()

    if buffer:
        stats.rows_updated += storage.write_labels(
            connection, model=config.model, prompt_sha=config.prompt_sha, results=buffer
        )
    return stats


def preview_prompt(fields: Iterable[FieldInput]) -> dict[str, Any]:
    fields = list(fields)
    if not fields:
        return {}
    first = fields[0]
    return {
        "field": f"{first.dataset_id}/{first.field_id}",
        "pending": len(fields),
        "user_prompt": build_user_prompt(first),
    }
