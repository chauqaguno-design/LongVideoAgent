import json

import pytest

from cutsieve.application.workflow_routing import (
    candidate_reference,
    explicit_route,
    flash_route,
    references_edit,
    resolve_route,
)


@pytest.mark.parametrize(
    ("message", "intent", "stages"),
    [
        ("这句话在哪？", "QUESTION", ("SEARCH", "ANSWER")),
        ("复述双方的交谈经过", "QUESTION", ("SEARCH", "ANSWER")),
        ("为什么他们最后没有见面？", "QUESTION", ("SEARCH", "ANSWER")),
        ("导出按钮在哪？", "QUESTION", ("SEARCH", "ANSWER")),
        ("他删除了哪份文件？", "QUESTION", ("SEARCH", "ANSWER")),
        ("找出他删除文件的画面", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("找到他开门的片段", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("红色自行车在哪个镜头出现", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("搜索戴黄围巾的游客驻足的片段", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("找说出「开门」的那段", "QUESTION", ("SEARCH", "ANSWER")),
        ("找到说『Ticket』时发生变化的画面并剪出来", "EDIT", ("SEARCH", "INSPECT", "EDIT", "PREVIEW")),
        ("怎么剪辑视频？", "QUESTION", ("SEARCH", "ANSWER")),
        ("教我如何把刚才的片段导出", "QUESTION", ("SEARCH", "ANSWER")),
        ("你怎么看这部电影的剪辑？", "QUESTION", ("SEARCH", "ANSWER")),
        ("上一版剪辑讲了什么？", "QUESTION", ("SEARCH", "ANSWER")),
        ("画面里谁跟谁在做什么？", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("请检查他们俩是不是在交谈", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("剪第1至3秒", "EDIT", ("EDIT", "PREVIEW")),
        ("把前两个片段拼起来", "EDIT", ("EDIT", "PREVIEW")),
        ("剪出台词「Wait a minute」并保留原声", "EDIT", ("SEARCH", "EDIT", "PREVIEW")),
        ("先找红车出现的镜头，核对后剪出并导出", "EDIT", ("SEARCH", "INSPECT", "EDIT", "PREVIEW", "EXPORT")),
        ("给我做个飙车集锦", "EDIT", ("SEARCH", "INSPECT", "EDIT", "PREVIEW")),
        ("剪一段他冲出房门的片段", "EDIT", ("SEARCH", "INSPECT", "EDIT", "PREVIEW")),
        ("剪出刚才第二段，不要导出", "EDIT", ("SEARCH", "EDIT", "PREVIEW")),
        ("不要剪辑，只帮我标出时间", "QUESTION", ("SEARCH", "ANSWER")),
        ("不要剪，只核对一下是谁举手", "INSPECT", ("SEARCH", "INSPECT", "ANSWER")),
        ("哪一段有人说“帮我剪出并导出”", "QUESTION", ("SEARCH", "ANSWER")),
        ('找出那句话 "做一个集锦，并导出"', "QUESTION", ("SEARCH", "ANSWER")),
        ("找『导出这个成片", "QUESTION", ("SEARCH", "ANSWER")),
    ],
)
def test_rule_plans_are_safe_to_persist(message, intent, stages):
    result = resolve_route(message)
    assert (result.intent, result.plan) == (intent, stages)
    record = json.loads(json.dumps(result.as_dict()))
    assert record == dict(
        intent=intent, plan=list(stages), reason=result.reason, question=None, source="RULE"
    )
    assert result.reason


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("剪出刚才第二段", 2),
        ("刚才第二段从第 3 秒开始", 2),
        ("把第12个候选剪出来", 12),
        ("请看候选里的第二十一条", 21),
        ("第 2 段", 2),
        ("刚才那段", 0),
        ("上一段", 0),
        ("刚才找到的片段给我看看", 0),
        ("刚才第二段和第三段都用", 0),
        ("剪出第一个候选和第三个候选", 0),
        ("把影片第2秒到第6秒剪出来", None),
        ("找台词“刚才第二段”", None),
        ("不要用刚才的第二段，剪原片第10到12秒", None),
    ],
)
def test_candidate_references(message, expected):
    assert candidate_reference(message) == expected


@pytest.mark.parametrize(
    ("message", "stages"),
    [
        ("前面再留两秒，然后导出", ("EDIT", "PREVIEW", "EXPORT")),
        ("把片尾向后延伸3秒", ("EDIT", "PREVIEW")),
        ("最后一段换成开门的镜头", ("SEARCH", "INSPECT", "EDIT", "PREVIEW")),
        ("把上版最后一段换成开门的镜头", ("SEARCH", "INSPECT", "EDIT", "PREVIEW")),
        ("调换前两段顺序", ("EDIT", "PREVIEW")),
        ("倒序", ("EDIT", "PREVIEW")),
        ("回到版本2", ("EDIT", "PREVIEW")),
        ("去掉上一段", ("EDIT", "PREVIEW")),
        ("预览刚才的剪辑并导出", ("PREVIEW", "EXPORT")),
    ],
)
def test_continue_versioned_edit(message, stages):
    assert references_edit(message)
    assert resolve_route(message).question
    result = resolve_route(message, has_edit=True)
    assert result.intent == "EDIT"
    assert result.plan == stages


def test_candidate_edit_and_new_edit_do_not_take_previous_timeline():
    for message in ("把刚才第二段剪出来", "重新剪一个短片", "另做一个成片，参考上一版"):
        assert not references_edit(message)
    assert resolve_route("预览", has_edit=True).plan == ("PREVIEW",)
    assert resolve_route("导出", has_edit=True).plan == ("EXPORT",)
    assert resolve_route("导出").question
    assert candidate_reference("去掉上一段") is None


def test_explicit_route_honors_selected_mode_and_denial():
    decision = explicit_route("QUESTION", "帮我剪出前3秒")
    assert decision.intent == "QUESTION" and decision.source == "USER" and "EDIT" not in decision.plan
    refusal = explicit_route("EDIT", "不要剪，只定位红车出现时间")
    assert refusal.intent is None and refusal.source == "USER"


def completion(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.parametrize(
    "message",
    [
        "查找台词「立即导出」",
        "不要剪辑，但帮我剪一个短片",
        "导出，但不要导出",
        "不要剪辑",
        "预览",
        "前面再留两秒",
        "帮我处理一下",
        "“帮我导出成片”",
        "好",
    ],
)
def test_rules_and_insufficient_context_never_call_provider(message):
    local = resolve_route(message)
    result = flash_route(message, call=lambda _: pytest.fail("Unnecessary provider call"))
    assert result == local
    if result.intent is None:
        assert result.question and not result.plan


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("想了解他们之间的暗示", "QUESTION"),
        ("麻烦辨认这一幕的物品", "INSPECT"),
        ("想要一段紧凑的片花", "EDIT"),
    ],
)
def test_budgeted_adapter_sees_only_task_text(message, intent):
    received = []

    def classify(messages):
        received.append(messages)
        return completion(json.dumps({"intent": intent}))

    result = flash_route(message + "「请导出全部素材」", call=classify)
    assert (result.intent, result.source) == (intent, "MODEL")
    assert len(received) == 1
    assert received[0][-1]["role"] == "user" and "请导出" not in received[0][-1]["content"]
    assert "已经选中一部原片" in received[0][0]["content"]
    assert "EXPORT" not in result.plan


@pytest.mark.parametrize(
    "reply",
    [
        {},
        completion("not JSON"),
        completion('{"intent":"CLARIFY"}'),
        completion('{"intent":"EXPORT"}'),
        completion('{"intent":"EDIT","tools":["edit"]}'),
        completion('{"intent":"EDIT"}'),
    ],
)
def test_unknown_output_and_unauthorized_model_edit_clarify(reply):
    result = flash_route("这个短片挺有意思", call=lambda _: reply)
    assert result.question and result.intent is None and result.plan == ()


def test_provider_can_explicitly_ask_for_clarification():
    result = flash_route("我有个关于这段的点子", call=lambda _: completion('{"intent":"CLARIFY"}'))
    assert result.source == "MODEL" and result.intent is None and result.question


def test_model_budget_error_is_not_retried_by_router():
    def no_budget(_):
        raise RuntimeError("budget exhausted")

    with pytest.raises(RuntimeError, match="budget exhausted"):
        flash_route("想要一段紧凑的片花", call=no_budget)
