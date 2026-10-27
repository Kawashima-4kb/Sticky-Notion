"""data source のスキーマからプロパティ入力欄の定義を作り、入力値を API 形式へ変換する。"""

import datetime
import json

SUPPORTED_TYPES = ("status", "select", "multi_select", "date", "checkbox")


def title_property_name(schema):
    for name, prop in schema.get("properties", {}).items():
        if prop.get("type") == "title":
            return name
    return "Name"


def editable_properties(schema):
    """入力欄を出すプロパティを [{"name", "type", "options"}] で返す（表示順は名前順）。"""
    fields = []
    for name, prop in schema.get("properties", {}).items():
        ptype = prop.get("type")
        if ptype not in SUPPORTED_TYPES:
            continue
        options = [o["name"] for o in prop.get(ptype, {}).get("options", [])] \
            if ptype in ("status", "select", "multi_select") else []
        fields.append({"name": name, "type": ptype, "options": options})
    fields.sort(key=lambda f: f["name"])
    return fields


def parse_date(text, today=None):
    """'今日'/'明日'/'YYYY-MM-DD'/'YYYY/M/D' を ISO 形式に。不正なら ValueError。"""
    today = today or datetime.date.today()
    text = text.strip()
    if text in ("今日", "today"):
        return today.isoformat()
    if text in ("明日", "tomorrow"):
        return (today + datetime.timedelta(days=1)).isoformat()
    parts = text.replace("/", "-").split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"日付の形式が不正です: {text}")
    return datetime.date(*(int(p) for p in parts)).isoformat()


def editable_properties_mcp(schema):
    """MCP の fetch で得たスキーマ（{名前: {"type", "options"}}）版の editable_properties。"""
    fields = []
    for name, prop in schema.items():
        ptype = prop.get("type")
        if ptype not in SUPPORTED_TYPES:
            continue
        options = []
        if ptype in ("status", "select", "multi_select"):
            raw = prop.get("options") or [o for g in prop.get("groups", []) for o in g.get("options", [])]
            options = [o["name"] for o in raw if isinstance(o, dict) and "name" in o]
        fields.append({"name": name, "type": ptype, "options": options})
    fields.sort(key=lambda f: f["name"])
    return fields


def title_property_name_mcp(schema):
    return next((name for name, prop in schema.items() if prop.get("type") == "title"), "title")


def _mcp_key(name):
    # MCP では id / url という名前のプロパティに接頭辞が必要
    return f"userDefined:{name}" if name.lower() in ("id", "url") else name


def split_multi(value):
    return [v.strip() for v in value.replace("、", ",").split(",") if v.strip()]


def build_mcp_properties(title_name, title, fields, values):
    """MCP の create-pages 用（SQLite 形式の値）。空の入力は送らない。"""
    props = {_mcp_key(title_name): title}
    types = {f["name"]: f["type"] for f in fields}
    for name, value in values.items():
        ptype = types.get(name)
        if ptype == "checkbox":
            if value:
                props[_mcp_key(name)] = "__YES__"
            continue
        value = (value or "").strip()
        if not ptype or not value:
            continue
        if ptype in ("status", "select"):
            props[_mcp_key(name)] = value
        elif ptype == "multi_select":
            props[_mcp_key(name)] = json.dumps(split_multi(value), ensure_ascii=False)
        elif ptype == "date":
            props[f"date:{name}:start"] = parse_date(value)
            props[f"date:{name}:is_datetime"] = 0
    return props


def build_properties(title_name, title, fields, values):
    """values: {プロパティ名: 入力値}。空の入力は送らない。"""
    props = {title_name: {"title": [{"type": "text", "text": {"content": title[:2000]}}]}}
    types = {f["name"]: f["type"] for f in fields}
    for name, value in values.items():
        ptype = types.get(name)
        if ptype == "checkbox":
            if value:
                props[name] = {"checkbox": True}
            continue
        value = (value or "").strip()
        if not ptype or not value:
            continue
        if ptype in ("status", "select"):
            props[name] = {ptype: {"name": value}}
        elif ptype == "multi_select":
            props[name] = {"multi_select": [{"name": n} for n in split_multi(value)]}
        elif ptype == "date":
            props[name] = {"date": {"start": parse_date(value)}}
    return props
