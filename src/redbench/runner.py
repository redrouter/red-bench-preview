#!/usr/bin/env python3
"""Run a frozen defensive task matrix on explicitly pinned OpenRouter routes."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
PROTOCOL_VERSION = 1
ARMS = {"authentic", "neutral"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
MODEL = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PROVIDER = re.compile(r"[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*){0,3}")
POLICY_CODES = {
    "content_filter", "content_policy_violation", "moderation_blocked", "safety",
    "safety_violation", "blocked_reason_safety",
}
RESPONSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,159}")
MAX_PROMPT_BYTES = 16384
MAX_RESPONSE_BYTES = 1048576
MICRO = Decimal(1000000)
INPUT_OVERHEAD = 256


@dataclass(frozen=True)
class Route:
    id: str
    model: str
    provider: str
    input_rate: Decimal
    output_rate: Decimal
    provider_labels: tuple[str, ...]
    model_aliases: tuple[str, ...]


class RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def json_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def amount(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid decimal amount") from None
    if not number.is_finite() or number < 0:
        raise ValueError("Expected a finite nonnegative decimal amount")
    return number


def load_cases(data, *, expected_arms=ARMS):
    cases = []
    observed = {}
    for line in data.splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        required = {"case_id", "arm", "prompt"}
        if not isinstance(case, dict) or not required <= set(case) <= required | {"prior_context"}:
            raise ValueError("Case rows need case_id, arm, and prompt with optional prior_context")
        case_id, arm, prompt = case["case_id"], case["arm"], case["prompt"]
        if (not isinstance(case_id, str) or not IDENTIFIER.fullmatch(case_id)
                or not isinstance(arm, str) or arm not in expected_arms
                or not isinstance(prompt, str) or not prompt.strip()
                or len(prompt.encode()) > MAX_PROMPT_BYTES):
            raise ValueError("Invalid case ID, arm, or prompt size")
        if "prior_context" in case:
            context = case["prior_context"]
            if (not isinstance(context, str) or not context.strip()
                    or len(context.encode()) + len(prompt.encode()) > MAX_PROMPT_BYTES):
                raise ValueError("Invalid prior context or combined prompt size")
        arms = observed.setdefault(case_id, set())
        if arm in arms:
            raise ValueError("Duplicate arm in approved case set")
        arms.add(arm)
        cases.append(case)
    if not cases or any(arms != expected_arms for arms in observed.values()):
        labels = " and ".join(sorted(expected_arms))
        raise ValueError(f"Each workflow needs these arms: {labels}")
    return cases


def load_routes(data):
    routes = json.loads(data)
    expected = {"id", "model", "provider", "input_usd_per_million", "output_usd_per_million"}
    if not isinstance(routes, list) or not routes:
        raise ValueError("Expected a list of pinned routes")
    parsed = []
    for route in routes:
        optional = {"provider_labels", "model_aliases"}
        if not isinstance(route, dict) or not expected <= route.keys() <= expected | optional:
            raise ValueError("Invalid route fields")
        for key, pattern in (("id", IDENTIFIER), ("model", MODEL), ("provider", PROVIDER)):
            if not isinstance(route[key], str) or not pattern.fullmatch(route[key]):
                raise ValueError(f"Invalid route {key}")
        if route["model"] == "openrouter/auto":
            raise ValueError("Every route requires a fixed model")
        labels = route.get("provider_labels", [])
        if not isinstance(labels, list) or any(not safe_identifier(label) for label in labels):
            raise ValueError("Invalid provider display labels")
        aliases = route.get("model_aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not MODEL.fullmatch(alias)
                                               for alias in aliases):
            raise ValueError("Invalid observed model aliases")
        input_rate = amount(route["input_usd_per_million"])
        output_rate = amount(route["output_usd_per_million"])
        if not input_rate or not output_rate:
            raise ValueError("Use positive published input and output rates")
        parsed.append(Route(route["id"], route["model"], route["provider"],
                            input_rate, output_rate, tuple(labels), tuple(aliases)))
    if len({route.id for route in parsed}) != len(parsed):
        raise ValueError("Duplicate configuration ID")
    return parsed


def request_body(case, route, max_output_tokens):
    messages = []
    if "prior_context" in case:
        messages.extend([
            {"role": "user", "content": case["prior_context"]},
            {"role": "assistant", "content": "I have noted the handoff and am ready to review the records."},
        ])
    messages.append({"role": "user", "content": case["prompt"]})
    return {
        "model": route.model,
        "messages": messages,
        "provider": {
            "only": [route.provider], "allow_fallbacks": False, "require_parameters": True,
            "max_price": {"prompt": float(route.input_rate), "completion": float(route.output_rate)},
        },
        "max_tokens": max_output_tokens,
        "stream": False,
    }


def plan_trials(cases, routes, max_output_tokens, plan_sha256):
    for case in cases:
        for route in routes:
            body = request_body(case, route, max_output_tokens)

            input_bound = len(json_bytes(body)) + INPUT_OVERHEAD
            reserve = (input_bound * route.input_rate + max_output_tokens * route.output_rate) / MICRO
            yield ({"case_id": case["case_id"], "arm": case["arm"], "config_id": route.id,
                    "model": route.model, "provider": route.provider, "plan_sha256": plan_sha256,
                    "reserved_usd": str(reserve), "input_token_reserve": input_bound,
                    "max_output_tokens": max_output_tokens}, body, route)


def safe_identifier(value):
    return value if isinstance(value, str) and RESPONSE_ID.fullmatch(value) else None


def usage_fields(data):
    if not isinstance(data, dict):
        return {}
    usage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = data.get(key)
        if type(value) is int and value >= 0:
            usage[key] = value
    for outer, inner in (("prompt_tokens_details", "cached_tokens"),
                         ("completion_tokens_details", "reasoning_tokens")):
        detail = data.get(outer)
        if isinstance(detail, dict) and type(detail.get(inner)) is int and detail[inner] >= 0:
            usage[outer] = {inner: detail[inner]}
    cost = data.get("cost")
    if type(cost) in (float, int):
        try:
            usage["cost"] = str(amount(cost))
        except ValueError:
            pass
    return usage


def policy_code(value):
    return value.lower() if isinstance(value, str) and value.lower() in POLICY_CODES else None


def describe_error(data, http_status=None):
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    result = {"status": "provider_block" if policy_code(code) else "api_error"}
    if http_status is not None:
        result["http_status"] = http_status
    if (type(code) is int and 100 <= code <= 599) or policy_code(code):
        result["error_code"] = code
    return result


def normalize_provider(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def describe_response(data, route):
    if not isinstance(data, dict):
        return {"status": "invalid_response"}
    usage = usage_fields(data.get("usage"))
    if data.get("error"):
        return {**describe_error(data), "usage": usage}
    requested_labels = {normalize_provider(label) for label in (route.provider, *route.provider_labels)}
    model = safe_identifier(data.get("model"))
    provider = safe_identifier(data.get("provider"))
    record = {"observed_model": model, "observed_provider": provider,
              "generation_id": safe_identifier(data.get("id")), "usage": usage,
              "route_verification": "reported_match" if model and provider else "unreported"}
    if (model and model not in (route.model, *route.model_aliases)
            or provider and normalize_provider(provider) not in requested_labels):
        record.update(status="route_mismatch", route_verification="reported_mismatch")
        return record
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        record["status"] = "invalid_response"
        return record
    choice = choices[0]
    finish = safe_identifier(choice.get("finish_reason"))
    native_finish = safe_identifier(choice.get("native_finish_reason"))
    record.update(finish_reason=finish, native_finish_reason=native_finish)
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
    content, refusal = message.get("content"), message.get("refusal")
    if policy_code(finish) or policy_code(native_finish):
        record["status"] = "provider_block"
    elif choice.get("error"):
        record.update(describe_error(choice))
    elif isinstance(refusal, str) and refusal.strip():
        record.update(status="model_refusal", refusal=refusal)
    elif finish in {"length", "max_tokens"}:
        record["status"] = "truncated"
    elif isinstance(content, str) and content.strip():
        record.update(status="completed", content=content)
    else:
        record["status"] = "invalid_response"
    return record


def read_response(response):
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return None


def openrouter(body, route, key, timeout):
    opener = build_opener(ProxyHandler({}), RefuseRedirect())
    request = Request(ENDPOINT, data=json_bytes(body), method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json", "Authorization": "Bearer " + key,
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            return describe_response(read_response(response), route)
    except HTTPError as error:
        with error:
            return describe_error(read_response(error), error.code)
    except URLError as error:
        return {"status": "timeout" if isinstance(error.reason, TimeoutError) else "transport_error"}
    except TimeoutError:
        return {"status": "timeout"}
    except OSError:
        return {"status": "transport_error"}


def open_private(path):
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("Ledger files must be regular and private (mode 0600)")
    return os.fdopen(fd, "r+b")


def sync_parent(path):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_ledger(handle, *, repair_last_record=False):
    handle.seek(0)
    data = handle.read()
    if data and not data.endswith(b"\n"):
        if not repair_last_record:
            raise ValueError("Incomplete claim ledger, inspect before spending again")
        boundary = data.rfind(b"\n") + 1
        handle.truncate(boundary)
        handle.flush()
        os.fsync(handle.fileno())
        data = data[:boundary]
    return [json.loads(line) for line in data.splitlines()]


def append_row(handle, row):
    handle.write(json_bytes(row) + b"\n")
    handle.flush()
    os.fsync(handle.fileno())


def trial_key(row):
    return row["case_id"], row["arm"], row["config_id"]


def index_ledger(rows, planned):
    indexed = {}
    for row in rows:
        key = trial_key(row)
        fixed = planned.get(key)
        if key in indexed or not fixed or any(row.get(field) != val for field, val in fixed.items()):
            raise ValueError("Ledger does not match immutable plan or has duplicate rows")
        indexed[key] = row
    return indexed


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_trials(trials, results, key, timeout, budget, limit, concurrency, responder=openrouter):
    planned = {trial_key(base): base for base, _, _ in trials}
    claim_path = results.with_name(results.name + ".claims")
    with open_private(claim_path) as claims:
        fcntl.flock(claims, fcntl.LOCK_EX | fcntl.LOCK_NB)
        claimed = index_ledger(read_ledger(claims), planned)
        with open_private(results) as outcomes:
            sync_parent(results)
            completed = index_ledger(read_ledger(outcomes, repair_last_record=True), planned)
            if not completed.keys() <= claimed.keys():
                raise ValueError("Terminal result without a durable claim")
            for trial_id in sorted(claimed.keys() - completed.keys()):
                row = {**claimed[trial_id], "status": "unknown_outcome", "finished_utc": utc_now()}
                append_row(outcomes, row)
                completed[trial_id] = row
            exposure = sum((reservation(row) for row in completed.values()), Decimal(0))
            pending = [trial for trial in trials if trial_key(trial[0]) not in claimed]
            started = 0
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for offset in range(0, min(limit, len(pending)), concurrency):
                    wave = []
                    for base, body, route in pending[offset:min(offset + concurrency, limit)]:
                        reserve = amount(base["reserved_usd"])
                        if exposure + reserve > budget:
                            break
                        claim = {**base, "started_utc": utc_now()}
                        append_row(claims, claim)
                        exposure += reserve
                        wave.append((claim, pool.submit(responder, body, route, key, timeout)))
                    for claim, future in wave:
                        try:
                            outcome = future.result()
                        except Exception:
                            outcome = {"status": "unknown_outcome"}
                        row = {**claim, **outcome, "finished_utc": utc_now()}
                        append_row(outcomes, row)
                        exposure += reservation(row) - amount(claim["reserved_usd"])
                        completed[trial_key(claim)] = row
                    started += len(wave)
                    if len(wave) < min(concurrency, limit - offset, len(pending) - offset):
                        break
    return {"planned": len(trials), "new_attempts": started, "terminal": len(completed),
            "remaining": len(trials) - len(completed), "reserved_usd": str(exposure),
            "statuses": dict(Counter(row["status"] for row in completed.values()))}


def reservation(row):
    return max(amount(row["reserved_usd"]), amount((row.get("usage") or {}).get("cost", "0")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="Reviewed {case_id,arm,prompt} JSONL")
    parser.add_argument("--routes", type=Path, required=True, help="Pinned routes and prices JSON")
    parser.add_argument("--results", type=Path, required=True, help="Private result JSONL")
    parser.add_argument("--approved-sha256", help="Reviewed case file SHA-256 required for execution")
    parser.add_argument("--budget-usd", default="2.50", help="Estimated batch exposure cap; max 10")
    parser.add_argument("--max-output-tokens", type=int, default=768)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--limit", type=int, help="Maximum number of new paid requests in this invocation")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--arms", choices=("paired", "triad"), default="triad")
    parser.add_argument("--execute", action="store_true", help="Make paid requests; default forecasts only")
    args = parser.parse_args(argv)
    try:
        budget = amount(args.budget_usd)
        if not 0 < budget <= 10 or not 1 <= args.max_output_tokens <= 4096 or not 1 <= args.timeout <= 55:
            raise ValueError("Expected budget in (0,10], max output in [1,4096], timeout in [1,55]")
        if not 1 <= args.concurrency <= 8 or (args.limit is not None and args.limit < 1):
            raise ValueError("Expected concurrency in [1,8] and any request limit to be positive")
        cases_raw, routes_raw = args.cases.read_bytes(), args.routes.read_bytes()
        cases_hash = hashlib.sha256(cases_raw).hexdigest()
        plan = {"protocol": PROTOCOL_VERSION, "cases_sha256": cases_hash,
                "routes_sha256": hashlib.sha256(routes_raw).hexdigest(),
                "endpoint": ENDPOINT, "max_output_tokens": args.max_output_tokens,
                "timeout": args.timeout, "arms": args.arms}
        plan_hash = hashlib.sha256(json_bytes(plan)).hexdigest()
        arms = ARMS | {"hold"} if args.arms == "triad" else ARMS
        cases = load_cases(cases_raw, expected_arms=arms)
        trials = list(plan_trials(cases, load_routes(routes_raw), args.max_output_tokens, plan_hash))
        if args.execute:
            if args.approved_sha256 != cases_hash:
                raise ValueError("Live execution requires the SHA-256 of the reviewed case file")
            key = os.environ.get("OPENROUTER_API_KEY", "")
            if not key.strip() or any(c.isspace() for c in key):
                raise ValueError("Set OPENROUTER_API_KEY privately in the execution environment")
            summary = run_trials(trials, args.results, key, args.timeout, budget,
                                 args.limit or len(trials), args.concurrency)
        else:
            summary = {"planned": len(trials), "reserved_usd": str(sum(reservation(base) for base, _, _ in trials))}
        print(json.dumps({**plan, **summary, "budget_usd": str(budget), "plan_sha256": plan_hash}, sort_keys=True))
    except json.JSONDecodeError:
        parser.exit(2, "Invalid JSON in plan or ledger\n")
    except (ValueError, KeyError) as error:
        parser.exit(2, f"Invalid plan or ledger: {error}\n")
    except BlockingIOError:
        parser.exit(2, "Another process owns the run ledger\n")
    except OSError:
        parser.exit(2, "Unable to read the plan or open the private result ledger\n")


if __name__ == "__main__":
    main()
