"""apply_patch / three_way 的单元测试。"""

from __future__ import annotations

from app.merge import MISSING, apply_patch, three_way


def merge(base, current, incoming):
    result, conflicts = three_way(base, current, incoming)
    return result, [c.to_dict() for c in conflicts]


# ---------------------------------------------------------------- RFC 7386

def test_patch_null_deletes_key():
    assert apply_patch({"a": 1, "b": 2}, {"a": None}) == {"b": 2}


def test_patch_nested_null_deletes_deep_key():
    target = {"a": {"x": 1, "y": 2}}
    assert apply_patch(target, {"a": {"x": None}}) == {"a": {"y": 2}}


def test_patch_non_object_replaces_entire_target():
    assert apply_patch({"a": 1}, [1, 2]) == [1, 2]
    assert apply_patch({"a": 1}, None) is None
    assert apply_patch([1, 2], {"a": 1}) == {"a": 1}


def test_patch_does_not_mutate_inputs():
    target = {"a": {"x": 1}}
    patch = {"a": {"y": 2}}
    apply_patch(target, patch)
    assert target == {"a": {"x": 1}}
    assert patch == {"a": {"y": 2}}


def test_patch_object_replaces_scalar_then_null_deletes():
    # 标量遇到对象补丁先视为空对象；null 表示删除
    assert apply_patch(5, {"a": None}) == {}


# --------------------------------------------------------------- 三方合并

def test_three_way_fast_forward_when_current_unchanged():
    base = {"a": 1}
    incoming = apply_patch(base, {"b": 2})
    result, conflicts = merge(base, base, incoming)
    assert conflicts == []
    assert result == {"a": 1, "b": 2}


def test_three_way_disjoint_paths_auto_replay():
    base = {"a": 1, "b": 2}
    current = {"a": 10, "b": 2}          # 他人改了 a
    incoming = {"a": 1, "b": 2, "c": 3}  # 来稿新增 c
    result, conflicts = merge(base, current, incoming)
    assert conflicts == []
    assert result == {"a": 10, "b": 2, "c": 3}


def test_three_way_same_independent_change():
    base = {"a": 1}
    current = {"a": 5}
    incoming = {"a": 5}
    result, conflicts = merge(base, current, incoming)
    assert conflicts == []
    assert result == {"a": 5}


def test_three_way_nested_disjoint_changes():
    base = {"cfg": {"x": 1, "y": 2}}
    current = {"cfg": {"x": 100, "y": 2}}
    incoming = {"cfg": {"x": 1, "y": 200}}
    result, conflicts = merge(base, current, incoming)
    assert conflicts == []
    assert result == {"cfg": {"x": 100, "y": 200}}


def test_three_way_diverging_path_conflicts_and_lists_three_values():
    base = {"a": 1}
    current = {"a": 2}
    incoming = {"a": 3}
    result, conflicts = merge(base, current, incoming)
    assert result == {"a": 2}  # 有冲突时结果不可用，但形状完整
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["path"] == "/a"
    assert c["base"] == {"present": True, "value": 1}
    assert c["current"] == {"present": True, "value": 2}
    assert c["incoming"] == {"present": True, "value": 3}


def test_three_way_delete_vs_modify_conflicts():
    base = {"a": 1, "b": 2}
    current = {"b": 2}                 # 当前版删除 a
    incoming = {"a": 99, "b": 2}       # 来稿修改 a
    result, conflicts = merge(base, current, incoming)
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "/a"
    assert conflicts[0]["current"]["present"] is False
    assert conflicts[0]["incoming"]["value"] == 99


def test_three_way_both_delete_is_resolved():
    base = {"a": 1, "b": 2}
    current = {"b": 2}
    incoming = {"b": 2}
    result, conflicts = merge(base, current, incoming)
    assert conflicts == []
    assert result == {"b": 2}


def test_three_way_delete_one_side_and_modify_other_field():
    base = {"a": 1, "b": 2}
    current = {"b": 2}             # 删除 a
    incoming = {"a": 1, "b": 20}   # 修改 b
    result, conflicts = merge(base, current, incoming)
    assert conflicts == []
    assert result == {"b": 20}


def test_three_way_type_divergence_conflicts():
    base = {"a": {"x": 1}}
    current = {"a": {"x": 1, "y": 2}}
    incoming = {"a": [1, 2]}
    result, conflicts = merge(base, current, incoming)
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "/a"


def test_three_way_object_vs_scalar_replacement_conflicts():
    # 根级对象替换为标量 vs 字段级修改
    base = {"a": 1}
    current = {"a": 1, "b": 2}
    incoming = 42
    result, conflicts = merge(base, current, incoming)
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == ""


def test_three_way_add_same_key_same_value_ok():
    base = {}
    result, conflicts = merge(base, {"k": "v"}, {"k": "v"})
    assert conflicts == []
    assert result == {"k": "v"}


def test_three_way_add_same_key_different_value_conflicts():
    base = {}
    result, conflicts = merge(base, {"k": "v1"}, {"k": "v2"})
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "/k"


def test_three_way_add_disjoint_keys_merged():
    base = {}
    result, conflicts = merge(
        base, {"k1": {"x": 1}}, {"k2": {"y": 2}})
    assert conflicts == []
    assert result == {"k1": {"x": 1}, "k2": {"y": 2}}


def test_three_way_null_value_is_distinct_from_missing():
    base = {"a": None}
    # base 中 a 显式为 null；两侧都没动该键（MISSING 语义在子层）
    result, conflicts = merge({"x": 1}, {"x": None}, {"x": None})
    assert conflicts == []
    assert result == {"x": None}


def test_pointer_escaping_in_conflict_path():
    base = {"a/b": 1, "c~d": 2}
    current = {"a/b": 10, "c~d": 2}
    incoming = {"a/b": 20, "c~d": 2}
    _, conflicts = merge(base, current, incoming)
    assert conflicts[0]["path"] == "/a~1b"

    current = {"a/b": 1, "c~d": 10}
    incoming = {"a/b": 1, "c~d": 20}
    _, conflicts = merge(base, current, incoming)
    assert conflicts[0]["path"] == "/c~0d"


def test_missing_singleton_survives_deepcopy():
    from copy import deepcopy

    assert deepcopy(MISSING) is MISSING
