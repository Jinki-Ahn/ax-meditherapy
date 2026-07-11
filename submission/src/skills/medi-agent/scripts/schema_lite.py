"""jsonschema 폴백 — 외부 패키지 없는 환경에서도 온톨로지 검증이 동작하도록 하는 최소 구현.

schema.json(v0.4)이 실제로 쓰는 키워드만 지원한다:
type · properties · required · items · enum · const · pattern ·
minimum · maximum · $ref(#/$defs/...) · if/then

pipeline.py와 validate_ontology.py가 쓰는 jsonschema 인터페이스
(validate 함수, ValidationError의 .message/.absolute_path)와 호환된다.
지원하지 않는 키워드를 만나면 조용히 통과시키는 대신 오류를 낸다 —
폴백이 정식 검증기보다 느슨해져 부적합 데이터를 통과시키는 사고를 막기 위함.
"""

import re

_SUPPORTED = {
    "type", "properties", "required", "items", "enum", "const", "pattern",
    "minimum", "maximum", "$ref", "if", "then",
    # 주석성 키워드(검증에 영향 없음)
    "$schema", "$defs", "title", "description", "examples", "version",
}

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


class ValidationError(Exception):
    def __init__(self, message, path):
        super().__init__(message)
        self.message = message
        self.absolute_path = list(path)

    def __str__(self):
        loc = "/".join(str(p) for p in self.absolute_path) or "(root)"
        return f"{loc}: {self.message}"


def _resolve_ref(ref, root):
    if not ref.startswith("#/"):
        raise ValidationError(f"지원하지 않는 $ref: {ref}", [])
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _is_type(data, expected):
    if expected == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if expected == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if isinstance(data, bool) and expected != "boolean":
        return False
    return isinstance(data, _TYPES[expected])


def _check_type(data, expected, path):
    # "type": "string" 또는 "type": ["string", "null"] (유니언) 둘 다 허용
    candidates = expected if isinstance(expected, list) else [expected]
    if not any(_is_type(data, t) for t in candidates):
        raise ValidationError(f"'{expected}' 타입이 아님: {data!r}", path)


def _matches(data, schema, root):
    """if 절 판정용 — 통과 여부만 반환."""
    try:
        _validate(data, schema, root, [])
        return True
    except ValidationError:
        return False


def _validate(data, schema, root, path):
    unsupported = set(schema) - _SUPPORTED
    if unsupported:
        raise ValidationError(f"schema_lite가 지원하지 않는 키워드: {sorted(unsupported)}", path)

    if "$ref" in schema:
        _validate(data, _resolve_ref(schema["$ref"], root), root, path)
        return

    if "type" in schema:
        _check_type(data, schema["type"], path)

    if "const" in schema and data != schema["const"]:
        raise ValidationError(f"const {schema['const']!r}이어야 함: {data!r}", path)

    if "enum" in schema and data not in schema["enum"]:
        raise ValidationError(f"enum {schema['enum']} 밖의 값: {data!r}", path)

    if "pattern" in schema and isinstance(data, str):
        if not re.search(schema["pattern"], data):
            raise ValidationError(f"패턴 {schema['pattern']!r} 불일치: {data!r}", path)

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            raise ValidationError(f"최솟값 {schema['minimum']} 미만: {data!r}", path)
        if "maximum" in schema and data > schema["maximum"]:
            raise ValidationError(f"최댓값 {schema['maximum']} 초과: {data!r}", path)

    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                raise ValidationError(f"필수 필드 누락: {key!r}", path)
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                _validate(data[key], sub, root, path + [key])

    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            _validate(item, schema["items"], root, path + [i])

    if "if" in schema and _matches(data, schema["if"], root):
        if "then" in schema:
            _validate(data, schema["then"], root, path)


def validate(data, schema):
    _validate(data, schema, schema, [])
