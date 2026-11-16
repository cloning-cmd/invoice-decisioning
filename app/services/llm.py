"""Gemini client, the extraction tool schema, and the response cache.

The model's only job is to read. It is forced (function-calling mode ANY,
one allowed function) to answer through a single function whose parameter
schema IS the extraction contract, so the output is structured JSON by
construction — we never parse prose and never regex model text. The payload is
then validated with jsonschema before anything downstream sees it.

The brief specified Claude via the Anthropic SDK; extraction runs on Gemini
instead (see ASSUMPTIONS.md). Only this file knows which provider is used.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app import config


class _ExplicitKeyFilter(logging.Filter):
    """Drop the SDK's 'Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using
    GOOGLE_API_KEY.' line. We always pass `api_key=` explicitly, and an explicit
    key takes precedence over the environment, so the line is untrue here and
    would send whoever reads the log after the wrong key."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "Both GOOGLE_API_KEY and GEMINI_API_KEY are set" not in record.getMessage()


logging.getLogger("google_genai._api_client").addFilter(_ExplicitKeyFilter())

TOOL_NAME = "record_invoice_extraction"

# Bump when the schema or prompt changes, so stale cache entries are not replayed
# against a different contract.
PROMPT_VERSION = "2026-11-16.2"


# ---------------------------------------------------------------- the contract

def _field(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "required": ["value", "confidence", "evidence", "page"],
        "properties": {
            "value": {"type": ["string", "null"],
                      "description": "Exactly as printed. Not normalised, not computed. null if absent or illegible."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                           "description": "Your reading confidence for THIS field only."},
            "evidence": {"type": ["string", "null"],
                         "description": "Verbatim substring of the document including its label, e.g. 'Invoice No: INV-2291'."},
            "page": {"type": ["integer", "null"], "minimum": 1},
        },
    }


def _obj(props: dict[str, Any], description: str = "") -> dict[str, Any]:
    return {"type": "object", "description": description, "additionalProperties": False,
            "required": list(props), "properties": props}


EXTRACTION_SCHEMA: dict[str, Any] = _obj({
    "document_type": {"type": "string", "enum": ["tax_invoice", "proforma", "credit_note", "purchase_order",
                                                 "delivery_note", "statement", "unknown"]},
    "document_quality": _obj({
        "is_scan": {"type": "boolean"},
        "legibility": {"type": "number", "minimum": 0, "maximum": 1},
        "pages": {"type": "integer", "minimum": 1},
        "issues": {"type": "array", "items": {"type": "string", "enum": [
            "skewed", "low_contrast", "partially_cropped", "handwriting",
            "stamp_overlap", "watermark", "second_page_missing"]}},
    }),
    "supplier": _obj({
        "name": _field("Supplier / seller legal name as printed"),
        "tax_id": _field("Supplier GSTIN / VAT number"),
        "address": _field("Supplier address"),
        "email": _field("Supplier email address"),
    }),
    "buyer": _obj({
        "name": _field("Buyer / Bill To name"),
        "tax_id": _field("Buyer GSTIN / VAT number"),
    }),
    "invoice_number": _field("Invoice number"),
    "invoice_date": _field("Invoice date, as printed"),
    "due_date": _field("Payment due date, as printed"),
    "currency": _field("Currency as printed: symbol, code or word (e.g. '₹', 'INR', 'EUR')"),
    "po_references": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["value", "confidence", "evidence", "explicit"],
        "properties": {
            "value": {"type": "string", "description": "The PO reference as printed"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "string", "description": "Verbatim surrounding text"},
            "explicit": {"type": "boolean",
                         "description": "true if printed in a PO field; false if mentioned elsewhere, e.g. in notes"},
        }}},
    "line_items": {"type": "array", "items": _obj({
        "line_no": {"type": "integer", "minimum": 1},
        "description": _field("Line description"),
        "sku": _field("Item / part code"),
        "qty": _field("Quantity as printed"),
        "uom": _field("Unit of measure"),
        "unit_price": _field("Unit rate as printed"),
        "tax_rate_pct": _field("Tax / GST rate for this line as printed"),
        "line_total": _field("Line amount as printed"),
    })},
    "totals": _obj({
        "subtotal": _field("Subtotal / taxable value as printed. null if not printed — never add up lines."),
        "discount": _field("Discount as printed"),
        "tax_total": _field("Total tax as printed. null if not printed."),
        "tax_breakdown": {"type": "array", "items": _obj({
            "label": {"type": "string", "description": "e.g. 'CGST 9%'"},
            "amount": _field("Amount for this tax line as printed"),
        })},
        "shipping": _field("Freight / shipping / handling charge as printed"),
        "grand_total": _field("Grand total / amount payable as printed"),
        "amount_in_words": _field("Amount in words as printed"),
    }),
    "payment": _obj({
        "terms": _field("Payment terms as printed"),
        "bank_name": _field("Bank name"),
        "account_number": _field("Bank account number / IBAN as printed"),
        "ifsc_or_swift": _field("IFSC or SWIFT/BIC code"),
        "beneficiary_name": _field("Account holder / beneficiary name"),
    }),
    "notes": {"type": "string", "description": "Anything odd you noticed: conflicts, corrections, stamps, damage."},
    "unreadable_regions": {"type": "array", "items": _obj({
        "page": {"type": "integer", "minimum": 1},
        "description": {"type": "string", "description": "What is unreadable and which field it would hold"},
        "box_2d": {"type": ["array", "null"], "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                   "minItems": 4, "maxItems": 4,
                   "description": "Approximate region as [ymin, xmin, ymax, xmax], normalised 0-1000 to the page"},
    })},
}, "Structured reading of one vendor document")

_VALIDATOR = jsonschema.Draft202012Validator(EXTRACTION_SCHEMA)

SYSTEM_PROMPT = """\
You are a document extraction engine. You report what the document says. You never judge whether the invoice should be paid, never infer values that are not present, and never repair arithmetic. If a field is absent or illegible, return null with confidence 0 and say why in notes.

Report by calling the record_invoice_extraction function. Rules:

1. Return null, not a guess. A plausible invoice number is worse than no invoice number: downstream, a null routes to a human and a wrong value routes to payment.
2. confidence is your reading confidence for THAT field, not your overall impression of the document. A crisp header on a blurry page is still 0.98.
3. evidence must be a verbatim substring of the document as it appears, long enough to be locatable — include the label ("Invoice No: INV-2291", not "INV-2291"). Do not paraphrase, reformat or correct it.
4. Never compute. If the subtotal is not printed, subtotal is null. Do not add up line items. The system does its own arithmetic and compares it with what is printed; doing the sum for it destroys that signal.
5. Do not normalise dates or amounts. Return "15/03/2026" and "1,23,456.78" exactly as printed.
6. If the document is not an invoice, set document_type accordingly and return whatever fields exist. Do not refuse.
7. If a value appears twice with different readings (header vs footer), return the one in the more authoritative position and mention the conflict in notes.
8. List every PO reference you can see, including ones mentioned only in notes or line descriptions (explicit=false for those).
9. Report any region you cannot read in unreadable_regions, naming the field it would hold and giving its approximate box_2d on the page.

The images are the source of truth. The machine-read text is a convenience that may contain OCR errors; where it disagrees with the image, trust the image and keep your evidence verbatim to the image."""

TOOL_DESCRIPTION = "Record the structured reading of the document. Call exactly once."


class ExtractionError(Exception):
    """Extraction could not produce a schema-valid payload. The message is shown to the user."""


@dataclass
class ExtractionResult:
    payload: dict[str, Any]      # schema-valid tool input
    raw: dict[str, Any]          # verbatim responses + provenance, stored as runs.raw_llm_json
    source: str                  # live | cache
    attempts: int
    usage: dict[str, int]


# ---------------------------------------------------------------- bank-number masking

_LONG_DIGIT_RUN = re.compile(r"(?:\d[ -]?){8,}\d")


def keep_last4(s: str) -> str:
    """'50200012344471' -> '••••••••••4471'. Separators are kept, so the shape survives."""
    alnum = [i for i, c in enumerate(s) if c.isalnum()]
    chars = list(s)
    for i in alnum[:-4]:
        chars[i] = "•"
    return "".join(chars)


def mask_long_digit_runs(text: str) -> str:
    """Mask any 9+ digit run (account numbers) down to its last four."""
    return _LONG_DIGIT_RUN.sub(lambda m: keep_last4(m.group()), text)


def mask_account_number(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask payment.account_number in place before the payload is stored anywhere.

    The system only ever compares the last four digits with the vendor master,
    so the full number has no business in the database, the cache or the logs.
    """
    acct = (payload.get("payment") or {}).get("account_number")
    if isinstance(acct, dict):
        value = acct.get("value")
        if value:
            acct["value"] = keep_last4(value)
            ev = acct.get("evidence")
            if ev:
                acct["evidence"] = (ev.replace(value, acct["value"]) if value in ev
                                    else mask_long_digit_runs(ev))
        elif acct.get("evidence"):
            acct["evidence"] = mask_long_digit_runs(acct["evidence"])
    return payload


def _mask_everywhere(node: Any) -> None:
    """Mask every extraction payload nested anywhere in a raw provider response."""
    if isinstance(node, dict):
        if isinstance(node.get("payment"), dict):
            mask_account_number(node)
        for child in node.values():
            _mask_everywhere(child)
    elif isinstance(node, list):
        for child in node:
            _mask_everywhere(child)


def validation_errors(payload: Any) -> list[str]:
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
            for e in sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.absolute_path))]


def iter_fields(node: Any, path: str = "", schema: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Flatten the payload to (field_path, field_object) pairs, in schema order
    (which follows the page: header, parties, lines, totals, payment), not in
    whatever key order the model happened to emit.

    Paths look like 'invoice_number', 'totals.grand_total', 'line_items[2].unit_price'.
    A field object is any dict carrying value + confidence.
    """
    if schema is None and path == "":
        schema = EXTRACTION_SCHEMA
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if "value" in node and "confidence" in node:
            out.append((path, node))
            return out
        props = (schema or {}).get("properties", {})
        keys = [k for k in props if k in node] + [k for k in node if k not in props]
        for key in keys:
            out.extend(iter_fields(node[key], f"{path}.{key}" if path else key, props.get(key)))
    elif isinstance(node, list):
        items = (schema or {}).get("items")
        for i, child in enumerate(node):
            out.extend(iter_fields(child, f"{path}[{i}]", items))
    return out


# ---------------------------------------------------------------- cache

def cache_path(sha256: str, cache_dir: Path | None = None) -> Path:
    return Path(cache_dir or config.LLM_CACHE_DIR) / f"{sha256}.json"


def _read_cache(sha256: str, cache_dir: Path | None) -> dict[str, Any] | None:
    path = cache_path(sha256, cache_dir)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_cache(sha256: str, entry: dict[str, Any], cache_dir: Path | None) -> None:
    path = cache_path(sha256, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- the call

def _user_parts(images_jpeg: list[bytes], machine_text: str) -> list[types.Part]:
    parts: list[types.Part] = []
    for i, img in enumerate(images_jpeg, start=1):
        parts.append(types.Part.from_text(text=f"Page {i} of {len(images_jpeg)}:"))
        parts.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    parts.append(types.Part.from_text(text="MACHINE-READ TEXT (PDF text layer or OCR — possibly imperfect):\n"
                                           + (machine_text.strip() or "(none available — read from the images)")))
    parts.append(types.Part.from_text(text=f"Extract this document by calling {TOOL_NAME}."))
    return parts


def _generation_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=config.LLM_TEMPERATURE,
        max_output_tokens=config.LLM_MAX_TOKENS,
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
            name=TOOL_NAME, description=TOOL_DESCRIPTION, parameters_json_schema=EXTRACTION_SCHEMA)])],
        # Mode ANY restricted to one function = the model must answer by calling
        # it; there is no path for a free-text reply (let alone a verdict).
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY, allowed_function_names=[TOOL_NAME])),
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(config.LLM_THINKING_LEVEL.upper())),
        http_options=types.HttpOptions(timeout=config.LLM_TIMEOUT_SECONDS * 1000),
    )


def _api_error(exc: genai_errors.APIError) -> ExtractionError:
    if exc.code in (401, 403):
        return ExtractionError(f"Gemini API rejected the key ({exc.code}) — check GEMINI_API_KEY in .env")
    if exc.code == 404:
        return ExtractionError(f"model {config.LLM_MODEL!r} is not available to this key: {exc.message}")
    if exc.code == 429:
        return ExtractionError(f"Gemini API quota or rate limit hit (429): {exc.message}")
    return ExtractionError(f"Gemini API error {exc.code} {exc.status}: {exc.message}")


def _call_live(images_jpeg: list[bytes], machine_text: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    if not config.GEMINI_API_KEY:
        raise ExtractionError("GEMINI_API_KEY is not set in .env")
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    gen_config = _generation_config()
    contents: list[types.Content] = [types.Content(role="user", parts=_user_parts(images_jpeg, machine_text))]
    responses: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    last_errors: list[str] = []

    for attempt in range(1 + config.LLM_SCHEMA_RETRIES):
        try:
            resp = client.models.generate_content(model=config.LLM_MODEL, contents=contents, config=gen_config)
        except (genai_errors.ClientError, genai_errors.ServerError) as exc:
            raise _api_error(exc) from exc
        except httpx2.TimeoutException as exc:
            raise ExtractionError(f"Gemini API did not answer within {config.LLM_TIMEOUT_SECONDS} s "
                                  "(LLM_TIMEOUT_SECONDS)") from exc
        except httpx2.TransportError as exc:
            raise ExtractionError("cannot reach the Gemini API (network). "
                                  "Set LLM_CACHE_MODE=offline to use cached extractions only") from exc

        responses.append(resp.model_dump(mode="json", exclude_none=True))
        um = resp.usage_metadata
        if um:
            usage["input_tokens"] += um.prompt_token_count or 0
            usage["output_tokens"] += (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
        finish = resp.candidates[0].finish_reason if resp.candidates else None
        call = next((c for c in (resp.function_calls or []) if c.name == TOOL_NAME), None)
        if call is None:
            last_errors = [f"no {TOOL_NAME} call in response (finish_reason={finish})"]
        else:
            payload = json.loads(json.dumps(call.args or {}))   # plain JSON types, nothing SDK-specific
            last_errors = validation_errors(payload)
            if finish == types.FinishReason.MAX_TOKENS:
                last_errors.insert(0, "response truncated at max_output_tokens")
            if not last_errors:
                return payload, responses, usage
        if attempt < config.LLM_SCHEMA_RETRIES and call is not None and resp.candidates:
            # Feed the validator's own words back; the model corrects its payload.
            contents.append(resp.candidates[0].content)
            contents.append(types.Content(role="user", parts=[types.Part.from_function_response(
                name=TOOL_NAME, response={"error": "Schema validation failed:\n" + "\n".join(last_errors[:20])
                                                   + "\nCall the function again with a corrected, complete payload."})]))
    raise ExtractionError("model output failed schema validation after retry: " + "; ".join(last_errors[:5]))


def extract_document(sha256: str, images_jpeg: list[bytes], machine_text: str, *,
                     mode: str | None = None, cache_dir: Path | None = None) -> ExtractionResult:
    """Run (or replay from cache) the extraction for one file, keyed by its sha256."""
    mode = mode or config.LLM_CACHE_MODE
    cached = _read_cache(sha256, cache_dir) if mode in ("cached", "offline") else None
    if cached is not None and cached.get("prompt_version") != PROMPT_VERSION and mode == "cached":
        cached = None  # contract changed since this was recorded; re-read live
    if cached is not None:
        errors = validation_errors(cached["payload"])
        if errors:
            raise ExtractionError(f"cached extraction for {sha256[:12]} fails the schema: {errors[0]}")
        raw = {"source": "cache", "cache_file": cache_path(sha256, cache_dir).name, **cached}
        return ExtractionResult(cached["payload"], raw, "cache", 0, {"input_tokens": 0, "output_tokens": 0})
    if mode == "offline":
        raise ExtractionError(f"LLM_CACHE_MODE=offline and no cached extraction exists for this file "
                              f"(sha256 {sha256[:12]}…); no network call was attempted")

    payload, responses, usage = _call_live(images_jpeg, machine_text)
    mask_account_number(payload)
    _mask_everywhere(responses)
    entry = {"sha256": sha256, "model": config.LLM_MODEL, "prompt_version": PROMPT_VERSION,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "usage": usage, "payload": payload, "responses": responses}
    _write_cache(sha256, entry, cache_dir)
    return ExtractionResult(payload, {"source": "live", **entry}, "live", len(responses), usage)
