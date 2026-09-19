"""JSON Merge Patch (RFC 7386) 与三方递归合并。

合并模型：

* ``apply_patch(target, patch)`` —— 严格按 RFC 7386 重放补丁；
* ``three_way(base, current, incoming)`` —— 递归比较 基线(base) /
  当前版(current) / 来稿(incoming)，按路径合并，任一路径三方同时分歧即冲突。

缺失键使用哨兵 :data:`MISSING` 表示，以便与显式 JSON ``null`` 区分。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

__all__ = [
    "MISSING",
    "Missing",
    "Conflict",
    "apply_patch",
    "three_way",
    "escape_pointer",
]


class _Missing:
    """键不存在的哨兵；``None`` 表示显式 JSON null。"""

    _instance = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False

    def __deepcopy__(self, memo: dict) -> "_Missing":
        # 深拷贝必须保持单例身份，否则“缺失”与普通值无法区分。
        return self


Missing = _Missing
MISSING = _Missing()


class Conflict:
    """单条三方冲突。"""

    __slots__ = ("path", "base", "current", "incoming", "base_present",
                 "current_present", "incoming_present")

    def __init__(self, path: str, base: Any, current: Any, incoming: Any,
                 *, base_present: bool, current_present: bool,
                 incoming_present: bool) -> None:
        self.path = path
        self.base = base
        self.current = current
        self.incoming = incoming
        self.base_present = base_present
        self.current_present = current_present
        self.incoming_present = incoming_present

    def to_dict(self) -> Dict[str, Any]:
        def val(v: Any, present: bool) -> Any:
            return {"present": present, "value": v if present else None}

        return {
            "path": self.path,
            "base": val(self.base, self.base_present),
            "current": val(self.current, self.current_present),
            "incoming": val(self.incoming, self.incoming_present),
        }


def escape_pointer(token: str) -> str:
    """按 RFC 6901 转义 JSON Pointer 路径段（先 ``~`` 后 ``/``）。"""

    return token.replace("~", "~0").replace("/", "~1")


def _is_plain_object(value: Any) -> bool:
    """JSON 对象判定。

    只接受 ``dict``（JSON 反序列化的普通对象），排除 bool/int/list/None 等。
    """

    return isinstance(value, dict)


def apply_patch(target: Any, patch: Any) -> Any:
    """按 RFC 7386 将 Merge Patch 应用到 target，返回新值（不修改入参）。

    * patch 不是对象：整个 target 被替换为 patch（深拷贝）；
    * patch 为对象：逐键合并，值为 ``None`` 表示删除该键，值为嵌套对象则递归。
    """

    if not _is_plain_object(patch):
        return deepcopy(patch)

    if _is_plain_object(target):
        result: Dict[str, Any] = deepcopy(target)
    else:
        # 标量/数组/缺失 与对象补丁合并时，标量先被当作空对象处理（RFC 7386）。
        result = {}

    for key, patch_value in patch.items():
        if patch_value is None:
            result.pop(key, MISSING)
        else:
            result[key] = apply_patch(result.get(key, MISSING), patch_value)
    return result


def three_way(base: Any, current: Any, incoming: Any
              ) -> Tuple[Any, List[Conflict]]:
    """递归三方合并。

    比较三个版本，返回 ``(合并结果, 冲突列表)``。冲突非空时合并结果不可用。

    规则（按路径）：

    * current、incoming 相对 base 均未改动       -> 取 current；
    * 仅一侧改动                                 -> 取改动侧（自动重放）；
    * 两侧都改动且结果相等                       -> 取该结果；
    * current 与 incoming 同为对象               -> 按键递归合并；
    * 其余情况（含一侧删除一侧修改、类型互异）   -> 冲突。
    """

    conflicts: List[Conflict] = []
    merged = _merge(base, current, incoming, "", conflicts)
    return merged, conflicts


def _merge(base: Any, current: Any, incoming: Any, path: str,
           conflicts: List[Conflict]) -> Any:
    current_changed = current != base
    incoming_changed = incoming != base

    if not current_changed and not incoming_changed:
        return deepcopy(current)

    if not current_changed:
        # 只有来稿改动，直接重放。
        return deepcopy(incoming)

    if not incoming_changed:
        # 只有当前版改动（他人已先写入相同方向的变更也走这里）。
        return deepcopy(current)

    if current == incoming:
        # 双方独立做出相同修改。
        return deepcopy(current)

    if _is_plain_object(current) and _is_plain_object(incoming):
        base_obj = base if _is_plain_object(base) else {}
        result: Dict[str, Any] = {}
        for key in set(base_obj) | set(current) | set(incoming):
            b = base_obj[key] if key in base_obj else MISSING
            c = current[key] if key in current else MISSING
            i = incoming[key] if key in incoming else MISSING
            child_path = f"{path}/{escape_pointer(key)}"
            if key not in current and key not in incoming:
                continue  # 两侧都删除了该键
            child = _merge(b, c, i, child_path, conflicts)
            if child is MISSING:
                result.pop(key, None)  # 该键在重放后被删除
            else:
                result[key] = child
        return result

    conflicts.append(Conflict(
        path=path,
        base=base,
        current=current,
        incoming=incoming,
        base_present=base is not MISSING,
        current_present=current is not MISSING,
        incoming_present=incoming is not MISSING,
    ))
    return deepcopy(current)
