# ═══════════════════════════════════════════════════════════════════
# app.graph — LangGraph 多 Agent 编排层（M19）
#
# 与 app.services 严格解耦：服务实例由 main.py DI 容器注入，
# 本包只负责"图编排"，不直接创建 LLM/向量库连接。
# ═══════════════════════════════════════════════════════════════════
from .contract_graph import (
    ContractState,
    build_contract_graph,
    run_contract_graph,
    run_linear_chain,
    MAX_GRAPH_RETRIES,
)

__all__ = [
    "ContractState",
    "build_contract_graph",
    "run_contract_graph",
    "run_linear_chain",
    "MAX_GRAPH_RETRIES",
]
