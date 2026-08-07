"""Agent 模式单元测试 (v0.9) - 工具注册表/schema 生成/参数解析/执行循环/上限/来源收集"""
import asyncio

import pytest

from app.core.agent import AGENT_SYSTEM_PROMPT, AgentRun
from app.core.llm import LLMResponse, LLMService, LLMToolCall
from app.core.tools import Tool, ToolRegistry, tools as global_tools


# ---------------- 工具注册表与 schema ----------------

class TestToolRegistry:
    def test_global_registry_has_5_tools(self):
        names = [t.name for t in global_tools.list()]
        assert set(names) == {
            "search_knowledge_base", "preview_document",
            "list_knowledge_bases", "knowledge_base_stats", "get_current_time",
        }

    def test_schema_hides_dependencies(self):
        """依赖注入参数（user）不应暴露给模型"""
        schema = global_tools.get("search_knowledge_base").to_schema()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "search_knowledge_base"
        props = fn["parameters"]["properties"]
        assert "user" not in props
        assert props["query"]["type"] == "string"
        assert props["top_k"]["type"] == "integer"
        assert "query" in fn["parameters"]["required"]
        assert "top_k" not in fn["parameters"]["required"]
        assert "user" not in fn["parameters"]["required"]

    def test_all_schemas_serializable(self):
        import json
        json.dumps(global_tools.schemas(), ensure_ascii=False)  # 不抛异常即通过

    async def test_register_and_execute_with_deps(self):
        """自定义工具：依赖注入 + 参数过滤"""
        reg = ToolRegistry()

        async def my_tool(query: str, top_k: int = 3, user=None) -> dict:
            return {"q": query, "k": top_k, "user": getattr(user, "username", None)}

        reg.register(description="测试工具", dependencies=("user",))(my_tool)
        assert reg.get("my_tool") is not None
        assert "user" not in reg.get("my_tool").to_schema()["function"]["parameters"]["properties"]

        class _U:
            username = "alice"

        result = await reg.execute("my_tool", {"query": "x", "top_k": 7}, deps={"user": _U()})
        assert result == {"q": "x", "k": 7, "user": "alice"}

    async def test_execute_unknown_tool_raises(self):
        with pytest.raises(ValueError, match="未知工具"):
            await global_tools.execute("no_such_tool", {}, deps={})

    async def test_get_current_time_pure(self):
        """纯函数工具无需依赖"""
        result = await global_tools.execute("get_current_time", {}, deps={})
        assert "datetime" in result
        assert len(result["datetime"]) == 19  # YYYY-MM-DD HH:MM:SS


# ---------------- 工具参数解析 ----------------

class TestParseToolArguments:
    def test_dict_passthrough(self):
        assert LLMService._parse_tool_arguments({"query": "a"}) == {"query": "a"}

    def test_json_string(self):
        assert LLMService._parse_tool_arguments('{"query": "a", "top_k": 3}') == {
            "query": "a", "top_k": 3}

    def test_invalid_json_returns_empty(self):
        assert LLMService._parse_tool_arguments("{broken") == {}

    def test_none_returns_empty(self):
        assert LLMService._parse_tool_arguments(None) == {}


# ---------------- Agent 执行循环 ----------------

class TestAgentRun:
    @pytest.fixture()
    def run(self):
        return AgentRun(user=None, collection="default", max_iterations=3)

    async def test_no_tool_calls_direct_answer(self, run, monkeypatch):
        """模型直接回答（无工具调用）"""

        async def fake_chat_with_tools(messages, tools, temperature=None):
            assert messages[0]["role"] == "system"
            assert messages[0]["content"] == AGENT_SYSTEM_PROMPT
            assert messages[-1]["role"] == "user"
            assert tools  # 传入了工具 schema
            return LLMResponse("你好，我是知识库助手。")

        monkeypatch.setattr(run.llm, "chat_with_tools", fake_chat_with_tools)
        result = await run.run("你好")
        assert result["answer"] == "你好，我是知识库助手。"
        assert result["steps"] == []
        assert result["iterations"] == 1

    async def test_tool_call_executed_and_looped(self, run, monkeypatch):
        """第一次调用工具 get_current_time，第二次基于结果回答"""
        calls = []

        async def fake_chat_with_tools(messages, tools, temperature=None):
            calls.append(messages)
            if len(calls) == 1:
                return LLMResponse("", [LLMToolCall("get_current_time", {})])
            # 第二轮消息应包含 assistant tool_calls + tool 结果
            roles = [m["role"] for m in messages]
            assert "assistant" in roles and "tool" in roles
            tool_msg = [m for m in messages if m["role"] == "tool"][-1]
            assert "datetime" in tool_msg["content"]
            return LLMResponse("现在是 " + tool_msg["content"])

        monkeypatch.setattr(run.llm, "chat_with_tools", fake_chat_with_tools)
        result = await run.run("现在几点")
        assert len(result["steps"]) == 1
        step = result["steps"][0]
        assert step["tool_name"] == "get_current_time"
        assert step["duration_ms"] >= 0
        assert step["result"]["datetime"]
        assert result["answer"].startswith("现在是")
        assert result["iterations"] == 2

    async def test_tool_error_does_not_break_loop(self, run, monkeypatch):
        """工具不存在：错误结果回填，循环继续"""
        calls = []

        async def fake_chat_with_tools(messages, tools, temperature=None):
            calls.append(messages)
            if len(calls) == 1:
                return LLMResponse("", [LLMToolCall("no_such_tool", {"query": "x"})])
            return LLMResponse("工具出错了，但我继续回答了。")

        monkeypatch.setattr(run.llm, "chat_with_tools", fake_chat_with_tools)
        result = await run.run("测试")
        assert len(result["steps"]) == 1
        assert "error" in result["steps"][0]["result"]
        assert result["answer"] == "工具出错了，但我继续回答了。"

    async def test_max_iterations_reached(self, run, monkeypatch):
        """模型一直要调用工具 → 达上限后强制总结"""
        run.max_iterations = 2
        chat_called = False

        async def fake_chat_with_tools(messages, tools, temperature=None):
            return LLMResponse("", [LLMToolCall("get_current_time", {})])

        async def fake_chat(messages, temperature=None):
            nonlocal chat_called
            chat_called = True
            assert messages[-1]["role"] == "user"  # 上限提示消息
            return "时间有限，直接总结。"

        monkeypatch.setattr(run.llm, "chat_with_tools", fake_chat_with_tools)
        monkeypatch.setattr(run.llm, "chat", fake_chat)
        result = await run.run("查东西")
        assert chat_called
        assert result["iterations"] == 2
        assert len(result["steps"]) == 2
        assert result["answer"] == "时间有限，直接总结。"

    async def test_source_files_collected(self, run, monkeypatch):
        """从检索结果中收集来源文件名"""
        calls = []

        async def fake_chat_with_tools(messages, tools, temperature=None):
            calls.append(messages)
            if len(calls) == 1:
                return LLMResponse("", [LLMToolCall(
                    "search_knowledge_base", {"query": "阈值", "top_k": 2})])
            return LLMResponse("答案完成。")

        async def fake_execute(name, arguments, deps):
            assert deps["collection"] == "default"
            return {
                "collection": "default", "total": 1,
                "hits": [{"index": 1, "filename": "告警手册.pdf", "score": 0.9,
                          "content": "阈值 80%"}],
            }

        monkeypatch.setattr(run.llm, "chat_with_tools", fake_chat_with_tools)
        monkeypatch.setattr(global_tools, "execute", fake_execute)
        result = await run.run("告警阈值是多少")
        assert result["source_files"] == ["告警手册.pdf"]
        assert result["steps"][0]["tool_name"] == "search_knowledge_base"
        assert result["steps"][0]["arguments"] == {"query": "阈值", "top_k": 2}

    def test_assistant_tool_message_json_arguments(self, run):
        """回填消息的 arguments 必须为 JSON 字符串（OpenAI 规范）"""
        resp = LLMResponse("", [LLMToolCall("search_knowledge_base", {"query": "阈值"})])
        msg = run._assistant_tool_message(resp)
        assert msg["role"] == "assistant"
        tc = msg["tool_calls"][0]["function"]
        assert tc["name"] == "search_knowledge_base"
        assert isinstance(tc["arguments"], str)
        import json
        assert json.loads(tc["arguments"]) == {"query": "阈值"}

    def test_summarize_result_truncates(self, run):
        long = {"data": "x" * 3000}
        assert len(run._summarize_result(long)) == 1500
        assert run._summarize_result("plain") == "plain"


# ---------------- 对话持久化中的工具步骤 ----------------

class TestConversationToolSteps:
    def test_message_roundtrip(self):
        from app.core.conversation import Message

        steps = [{"tool_name": "get_current_time", "arguments": {}, "result": {}, "duration_ms": 5}]
        msg = Message(role="assistant", content="答案", tool_steps=steps)
        assert msg.to_dict()["tool_steps"] == steps
