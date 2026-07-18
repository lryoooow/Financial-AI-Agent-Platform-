"""deepagents 链路（对话优先落地，研究管线后续迁移）。

与 agents/graph.py 的固定管线并存：由 settings.chat_engine 决定走哪条。
老管线在新链路验证通过前保持可用，不删。
"""
