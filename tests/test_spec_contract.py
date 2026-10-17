"""Review 17e SDK-11: the response readers must agree with docs/openapi.yaml.

For every reader the test starts from the examples of the OpenAPI document and, field by field (nested objects and
array items included):

* drops the field: if the SDK then rejects the body, the field must be in ``required`` of its schema;
* sets it to ``null`` where the schema allows null: the SDK must accept it.

So the SDK can never again require a field (or reject a ``null``) that the published contract does not promise.
The test is skipped when the spec file is not next to the SDK (for example in an unpacked sdist) or PyYAML is missing.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from tmailor import models as M
from tmailor._schema import SchemaError

SPEC_PATH = Path(os.environ.get("TMAILOR_OPENAPI_SPEC") or Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml")

yaml = pytest.importorskip("yaml")
if not SPEC_PATH.is_file():  # pragma: no cover - only outside the monorepo
    pytest.skip(f"OpenAPI document not found at {SPEC_PATH}", allow_module_level=True)

SPEC: Dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text("utf-8"))


def _resolve(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Follow $ref and merge allOf into one object schema; for oneOf/anyOf with null keep the non-null branch."""
    if "$ref" in schema:
        node: Any = SPEC
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _resolve(node)
    if "allOf" in schema:
        out: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for sub in schema["allOf"]:
            r = _resolve(sub)
            out["properties"].update(r.get("properties", {}))
            out["required"] += r.get("required", [])
        return out
    for key in ("oneOf", "anyOf"):
        if key in schema:
            branches = [b for b in schema[key] if b.get("type") != "null"]
            if len(branches) == 1:
                return _resolve(branches[0])
    return schema


def _nullable(schema: Dict[str, Any]) -> bool:
    t = schema.get("type")
    if isinstance(t, list) and "null" in t:
        return True
    return any(b.get("type") == "null" for k in ("oneOf", "anyOf") for b in schema.get(k, []))


def _response(path: str, method: str, status: str) -> Tuple[Dict[str, Any], List[Any]]:
    content = SPEC["paths"][path][method]["responses"][status]["content"]["application/json"]
    examples: List[Any] = []
    if "example" in content:
        examples.append(content["example"])
    for ex in (content.get("examples") or {}).values():
        examples.append(ex["value"])
    assert examples, f"{method.upper()} {path} {status} has no example in the spec"
    return content["schema"], examples


def _component_examples(name: str) -> List[Any]:
    """Examples of every response in the spec whose schema is the component ``name``."""
    out: List[Any] = []
    for item in SPEC["paths"].values():
        for op in item.values():
            for resp in (op.get("responses") or {}).values():
                content = (resp.get("content") or {}).get("application/json") or {}
                if content.get("schema", {}).get("$ref") == f"#/components/schemas/{name}":
                    if "example" in content:
                        out.append(content["example"])
                    out += [ex["value"] for ex in (content.get("examples") or {}).values()]
    assert out, f"no example for {name}"
    return out


Reader = Callable[[Any], Any]

# reader, schema, example bodies
CASES: List[Tuple[str, Reader, Dict[str, Any], List[Any]]] = [
    ("Health", M.Health.from_api, {"$ref": "#/components/schemas/Health"}, _component_examples("Health")),
    ("Me", M.Me.from_api, {"$ref": "#/components/schemas/Me"}, _component_examples("Me")),
    ("Mailbox", M.MailboxFields.from_api, {"$ref": "#/components/schemas/Mailbox"}, _component_examples("Mailbox")),
    ("Message", M.Message.from_api_with_body, {"$ref": "#/components/schemas/Message"}, _component_examples("Message")),
    ("Usage", M.Usage.from_api, {"$ref": "#/components/schemas/Usage"}, _component_examples("Usage")),
    ("DomainList", M.DomainList.from_api, *_response("/v1/domains", "get", "200")),
    ("MailboxPage", M.parse_mailbox_page, *_response("/v1/mailboxes", "get", "200")),
    ("MessagePage", M.MessagePage.from_api, *_response("/v1/mailbox/messages", "post", "200")),
    ("Deleted", M.parse_deleted, *_response("/v1/mailbox/messages/clear", "post", "200")),
]


def _accepts(reader: Reader, body: Any) -> Optional[str]:
    """None when the reader accepts ``body``, else the SchemaError text."""
    try:
        reader(body)
    except SchemaError as exc:
        return str(exc)
    return None


def _walk(reader: Reader, root: Any, node: Any, schema: Dict[str, Any], where: str, problems: List[str]) -> None:
    schema = _resolve(schema)
    if isinstance(node, dict):
        required = set(schema.get("required", []))
        props = schema.get("properties", {})
        for key in list(node):
            prop = props.get(key)
            if prop is None:
                continue  # extra example field the schema does not describe: not part of the contract
            # 1) drop the field
            saved = node.pop(key)
            err = _accepts(reader, root)
            node[key] = saved
            if err is not None and key not in required:
                problems.append(f"{where}.{key}: SDK requires it but the spec does not list it in required ({err})")
            # 2) null where the spec allows null
            if _nullable(prop) and saved is not None:
                node[key] = None
                err = _accepts(reader, root)
                node[key] = saved
                if err is not None and "both be null" not in err:  # token/web of Mailbox are null together
                    problems.append(f"{where}.{key}: spec allows null but the SDK rejects it ({err})")
            _walk(reader, root, node[key], prop, f"{where}.{key}", problems)
    elif isinstance(node, list) and node and "items" in schema:
        _walk(reader, root, node[0], schema["items"], f"{where}[0]", problems)


@pytest.mark.parametrize(("name", "reader", "schema", "examples"), CASES, ids=[c[0] for c in CASES])
def test_reader_matches_openapi(name: str, reader: Reader, schema: Dict[str, Any], examples: List[Any]) -> None:
    problems: List[str] = []
    for i, ex in enumerate(examples):
        body = copy.deepcopy(ex)
        assert _accepts(reader, body) is None, f"{name}: spec example #{i} is rejected: {_accepts(reader, body)}"
        _walk(reader, body, body, schema, f"{name}#{i}", problems)
    assert not problems, "\n".join(problems)


def test_contract_covers_every_component_read_by_the_sdk() -> None:
    covered = {c[2].get("$ref", "").rsplit("/", 1)[-1] for c in CASES}
    for name in ("Health", "Me", "Mailbox", "Message", "Usage"):
        assert name in covered


def test_walker_detects_a_mismatch() -> None:
    # Self-check: a reader stricter than its schema is reported.
    def strict(d: Any) -> Any:
        if "extra" not in d:
            raise SchemaError("$.extra is missing")
        return M.Health.from_api(d)

    body = {"status": "ok", "time": 1, "version": "v1", "extra": 1}
    schema = {"type": "object", "required": ["status"], "properties": {k: {} for k in body}}
    problems: List[str] = []
    _walk(strict, body, body, schema, "X", problems)
    assert any(p.startswith("X.extra") for p in problems)
    assert any(p.startswith("X.time") for p in problems)  # Health.from_api requires time, this schema does not
