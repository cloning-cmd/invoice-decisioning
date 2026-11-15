"""Anthropic client, the extraction tool schema, and the response cache.

The model's only job is to read. It is forced to answer through one tool whose
input_schema IS the extraction contract, so the output is structured JSON by
construction — we never parse prose and never regex model text. The payload is
then validated with jsonschema before anything downstream sees it.

SDK note: anthropic 1.x sends API version 2023-06-01, which still accepts
`temperature` for claude-sonnet-4-5; the 1.x signature no longer takes it as a
keyword, so it goes through `extra_body` (see ASSUMPTIONS.md).
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import jsonschema

from app import config

TOOL_NAME = "record_invoice_extraction"

# Bump when the schema or prompt changes, so stale cache entries are not replayed
# against a different contract.
PROMPT_VERSION = "2026-11-16.1"


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
    })},
}, "Structured reading of one vendor document")

_VALIDATOR = jsonschema.Draft202012Validator(EXTRACTION_SCHEMA)

SYSTEM_PROMPT = """\
You are a document extraction engine. You report what the document says. You never judge whether the invoice should be paid, never infer values that are not present, and never repair arithmetic. If a field is absent or illegible, return null with confidence 0 and say why in notes.

Report through the record_invoice_extraction tool. Rules:

1. Return null, not a guess. A plausible invoice number is worse than no invoice number: downstream, a null routes to a human and a wrong value routes to payment.
2. confidence is your reading confidence for THAT field, not your overall impression of the document. A crisp header on a blurry page is still 0.98.
3. evidence must be a verbatim substring of the document as it appears, long enough to be locatable — include the label ("Invoice No: INV-2291", not "INV-2291"). Do not paraphrase, reformat or correct it.
4. Never compute. If the subtotal is not printed, subtotal is null. Do not add up line items. The system does its own arithmetic and compares it with what is printed; doing the sum for it destroys that signal.
5. Do not normalise dates or amounts. Return "15/03/2026" and "1,23,456.78" exactly as printed.
6. If the document is not an invoice, set document_type accordingly and return whatever fields exist. Do not refuse.
7. If a value appears twice with different readings (header vs footer), return the one in the more authoritative position and mention the conflict in notes.
8. List every PO reference you can see, including ones mentioned only in notes or line descriptions (explicit=false for those).
9. Report any region you cannot read in unreadable_regions, naming the field it would hold.

The images are the source of truth. The machine-read text is a convenience that may contain OCR errors; where it disagrees with the image, trust the image and keep your evidence verbatim to the image."""

TOOL = {
    "name": TOOL_NAME,
    "description": "Record the structured reading of the document. Call exactly once.",
    "input_schema": EXTRACTION_SCHEMA,
}


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


def _mask_responses(responses: list[dict[str, Any]]) -> None:
    for r in responses:
        for block in r.get("content", []):
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                mask_account_number(block["input"])


def validation_errors(payload: Any) -> list[str]:
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
            for e in sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.absolute_path))]


def iter_fields(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Flatten the payload to (field_path, field_object) pairs.

    Paths look like 'invoice_number', 'totals.grand_total', 'line_items[2].unit_price'.
    A field object is any dict carrying value + confidence.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if "value" in node and "confidence" in node:
            out.append((path, node))
            return out
        for key, child in node.items():
            out.extend(iter_fields(child, f"{path}.{key}" if path else key))
    elif isinstance(node, list):
        for i, child in enumerate(node):
            out.extend(iter_fields(child, f"{path}[{i}]"))
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

def _user_content(images_jpeg: list[bytes], machine_text: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for i, img in enumerate(images_jpeg, start=1):
        content.append({"type": "text", "text": f"Page {i} of {len(images_jpeg)}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.standard_b64encode(img).decode("ascii")}})
    content.append({"type": "text", "text":
                    "MACHINE-READ TEXT (PDF text layer or OCR — possibly imperfect):\n"
                    + (machine_text.strip() or "(none available — read from the images)")})
    content.append({"type": "text", "text": "Extract this document with the record_invoice_extraction tool."})
    return content


def _call_live(images_jpeg: list[bytes], machine_text: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY) if config.ANTHROPIC_API_KEY else anthropic.Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_content(images_jpeg, machine_text)}]
    responses: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    last_errors: list[str] = []

    for attempt in range(1 + config.LLM_SCHEMA_RETRIES):
        try:
            resp = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=config.LLM_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[TOOL],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=messages,
                extra_body={"temperature": config.LLM_TEMPERATURE},
            )
        except anthropic.AuthenticationError as exc:
            raise ExtractionError("Anthropic API rejected the credentials — set ANTHROPIC_API_KEY in .env") from exc
        except anthropic.NotFoundError as exc:
            raise ExtractionError(f"model {config.LLM_MODEL!r} not available: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise ExtractionError("Anthropic API rate limit hit; retry shortly") from exc
        except anthropic.APIStatusError as exc:
            raise ExtractionError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ExtractionError("cannot reach the Anthropic API (network). "
                                  "Set LLM_CACHE_MODE=offline to use cached extractions only") from exc

        responses.append(resp.to_dict())
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        tool_use = next((b for b in resp.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if tool_use is None:
            last_errors = [f"no {TOOL_NAME} call in response (stop_reason={resp.stop_reason})"]
        else:
            payload = tool_use.input
            last_errors = validation_errors(payload)
            if resp.stop_reason == "max_tokens":
                last_errors.insert(0, "response truncated at max_tokens")
            if not last_errors:
                return payload, responses, usage
        if attempt < config.LLM_SCHEMA_RETRIES and tool_use is not None:
            # Feed the validator's own words back; the model corrects its payload.
            messages.append({"role": "assistant", "content": [b.to_dict() for b in resp.content]})
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_use.id, "is_error": True,
                "content": "Schema validation failed:\n" + "\n".join(last_errors[:20])
                           + "\nCall the tool again with a corrected, complete payload.",
            }]})
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
    _mask_responses(responses)
    entry = {"sha256": sha256, "model": config.LLM_MODEL, "prompt_version": PROMPT_VERSION,
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "usage": usage, "payload": payload, "responses": responses}
    _write_cache(sha256, entry, cache_dir)
    return ExtractionResult(payload, {"source": "live", **entry}, "live", len(responses), usage)
