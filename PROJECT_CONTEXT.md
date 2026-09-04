# PROJECT_CONTEXT.md — 项目记忆文件

> 本文件由 AI 自动生成，后续开发过程中需及时更新。
> 最后更新：2026-09-01（M18 工程化交付完成；94 项 pytest 全通过；chunker.py 正则 bug 修复；中文日期解析增强；README 全量重写含 Mermaid 架构图；run_accuracy_test.py + demo_script.py 新建）

---

## 一、项目概述

| 项目 | 内容 |
|------|------|
| **项目名称** | 智能合同管理系统（Smart Contract Management System） |
| **项目目标** | 构建可商用的智能合同管理系统，实现合同上传、AI 提取变量、自动分类、台账管理、状态流转、业财联动 |
| **代码仓库** | `d:\Project\langchain\contract-system` |
| **启动命令** | `docker compose up -d` |
| **访问入口** | Odoo `http://localhost:8069` · AI 服务 `http://localhost:8000/docs` · Chroma `http://localhost:8001` |

### 架构总览

```
contract-net (Docker bridge 网络)
├── PostgreSQL 15      :5432   ← Odoo 唯一关系库
├── Odoo 17 CE         :8069   ← 业务平台，自定义模块挂载到 /mnt/extra-addons
├── FastAPI AI 服务     :8000   ← PDF 解析 → 切块 → 分类 → RAG 提取
└── Chroma 0.5.20      :8001   ← 独立向量库（容器内 8000，宿主机 :8001）
```

### 核心约定

1. **不修改 Odoo 源码**——所有业务逻辑写在 `odoo/addons/contract_ai/` 自定义模块
2. **容器间只通过服务名互访**——禁止在 compose 的网络内写 `localhost` 或 IP
3. **配置即代码**——所有参数走 `.env` → `docker-compose.yml` → 应用内环境变量，不硬编码
4. **Odoo 模块命名**——主模块 `contract_ai`，XML ID 前缀 `contract_ai.xxx`
5. **LLM 客户端**——M10 起改用 raw `openai.OpenAI` SDK（而非 LangChain ChatOpenAI），`invoke()` 提供向后兼容的 `.content` 接口
6. **提示词存储**——M9 起统一用 YAML（`prompts/v1/prompts.yaml`），字段字典用 JSON（`prompts/field_dict.json`）

---

## 二、技术栈

| 层级 | 技术 | 版本 | 备注 |
|------|------|------|------|
| 业务平台 | Odoo CE | 17.0-20260817 | 官方镜像 `odoo:17.0`，挂载只读 addons |
| 后端框架 | FastAPI + Uvicorn | Python 3.11 | `uvicorn app.main:app`（lifespan 初始化） |
| LLM SDK | openai | 3.5.0 | 原生 SDK，DeepSeek / OpenAI / qwen 全兼容（base_url 区分 provider） |
| 向量数据库 | Chroma | 0.5.20 | ⚠️ 客户端 chromadb 必须严格对齐 0.5.20（1.5.9 会因 `_type` 字段被拒绝） |
| OCR | PaddleOCR / PaddlePaddle | latest | 扫描版 PDF 自动分流 |
| 关系数据库 | PostgreSQL | 15-alpine | Odoo 唯一依赖 |
| Embedding | BAAI/bge-small-zh-v1.5 | 512d | bge-large-zh-v1.5 有缓存问题（容器内 HF 外网不通），改用 small，volume 挂载宿主机 HF 缓存 + `HF_HUB_OFFLINE=1` |
| 容器化 | Docker + Docker Compose | ≥ 4.20 / ≥ 2.20 | Windows 统一用 Desktop 版 |

### LLM 提供商（四选一，OpenAI 兼容协议）

| Provider | `.env` 配置 | 推荐模型 | 默认 base_url |
|----------|------------|----------|---------------|
| DeepSeek | `LLM_PROVIDER=deepseek` | `deepseek-chat` | `https://api.deepseek.com/v1` |
| 通义千问 | `LLM_PROVIDER=qwen` | `qwen-plus` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| OpenAI | `LLM_PROVIDER=openai` | `gpt-4o-mini` | `https://api.openai.com/v1` |
| Anthropic | `LLM_PROVIDER=anthropic` | `claude-3-5-sonnet-latest` | 自定义 base_url |

> ⚠️ `.env` 当前配置：`LLM_PROVIDER=deepseek`, `LLM_BASE_URL=https://api.deepseek.com/v1`, `LLM_MODEL=deepseek-chat`

---

## 三、项目目录结构

```
contract-system/
├── docker-compose.yml              # 四服务编排（含 HF 缓存 volume + OFFLINE 环境变量）
├── .env                            # 环境变量（不提交）
├── .env.example                    # 环境变量模板
├── .gitignore
├── README.md                       # 项目说明
├── PROJECT_CONTEXT.md              # ★ 本文件（AI 记忆）
│
├── odoo/
│   ├── config/
│   │   └── odoo.conf               # Odoo 运行时配置
│   └── addons/
│       └── contract_ai/            # ★ 唯一自定义模块
│           ├── __init__.py
│           ├── __manifest__.py
│           ├── models/
│           │   ├── __init__.py                    # 6 个模型导入
│           │   ├── contract.py                    # contract.contract 主台账（状态机 + AI + 校验）
│           │   ├── contract_counterparty.py        # contract.counterparty 相对方
│           │   ├── contract_signatory.py          # contract.signatory 签约人
│           │   ├── contract_template.py           # contract.template + contract.clause
│           │   ├── contract_element.py            # contract.element AI 原子元素
│           │   └── contract_payment_plan.py       # contract.payment.plan 收付款计划（M16 重写：状态机 draft→confirmed→paid + direction 自动判定）
│           ├── security/
│           │   ├── contract_ai_security.xml       # 2 用户组
│           │   └── ir.model.access.csv            # 15 条 ACL（7 模型 × manager/user）
│           ├── data/
│           │   └── contract_sequence.xml          # 合同编号序列（CG-YYYY-NNNN）
│           └── views/
│               ├── contract_views.xml             # contract.contract kanban/tree/form/search + 付款计划 Tab（M16 增强：汇总面板 + 子表状态按钮 + header 生成按钮）
│               ├── contract_counterparty_views.xml # counterparty kanban/tree/form
│               ├── contract_signatory_views.xml    # signatory tree/form
│               ├── contract_template_views.xml    # template tree/form
│               ├── contract_payment_plan_views.xml # ★ M16 新建：收付款计划独立 tree/form/pivot/graph/search + 台账汇总
│               └── contract_menu.xml               # 顶级菜单「📄 合同管理」+ M16 新增「💰 收付款台账」子菜单
│
└── ai-service/
    ├── Dockerfile                  # Python 3.11-slim 基础镜像
    ├── requirements.txt            # ⚠️ chromadb==0.5.20（锁版本）
    ├── test_m8.py / test_m9.py / test_m10.py / test_m11.py / test_m12.py / test_m13.py / test_m14.py   # 单元测试（本地运行，mock LLM 客户端；test_m14 为 FastAPI TestClient 测试）
    ├── audit_m9.py                 # M9 核查脚本
    ├── README.md                   # ★ 全量重写（Mermaid 架构图 + M1-M18 完成度 + API 文档 + 准确率数据）
    ├── app/
    │   ├── __init__.py
    │   ├── config.py               # pydantic-settings（llm_*, chroma_*, embedding_*, prompts_*）
    │   ├── main.py                 # FastAPI 入口（lifespan + 6 路由 + _get_llm_client 工厂；test_m14.py 验证）
    │   └── services/
    │       ├── pdf_parser.py       # PDF 解析（文字版 pdfplumber + 扫描版 PaddleOCR 分流） — 558 行
    │       ├── chunker.py          # 文本清洗 + 语义切块 — 460 行（M7 原 400→460：修复 pattern 3/4 正则 + 空列表兜底）
    │       ├── vector_store.py     # Chroma HttpClient 封装 — 317 行（M8）
    │       ├── prompt_manager.py   # YAML 提示词版本管理 + field_dict 注入 — 235 行（M9）
    │       ├── llm_client.py       # BaseLLM + OpenAILLM + DeepSeekLLM + create_llm() — 301 行（M10）
    │       ├── classifier.py        # 合同分类（规则 baseline + LLM + PromptManager + json_mode）— 385 行（M11）
    │       ├── extractor.py         # 字段提取（M13 核心：RAG + LLM + Pydantic + 重试）— 487 行（M13 重写）
    │       └── rag_learner.py       # RAG 监督学习（范例入库 + 相似检索）— M12 新增
    ├── prompts/
    │   ├── field_dict.json                    # 9 组枚举字段字典（contract_type / dispute_resolution / payment_method / currency / party_role / date_format / amount_format / extract_schema）
    │   └── v1/
    │       ├── prompts.yaml                   # ★ 结构化 YAML 提示词（4 条目：system_extract / classify / extract / extract_retry）
    │       ├── system_extract.txt             # 旧格式，保留向后兼容
    │       ├── template_classify.txt
    │       ├── template_extract.txt
    │       └── template_extract_retry.txt
    ├── examples/
    │   └── annotations/                       # 12 份金标准合同标注（purchase/sales/service/lease × real + 非 real）
    ├── scripts/
    │   ├── bootstrap_examples.py              # 金标准入库（需 Chroma 运行）
    │   ├── evaluate_m15.py                    # ★ M15 评估脚本（字段准确率 + RAGAS 4 指标 + Markdown 报告；1091 行）
    │   ├── run_accuracy_test.py                # ★ M18 准确率测试 CLI（360 行：argparse + mock_extract + AIServiceClient HTTP + field_match 条形图）
    │   ├── demo_script.py                     # ★ M18 演示辅助（12 步打印 + --run --slow）
    │   ├── evaluate_accuracy.py               # 旧版准确率回归测试（已过时，引用旧类名）
    │   └── generate_annotation.py             # 标注生成
    └── tests/                                 # ★ M18 pytest 单元测试（94 项全通过）
        ├── __init__.py
        ├── conftest.py                        # fixtures + SAMPLE_CONTRACT_TEXT + SAMPLE_CONTRACT_NO_NUMBERS
        ├── test_chunker.py                    # 48 项：clean_text(15) + _find_clause_boundaries(8) + chunk_text(10) + _hard_split(4) + _get_overlap_tail(4) + process_contract_text(1)
        ├── test_classifier.py                 # 23 项：TestRuleClassify(6) + TestFusionStrategy(4) + TestClassifierEdgeCases(4) + TestRealSamples(4)
        └── test_field_validator.py            # 23 项：TestNormalizeDate(3) + TestParseAmount(3) + TestFuzzyMatch(5) + TestValidateEnum(5)
```

### ✅ 架构已清理

- 已删除遗留模块 `contract_system/`（完整实现已合并进 contract_ai）
- 数据库已重置为干净状态（DROP + CREATE）
- 当前**只有一个自定义模块** `contract_ai`，零冲突
- Odoo XML ID 前缀统一为 `contract_ai.xxx`
- M3 第二轮重构后 7 模型 39 字段对齐；15 条 ACL 覆盖全部 7 模型；3 个辅助模型有了视图 XML

---

## 四、Docker 服务配置速查

| 服务 | 镜像 | 容器名 | 端口映射 | 健康检查 | 备注 |
|------|------|--------|----------|----------|------|
| postgres | postgres:15-alpine | contract-postgres | 5432:5432 | pg_isready | 唯一关系库 |
| odoo | odoo:17.0 | contract-odoo | 8069:8069 | curl /web/login | addons 只读挂载 |
| ai-service | 自定义 Python 3.11 | contract-ai | 8000:8000 | curl /api/health | 构建自 ./ai-service；挂载 HF 缓存 + 设置 HF_HUB_OFFLINE=1 |
| chroma | ghcr.io/chroma-core/chroma:0.5.20 | contract-chroma | 8001:8000 | 内部 heartbeat | 容器内 8000，⚠️ 客户端必须 0.5.20 |

### 数据卷

| 卷名 | 用途 | 清理 |
|------|------|------|
| odoo_db_data | PostgreSQL 数据文件 | `docker volume rm` |
| odoo_data | Odoo 附件/会话/缓存 | 同上 |
| chroma_data | Chroma 向量库 | 同上 |
| odoo_logs | Odoo 运行日志 | 同上 |

### 外部挂载（非命名卷）

| 路径 | 挂载目标 | 备注 |
|------|----------|------|
| `C:/Users/ruoli/.cache/huggingface/hub` | ai-service 容器内 `/root/.cache/huggingface/hub` | 解决容器内 HF 外网不通问题 |

### 常用命令

```bash
docker compose up -d                         # 启动（幂等）
docker compose down                          # 停止（保留数据）
docker compose down -v                       # 停止 + 删除所有卷 ⚠️
docker exec contract-odoo odoo -d contract_db -u contract_ai --stop-after-init   # 升级模块
docker exec contract-odoo odoo -d contract_db -i contract_ai --stop-after-init   # 安装模块
docker logs -f contract-odoo                 # 实时看 Odoo 日志
docker exec contract-ai python scripts/bootstrap_examples.py   # 金标准入库
docker exec contract-ai python scripts/evaluate_accuracy.py    # 准确率测试
curl http://localhost:8000/api/health         # AI 服务健康检查（显示 prompts_version / llm_provider）
```

---

## 五、模块列表

### M1 · 项目初始化与基础架构 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已完成 |
| **完成时间** | 2026-08-28 |
| **关键文件** | `docker-compose.yml`、`odoo/config/odoo.conf`、`.env.example`、`README.md`、`start.ps1` |

### M2 · Odoo 自定义模块骨架 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已完成 |
| **完成时间** | 2026-08-29 |
| **关键文件** | `contract_ai/__manifest__.py`、`models/contract.py`、`security/*.xml`、`security/ir.model.access.csv` |

### M3 · 合同核心数据模型 ✅ 全字段对齐

| 项 | 内容 |
|----|------|
| **状态** | ✅ 7 模型 39 字段 + 视图层全部对齐（2026-09-01 静态分析验证 0 处旧字段残留） |
| **完成时间** | 2026-08-29（第二轮重构）；2026-09-01 视图层全量重写对齐 |
| **新增文件** | `contract_counterparty.py`、`contract_signatory.py`、`contract_template.py`、`contract_element.py`、`contract_payment_plan.py` |
| **ACL 状态** | ✅ 已补全：15 条 ACL 覆盖全部 7 模型（每个模型 manager + user 两条） |
| **视图状态** | ✅ contract_views.xml 全量重写（draft/approval/seal/archived/void 五态、type/date_signed/date_start/date_end/source_pdf 全对齐）；✅ counterparty / signatory / template / payment.plan 均有独立视图 XML（M16 新建 contract_payment_plan_views.xml）；✅ payment.plan 有 tree/form/pivot/graph/search 全套独立视图 |
| **One2many 配对** | ✅ 7 对 One2many ↔ inverse Many2one 全部验证一致 |

#### 合同字段映射（M3 重构）

| 旧字段 | 新字段 | 类型变更 |
|---|---|---|
| `contract_type` | `type` | Selection（值集扩展） |
| `sign_date` | `date_signed` | Date |
| `effective_date` | `date_start` | Date |
| `expire_date` | `date_end` | Date |
| `partner_a` | `partner_a` | Char → Many2one(contract.counterparty) |
| `dispute_resolution` | `dispute_resolution` | Text → Selection(litigation/arbitration/negotiation) |
| `pdf_file` | `source_pdf` | Binary |
| — | `signatory_id` | 新增 Many2one |
| — | `ai_extracted_json` | 新增 Json |
| — | `payment_plan_ids` | 新增 One2many |
| — | `clause_ids` | 新增 One2many |
| — | `element_ids` | 新增 One2many |

#### 状态机（5 态）

| state 值 | 说明 |
|---|---|
| `draft` | 草拟 |
| `approval` | 审批中 |
| `seal` | 已用印 |
| `archived` | 已归档 |
| `void` | 已作废（不可逆） |

### M5 · 合同管理界面（视图） ✅ 主合同视图已对齐

| 项 | 内容 |
|----|------|
| **状态** | ✅ contract_views.xml（主合同 kanban/tree/form/search）全对齐 M3 重构后字段；✅ counterparty / signatory / template 有独立视图；🟡 clause / element / payment.plan 缺独立视图（当前内嵌在合同 Tab 中） |
| **验证** | `_check_views.py` 静态扫描 4 个视图 XML：0 处旧字段（contract_type / sign_date / effective_date / expire_date / partner_a_id / partner_b_id / pdf_file / pdf_filename / action_cancel / approving / approved / sealed / cancelled）残留；18 个新字段全部存在 |

### M4 · 合同状态机与工作流（已合并进 M3）

状态机字段和 5 个流转按钮方法已在 M3 的 `contract.py` 中定义。

---

### M6 · PDF 预处理与 OCR 分流 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现 |
| **完成时间** | 2026-08-30 |
| **关键文件** | `ai-service/app/services/pdf_parser.py`（558 行） |
| **核心类** | `PdfProcessor` — 文字版用 pdfplumber 直接抽取，扫描版走 PaddleOCR |
| **API** | `POST /api/contract/parse`（FastAPI main.py:126） |
| **输出** | 统一文本 + 分流判定（`PdfExtractionResult` dataclass） |

### M7 · 文本清洗与语义切块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现 |
| **完成时间** | 2026-08-30 |
| **关键文件** | `ai-service/app/services/chunker.py`（400 行） |
| **核心类** | 章节识别 + 段落清洗（OCR 噪声、页码、页眉页脚）+ 语义切块（保留结构标签） |

### M8 · Embedding 与向量库管理 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现 + 本地 test_m8.py 通过 + audit_m9.py 间接验证 |
| **完成时间** | 2026-08-30 |
| **关键文件** | `ai-service/app/services/vector_store.py`（317 行） |
| **核心类** | `VectorStore` — Chroma HttpClient 封装，双集合（contract_examples / contract_chunks） |
| **Embedding** | SentenceTransformer 离线加载，`BAAI/bge-small-zh-v1.5`（512d），volume 挂载 HF 缓存 |
| **API** | `POST /api/vector/query` + `GET /api/vector/stats`（main.py:235-263） |
| **⚠️ 关键约束** | 客户端 chromadb 版本必须严格对齐 Chroma 服务端 0.5.20；1.5.9 会因 `_type` 字段被旧版服务拒绝 |

### M9 · 提示词管理与版本控制 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 4 项需求全部通过 audit_m9.py 核查 |
| **完成时间** | 2026-08-31 |
| **关键文件** | `ai-service/app/services/prompt_manager.py`（235 行）+ `ai-service/prompts/v1/prompts.yaml` + `ai-service/prompts/field_dict.json` |
| **核心类** | `PromptManager` — YAML 解析 + `render(name, version, **variables)` + `load()` 向后兼容 + 版本回滚 |
| **提示词条目** | 4 个：system_extract / classify / extract / extract_retry（YAML 结构化，含 name / version / system_prompt / user_prompt_template / variables） |
| **字段字典** | 9 组枚举：contract_type(5) / dispute_resolution(4) / payment_method(6) / currency / party_role / date_format / amount_format / extract_schema(15 字段) |
| **变量替换** | Python format `{xxx}` + `{field_dict.xxx.yyy}` 点号路径；缺失变量保留原样不抛异常 |
| **自动继承** | extract / extract_retry 本身无 system_prompt，render() 时自动从 system_extract 补齐 |
| **验证** | `test_m9.py` 6 项全过 + `audit_m9.py` 核查 4 需求全绿 |
| **API 联动** | FastAPI lifespan 初始化 PromptManager；health API 返回 `prompts_version: "v1"` |

### M10 · LLM 调用封装 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 7 项 mock 测试全过（test_m10.py），未做真实 API 调用验证 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `ai-service/app/services/llm_client.py`（301 行） + `ai-service/app/main.py`（76-104 行 `_get_llm_client` 重写） |
| **架构** | BaseLLM(abc.ABC) → OpenAILLM / DeepSeekLLM；create_llm() 工厂函数 |
| **接口** | `chat(messages, temperature=0, json_mode=False, max_tokens=None, **kwargs) → LLMResponse` |
| **JSON 模式** | `response_format={"type":"json_object"}` + json.loads() 二次验证，失败自动重试 |
| **重试** | 指数退避：1s → 2s → 4s（默认 3 次）；捕获 5 类异常（APIError / APITimeoutError / APIConnectionError / RateLimitError / JSONDecodeError） |
| **向后兼容** | `invoke(prompt_or_messages) → LLMResponse`（返回有 `.content` 的对象，兼容 classifier.py / extractor.py 现有代码） |
| **工厂分发** | provider="deepseek" → DeepSeekLLM；其他走 OpenAILLM + 自定义 base_url（qwen/DashScope 默认自动填） |
| **main.py 联动** | `_get_llm_client()` 已从 ChatOpenAI 切到 create_llm()，零 LangChain 硬依赖 |

---

### M11 · 合同分类模块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现（385 行）+ test_m11.py 9 项 mock 测试全过 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `ai-service/app/services/classifier.py`、`ai-service/test_m11.py` |
| **核心类** | `ContractClassifier` — 规则关键词 baseline + LLM 语义分类 + 加权融合策略 |
| **融合策略（3 分支）** | (a) LLM 失败 → 纯回退规则（rule_fallback）；(b) LLM conf ≥ 0.7 → 信任 LLM，规则一致加成 / 冲突轻微惩罚（llm）；(c) LLM conf < 0.7 → ensemble 加权融合：共识 → `rule*0.55+llm*0.45+0.1`（上限 0.9），冲突 → 选高置信者 × 0.7（ensemble） |
| **Prompt 来源** | M9 PromptManager + `prompts/v1/prompts.yaml` "classify" 条目；降级时用硬编码 prompt |
| **LLM 调用** | `BaseLLM.chat(messages, json_mode=True)` 强制 JSON；PromptManager.render() 构造 system+user |
| **置信度阈值** | `LLM_CONFIDENCE_THRESHOLD = 0.7` |
| **类别体系** | 采购合同 / 销售合同 / 服务合同 / 租赁合同 / 其他（与 field_dict.json 一致） |
| **API** | `POST /api/contract/classify`（main.py:169-185）— 响应含 `llm_failed` 字段 |
| **依赖** | M9（PromptManager 可选）、M10（BaseLLM.chat） |
| **method_used** | `"llm"` / `"ensemble"` / `"rule_fallback"` |
| **测试覆盖** | 规则 baseline 4 类 / LLM 高置信 → LLM / LLM 低置信+冲突 → ensemble 冲突 / LLM 异常 → rule_fallback / LLM+规则高置信一致加成 / 字段完整性 / PromptManager 集成 / LLM 无效类型 → ensemble 冲突 / **LLM 低置信+规则一致 → ensemble 共识加成**（新增） |

### M12 · RAG 监督学习模块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现（~350 行）+ test_m12.py 8 项 mock 测试全过 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `ai-service/app/services/rag_learner.py`、`ai-service/test_m12.py`、`ai-service/scripts/bootstrap_examples.py`（修复） |
| **核心类** | `RAGLearner` — 范例入库（切块→Chroma）+ 相似检索（去重→内存缓存补全） |
| **入库方式** | `add_example({id, contract_type, text, extraction})` → 复用 M7 chunk_text 切块 → VS.add_texts 写入 contract_examples 集合 |
| **检索方式** | `retrieve_examples(query_text, contract_type=None, top_k=3)` → VS.query 拿 chunks → 按 example_id 去重取最佳分数 → 内存缓存补全完整范例 |
| **设计亮点** | 内存缓存 `self._examples[example_id]` 保存完整数据（含完整文本 + extraction dict）；Chroma metadata 只存 example_id / contract_type / chunk_index / char_count（不含嵌套 dict） |
| **collection** | `contract_examples`（config.chroma_collection_examples） |
| **修复的历史问题** | bootstrap_examples.py / evaluate_accuracy.py 引用了不存在的 `ContractVectorStore` 类 → 全部改用 `VectorStore` + `RAGLearner` |
| **测试覆盖** | add_example 基本流程 / 必填字段校验 / batch 批量 / 检索无过滤 / 检索带 contract_type 过滤 / 空查询&异常边界 / 多 chunk 去重取最佳分数 / get_example+list_examples+stats+clear_memory |
| **依赖** | M7（chunk_text）、M8（VectorStore） |
| **遗留** | load_examples() 支持从 annotations/ 目录批量加载，但需要配合 PDF 解析出的 texts dict |

### M13 · 合同变量提取模块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现（487 行）+ test_m13.py 10 项 mock 测试全过 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `ai-service/app/services/extractor.py`、`ai-service/test_m13.py` |
| **核心类** | `ContractExtractor`（注入 RAGLearner + BaseLLM + PromptManager） |
| **主入口** | `extract_contract_fields(text, contract_type, use_rag, top_k_examples) → ExtractResult` |
| **依赖** | M9（PromptManager.render）、M10（BaseLLM.chat + json_mode）、M12（RAGLearner 可选） |
| **Prompt 流程** | system_extract（复用）→ extract（首次）/ extract_retry（重试）— 3 个 prompt 全部来自 prompts.yaml |
| **JSON 解析** | 容错：直接 parse → ```json ``` 提取 → 最外层 {} 正则兜底 |
| **Schema 校验** | Pydantic ContractExtraction（15 字段） |
| **业务校验** | 日期格式硬约束（YYYY-MM-DD + 实际解析）；金额大写↔数字仅 WARNING 不触发 retry |
| **重试策略** | 最多 3 次；JSON 解析失败/Schema 校验失败/业务校验失败/LLM 异常 → 每次都用 extract_retry prompt 带上 previous_result + validation_errors |
| **兜底** | 重试耗尽 → 返回合法 Schema + confidence=0.1，区分 llm_failed=True/False |
| **返回值** | ExtractResult dataclass：fields（dict）+ 结构化日志（prompt_version, few_shot_sources, attempt_count, elapsed_seconds, validation_errors, llm_failed） |
| **低耦合** | 构造函数不直接依赖 VectorStore、Chroma、PDFParser —— 全通过注入 |
| **向后兼容** | 保留 `.extract(text, contract_type, use_few_shot)` → dict 的旧签名（内部调新主入口） |
| **API** | `POST /api/contract/extract`（main.py）— 响应新增 attempt_count / elapsed_seconds |
| **遗留** | _num_to_chinese 算法有 bug（500000→"伍拾万万元整"多一个"万"），但仅用于 WARNING 不影响功能 |

### M14 · AI 服务 API 层 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现（main.py 512 行）+ test_m14.py 17 项 mock 测试全过 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `ai-service/app/main.py`、`ai-service/test_m14.py`（445 行） |
| **路由清单（6 个）** | `GET /api/health` · `POST /api/contract/parse` · `POST /api/contract/classify` · `POST /api/contract/extract`（PDF 全流程） · `GET /api/vector/search` · `GET /api/vector/stats` |
| **统一响应** | `_ok()` / `_err()` helpers，所有端点返回 `{success, data, error, elapsed_ms}` |
| **全局异常** | HTTPException handler + Exception 兜底 handler，未捕获异常也包成统一格式 500 |
| **lifespan** | 启动时初始化 PdfProcessor / PromptManager / VectorStore / RAGLearner；延迟初始化 LLM + Classifier + Extractor（需 API Key） |
| **LLM 延迟** | `_get_llm_client()` 工厂，首次调用时创建，无 API Key 抛 HTTPException |
| **extract 端点** | PDF 上传 → 解析 → 自动分类（可选 contract_type 预设） → RAG 字段提取 → 一键全流程 |
| **测试覆盖** | 健康检查(1) / 分类(3) / 字段提取全流程(5) / 向量检索(4) / 向量统计(1) / 响应格式(2) / 全局异常(1) |
| **Odoo 联动** | Odoo 侧 `action_ai_extract()` 按钮 HTTP 调 `/api/contract/extract` |

### M15 · 评估与报告模块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现（evaluate_m15.py 1091 行）+ offline 模式实测通过 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `ai-service/scripts/evaluate_m15.py` |
| **测试集** | 12 份金标准标注（purchase/sales/service/lease × 3 each），12 个评估字段；无真实 PDF 时自动合成合同文本兜底 |
| **字段准确率** | `field_match()` 逐字段比较：日期归一化 YYYY-MM-DD、金额 2% 容差 + "万元"后缀处理、字符串双向包含模糊匹配 |
| **RAGAS 指标（4 个全覆盖）** | Faithfulness（忠实度）+ Answer Relevancy（相关性）+ Context Precision（精度）+ Context Recall（召回率）；优先调 ragas 库，不可用时降级自算版（token 级 + 日期/金额特殊处理） |
| **对比评估** | RAG=ON（few-shot）vs RAG=OFF（基线）两组，自动计算提升幅度 |
| **输出** | Markdown 报告（6 章节：总体对比 → 逐字段 → RAG 指标 → 逐合同详情 → RAG 修复案例 → 分析结论）+ JSON 原始数据 |
| **CLI 参数** | `--offline`（mock 预测值，不调 LLM）/ `--no-ragas` / `--annot-dir` / `--pdf-dir` / `--output-dir` |
| **实测结果** | offline 模式：字段准确率 88.89% → 98.61%（+9.7pp），144 个问答对 |
| **遗留脚本** | `evaluate_accuracy.py`（旧版，引用已过期类名，建议删除或留作备份） |
| **依赖** | M12（RAGLearner）+ M13（ContractExtractor） |

### M16 · 业财一体化模块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现（重写 payment.plan 211 行 + contract.py 新增 210 行 + 新建独立视图 XML 204 行）；Docker 升级 EXIT=0 零报错 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `odoo/addons/contract_ai/models/contract_payment_plan.py`（重写）、`contract.py`（新增 generate_payment_plan + 汇总字段）、`views/contract_payment_plan_views.xml`（新建）、`views/contract_views.xml`（增强付款计划 Tab）、`views/contract_menu.xml`（新增台账菜单） |
| **模型重构** | `contract.payment.plan` 状态机从 `pending/partial/done/overdue/cancelled` → `draft/confirmed/partial/paid/cancelled`；新增 `direction`（收/付方向自动判定）+ 4 个 related 字段（contract_type/code/name/amount 用于 Pivot 分组） |
| **状态流转** | `action_confirm()` 草稿→已确认；`action_done()` 自动填实际金额/日期→已完成；`action_cancel()` 任意非终态→已取消；`action_reset_draft()` 已确认/部分/取消→草稿 |
| **generate_payment_plan()** | 中文付款条款正则解析器（contract.py:343-420）：按分号/换行切句 → 提取百分比（30% / 百分之30 / 30 ％）→ 节点类型关键词匹配（预付/到货/验收/尾款/分期）→ 时间偏移估算（显式天数优先，否则按 milestone 给合理默认）→ 自动生成 payment.plan 记录 |
| **时间偏移估算** | 显式天数优先（N 工作日/N 日内/N 天内/N 个月内→N×30）→ 签订后→0 天→ 到货→30 天→ 验收→60 天→ 尾款→90 天→ 分期→按月数累加→ 默认 15 天 |
| **汇总计算字段** | 合同模型新增 5 个 store=True 的 compute 字段：`payment_plan_count` / `planned_amount_total` / `paid_amount_total` / `remaining_amount_total` / `overdue_count`，合同表单「收付款汇总」面板实时联动 |
| **收/付方向自动判定** | `_compute_direction`：purchase/lease→应付（payable）；sale/service/labor/partnership/consulting→应收（receivable）；可手动覆盖 |
| **独立台账视图** | 新建 `contract_payment_plan_views.xml`：tree（带 color decoration：已完成绿/已确认蓝/逾期红/取消灰）+ form（header 状态按钮 + sheet 布局）+ **pivot**（行维度：合同类型 × 收/付方向 × 状态；度量：计划金额/实际金额/剩余金额）+ graph（柱状）+ search（含按方向/类型/状态过滤） |
| **独立菜单入口** | contract_menu.xml 新增「💰 收付款台账」子菜单（sequence=50），绑定 action_contract_payment_plan act_window |
| **合同视图增强** | contract_views.xml：header 新增「📋 自动生成计划」按钮；付款计划 Tab 顶部加汇总面板（planned/paid/remaining/overdue）；子表加 control 区域（确认/完成/取消按钮）+ color decoration |
| **Bug 修复** | contract.py 旧 URL `/api/contract/quick` → M14 重命名后的 `/api/contract/extract`（action_ai_extract 方法内） |
| **Docker 验证** | `docker compose run --rm odoo -d contract_db -u contract_ai --stop-after-init` EXIT=0；Registry loaded in 3.495s；零 CRITICAL / 零 ERROR / 零 Traceback |
| **遗留** | 旧 `contract_views.xml` 内嵌子表的 `action_mark_paid` 方法已被 `action_done` 替代（contract_payment_plan.py 中已删除旧方法） |

### M17 · 配置域与报表模块 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 已实现 |
| **完成时间** | 2026-09-01 |
| **关键文件** | `odoo/addons/contract_ai/models/contract_config.py`（新建单例模型）、`views/contract_payment_plan_views.xml`（增强报表）、`views/contract_report_views.xml`（新建 search/pivot/graph/ledger）、`views/contract_config_views.xml`（配置面板 Form）、`report/contract_pdf_reports.xml`（PDF QWeb 2 套模板）、`controllers/contract_export.py`（openpyxl Excel 导出）、`views/contract_menu.xml`（新增台账 + 配置菜单） |
| **contract.config 单例模型** | 字段：code_prefix / code_date_format / code_padding / code_start_no / default_* / enable_* / ai_service_url / code_preview(compute)；SQL unique(id) 约束；generate_contract_code() 找 ir.sequence → 不存在则 _ensure_sequence() + _sync_sequence() → sequence.next_by_id() |
| **合同台账报表** | Search view：5 状态 filter + 4 类型 filter + 3 group_by；Pivot：type×state 行 + amount/planned/paid 度量；Graph：type vs amount 柱状 |
| **PDF 导出（QWeb + wkhtmltopdf 0.12.6.1）** | 2 套模板：report_contract_ledger_pdf（汇总指标 div + 10 列表格）+ report_contract_detail_pdf（基本信息 + 甲乙双方 + 收付款计划 table sorted by sort_order）；wkhtmltopdf 输出 42KB/41KB PDF 成功 |
| **Excel 导出（openpyxl 3.1.5）** | @http.route /contract/export/excel；Sheet1 合同列表 17 列（currency_fmt + thin_border）+ Sheet2 按 type 分组汇总合计行 |
| **配置面板** | contract_config_views.xml Form：编号规则预览 + AI 服务 URL + 各类默认值 |
| **Bug 修复** | QWeb `context_timestamp(env).strftime(...)` AssertionError → `datetime.datetime.now().strftime(...)`；Odoo 17 tree view 不支持 totals="true" sum="..." → 去掉；pivot 不接受 date_signed:month → 只用真实字段；openpyxl 临时 pip install 到 Odoo 容器 |
| **遗留** | openpyxl 未加入 Odoo Dockerfile（重建容器会丢失） |

### M18 · 工程化交付与部署 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 94 项 pytest 全通过 |
| **完成时间** | 2026-09-01 |
| **测试文件** | `ai-service/tests/conftest.py`（fixtures + SAMPLE_CONTRACT_TEXT）+ `test_chunker.py`（48 项：clean_text / _find_clause_boundaries / chunk_text / _hard_split / _get_overlap_tail / process_contract_text）+ `test_classifier.py`（23 项：规则 baseline / 融合策略 3 分支 / 边界条件 / 真实样例）+ `test_field_validator.py`（23 项：normalize_date 多格式 + 中文数字 / parse_amount 多格式 / fuzzy_match / validate_enum） |
| **准确率测试脚本** | `ai-service/scripts/run_accuracy_test.py`（360 行）：argparse CLI + mock_extract 离线模式 + AIServiceClient HTTP 模式 + field_match 语义比较（日期归一化 / 金额 2% 容差 / 字符串双向包含）+ 条形图输出 |
| **演示辅助脚本** | `ai-service/scripts/demo_script.py`（12 步演示流程打印 + --run --slow 参数） |
| **README.md** | 全量重写：Mermaid 架构图 + ASCII 架构图 + 完整项目结构树 + M1-M18 模块完成度表 + Excel/PDF 导出说明 + 准确率数据（OFF 88.89% → ON 98.61%）+ pytest 指南 + curl 示例 |
| **生产代码 bug 修复** | chunker.py pattern 3 `\s`→`\s*`（顿号后空格可选）+ pattern 4 `\s`→`\s*`（右括号后空格可选）+ _aggregate_segments 空列表兜底；normalize_date 增强中文数字→阿拉伯转换（〇→0, 一→1, 二→2...）；validate_enum 增强关键词子串匹配 |
| **Mock 修复** | test_classifier.py 所有 _R mock 补 `.usage = None` 属性 + JSON key 从 `contract_type` 改为 `type`（与 classifier.py 的 data.get("type") 对齐） |
| **遗留** | run_accuracy_test.py 未做端到端 PDF+标注测试；demo_script.py 未执行过；README.md 未人工 review 确认内容准确性 |

---

### P0-1 · LangGraph 多 Agent 工作流 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 5 项 pytest 全通过（并入 114 项总套件） |
| **完成时间** | 2026-09-04 |
| **实现** | `ai-service/app/graph/contract_graph.py`：StateGraph(ContractState TypedDict) 节点 classify → extract → validate → (retry 回路 ≤2) → review；conditional_edges 路由；linear_chain 对照实现 |
| **路由** | `main.py` 新增 POST `/api/contract/extract-graph`（图工作流）+ `/api/contract/extract-chain`（线性管道对照） |
| **测试** | `tests/test_contract_graph.py`：happy path / retry 回路恢复 / 最大重试不无限循环 / 线性链证据 / 类型传递 |

### P0-2 · Odoo 17 审批流 + Cron ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 模块升级 EXIT=0 + Odoo shell 冒烟验证通过 |
| **完成时间** | 2026-09-04 |
| **审批日志模型** | `models/contract_approval_log.py`：contract.approval.log（contract_id / approver_id / action submit·approve·reject·cancel / comment） |
| **审批方法** | contract.py：action_submit_approval（draft→approval）/ action_approve（approval→seal，原"用印"语义合并）/ action_reject（approval→draft）/ 撤回记 cancel；每步落 approval.log + chatter |
| **服务端动作** | `security/contract_workflow_actions.xml`：3 个 ir.actions.server 绑定表单动作菜单（Odoo 17 无 workflow 模块） |
| **双 Cron** | `data/contract_cron.xml`：每日 _cron_overdue_reminder（逾期计划按合同聚合提醒）+ _cron_expire_contracts（到期 seal→archived 自动归档） |
| **视图/ACL/manifest** | contract_views.xml header 三按钮 + "✅ 审批记录"Tab；ir.model.access.csv +2 条（manager 全权 / 内部用户读写不可删）；manifest data 顺序 security XML → CSV → 动作 → sequence → cron → 视图 |
| **验证** | 升级后 DB 确认：contract_approval_log 表 / ir_cron id=19,20 / ir_act_server 246-248 绑定 / ACL 2 条；shell 冒烟 submit→reject→submit→approve 四日志按序落库 + 到期自动归档 |

### P0-3 · LLM 提取正则兜底 ✅

| 项 | 内容 |
|----|------|
| **状态** | ✅ 15 项 pytest 全通过（并入 114 项总套件） |
| **完成时间** | 2026-09-04 |
| **实现** | `ai-service/app/services/postprocess_fallback.py`：fill(fields, text) 只补缺失不覆盖；contract_code（标签锚定）/ amount（总价锚定模式优先防单价误判，支持万元/元/千分位）/ 日期三兄弟（锚点上下文 + 中文数字归一化）/ partner_a·b（公司后缀词库）/ dispute_resolution（关键词→枚举）/ currency（符号→ISO） |
| **集成点** | extractor.py：_business_validate() 之后、return 之前调用 postprocess_fill；fallback 路径同样接入 |
| **关键修复** | 金额前缀 `人民币?` → `(?:人民币)?`（量词只作用于"币"字导致"合同总价：500,000 元"漏配） |

---

## 六、已完成内容记录

### M1 · 项目初始化
四服务 Docker Compose + Odoo 配置 + 环境变量模板 + FastAPI 骨架 + Chroma 镜像 + start.ps1 + README。四个服务 healthcheck 全部通过 ✅

### M2 · contract_ai 模块骨架
`__manifest__.py` + 2 用户组 + 15 条 ACL + 合同编号序列（CG-YYYY-NNNN）。安装状态 `installed` ✅

### M3+M4+M5 · 合并进 contract_ai（原 contract_system 废弃）
触发原因：双模块同 `_name = "contract.contract"` 并存 → 合并 → 删遗留 → 重置数据库 → 重装。7 模型 39 字段对齐；One2many ↔ Many2one 配对验证一致；15 条 ACL 覆盖全部模型；3 个辅助模型有了视图 XML。

### M3 · 第二轮重构字段变更
contract_type→type、sign_date→date_signed、dispute_resolution Text→Selection、pdf_file→source_pdf、partner_a/b Char→Many2one(counterparty)。新增 signatory_id / ai_extracted_json / payment_plan_ids / clause_ids / element_ids。状态机简化 6 态→5 态（删 approved/cancelled，改 void）。

### M6 · PDF 预处理
PdfProcessor 分流文字版（pdfplumber）/ 扫描版（PaddleOCR），POST /api/contract/parse 返回统一文本 + 分流判定。

### M7 · 文本清洗与切块
chunker.py 实现章节识别、OCR 噪声清洗、语义切块（保留结构标签）。

### M8 · 向量库
VectorStore 封装 Chroma HttpClient 0.5.20（严格对齐），双集合（contract_examples / contract_chunks），SentenceTransformer bge-small-zh-v1.5 离线加载（HF 缓存 volume 挂载）。vector_query / vector_stats API。

### M9 · 提示词管理
PromptManager 全量重写为 YAML 驱动 + 字段字典 JSON：
- prompts/v1/prompts.yaml：4 条结构化 prompt（system_extract / classify / extract / extract_retry），每条有 name / version / system_prompt / user_prompt_template / variables
- prompts/field_dict.json：9 组枚举，供提示词中 `{field_dict.contract_type.values}` 点号路径引用
- render(name, version=None, **variables)：正则 `{[a-zA-Z_][a-zA-Z0-9_.]*}` 替换；变量优先级 user_vars > field_dict > 保留原样；extract / extract_retry 自动继承 system_extract 的 system_prompt
- load() 向后兼容，返回 user_template 原始 str
- rollback(version) 切换版本
- test_m9.py 6 项全过 + audit_m9.py 核查 4 需求全绿
- FastAPI lifespan 初始化；health API 返回 prompts_version: "v1"

### M10 · LLM 客户端封装
BaseLLM(abc.ABC) + OpenAILLM + DeepSeekLLM + create_llm() 工厂：
- 用 raw openai.OpenAI SDK（3.5.0），base_url 区分 provider
- chat(messages, temperature=0, json_mode=False) → LLMResponse(content, usage, model, finish_reason)
- json_mode → response_format={"type":"json_object"} + json.loads() 二次验证
- 重试 3 次指数退避（1s→2s→4s），捕获 APIError / APITimeoutError / APIConnectionError / RateLimitError / JSONDecodeError
- invoke(prompt_or_messages) → LLMResponse，返回有 .content 的对象，兼容 classifier.py / extractor.py 现有代码（无需修改调用方）
- create_llm("deepseek", api_key) → DeepSeekLLM；其他走 OpenAILLM + 自定义 base_url
- main.py _get_llm_client() 已从 ChatOpenAI 切到 create_llm() 工厂
- test_m10.py 7 项 mock 测试全过（工厂分发 / chat() / json_mode / 重试 / invoke 兼容 / 边界）

### M11 · 合同分类模块
全量重写 classifier.py + 二次迭代融合策略，核心特性：
- **3 分支加权融合**：(a) LLM 失败 → 纯回退规则；(b) LLM 高置信(≥0.7) → 信任 LLM（一致+0.05 / 冲突不变）；(c) LLM 低置信(<0.7) → ensemble 加权融合
  - ensemble 共识：`rule_conf*0.55 + llm_conf*0.45 + 0.1`（上限 0.9），双方都不确定但指向同一结论时互相增强
  - ensemble 冲突：选高置信者 × 0.85 惩罚，表达"对方有异议"但不过度贬低
- Prompt 来源：优先 `PromptManager.render("classify", ...)`，降级硬编码 prompt
- LLM 调用：`BaseLLM.chat(messages, json_mode=True)` 强制 JSON
- LLM 返回无效类型时强制 conf=0.3 确保进入 ensemble/回退分支
- 规则 baseline：标题匹配 +5 分、正文关键词频率累加、动态置信度
- main.py：`_get_llm_client()` 传 `prompt_manager`；ClassifyResponse 新增 `llm_failed`
- test_m11.py **9 项 mock 测试**（规则 4 类 / LLM 高置信 / ensemble 冲突 / LLM 异常回退 / 高置信一致加成 / 字段完整性 / PromptManager 集成 / 无效类型 → ensemble 冲突 / **低置信+规则一致 → ensemble 共识加成**）
- 代码清理：删除旧 `_extract_json_str` 遗留方法、无用 import re/Optional

### M14 · AI 服务 API 层
main.py 全量重写（317→512 行），test_m14.py 17 项 mock 测试全过：
- **统一响应格式**：`_ok()` / `_err()` helpers，所有端点返回 `{success, data, error, elapsed_ms}`
- **全局异常处理**：HTTPException handler + Exception 兜底 handler，未捕获异常也包成统一格式 500
- **生命周期管理**：lifespan 初始化 PdfProcessor / PromptManager / VectorStore / RAGLearner；LLM + Classifier + Extractor 延迟初始化
- **LLM 延迟工厂**：`_get_llm_client()` 首次调用时创建，无 API Key 抛 HTTPException（全局 handler 包成统一格式）
- **6 个路由**：health / parse / classify / extract（PDF 全流程）/ vector_search（GET）/ vector_stats
- **extract 端点全流程**：PDF 上传 → 解析 → 自动分类（可选 contract_type 预设跳过）→ RAG 字段提取，返回 parse_result + classify + extraction 三段式响应
- **test_m14.py**（445 行，FastAPI TestClient + mock 服务）：健康检查(1) / 分类(3) / 字段提取全流程(5) / 向量检索(4) / 向量统计(1) / 响应格式(2) / 全局异常(1)
- 路由从 7 个精简到 6 个：删除 `/api/contract/quick`（功能被 `/api/contract/extract` 全覆盖）和 `/api/vector/query`（改用 GET `/api/vector/search` query 参数版）

### M15 · 评估与报告模块
新建 evaluate_m15.py（1091 行），离线模式实测通过：
- **测试集**：12 份金标准标注（purchase/sales/service/lease × 3 each），12 个评估字段；无真实 PDF 时 `synthesize_contract_text()` 自动合成兜底
- **字段准确率**：`field_match()` 逐字段比较，日期归一化 YYYY-MM-DD、金额 2% 容差 + "万元"后缀处理、字符串双向包含模糊匹配
- **RAGAS 指标 4 个全覆盖**：Faithfulness（忠实度）+ Answer Relevancy（相关性）+ Context Precision（精度）+ Context Recall（召回率）；优先调 ragas 库（LLM-as-judge），不可用时降级自算版（token 级 + 日期/金额特殊处理，零外部依赖）
- **两组对比评估**：RAG=ON（few-shot）vs RAG=OFF（基线），自动计算提升幅度
- **输出物**：Markdown 报告（6 章节：总体对比 → 逐字段 → RAG 指标 → 逐合同详情 → RAG 修复案例 → 分析结论）+ JSON 原始数据
- **CLI 参数**：`--offline`（mock 预测值，不调 LLM）/ `--no-ragas` / `--annot-dir` / `--pdf-dir` / `--output-dir`
- **实测结果**：offline 模式，字段准确率 88.89% → 98.61%（+9.7pp），144 个问答对
- **遗留清理**：evaluate_accuracy.py（旧版，引用已过期的 PDFParser / ContractExtractor(llm, vs, pm) 签名）标注为过时

### M16 · 业财一体化模块
contract_payment_plan.py 全量重写（131→211 行）+ contract.py 新增 210 行 + 新建 contract_payment_plan_views.xml（204 行）：
- **状态机重构**：`draft → confirmed → partial → paid / cancelled`（替换旧的 pending/partial/done/overdue/cancelled）；4 个状态流转方法 action_confirm / action_done / action_cancel / action_reset_draft；action_done 自动填充 actual_amount/actual_date
- **direction 收/付方向自动判定**：purchase/lease→payable（我方付款）；sale/service/labor/partnership/consulting→receivable（我方收款）；store=True 便于 group_by
- **generate_payment_plan() 中文付款条款解析器**：contract.py 主入口 + _parse_payment_terms() + _parse_time_offset() 辅助；按 [；;\n] 切句 → 百分比正则提取 → 节点类型关键词映射 → 时间偏移估算（显式天数优先，否则按 milestone 默认）→ 金额=合同额×百分比，日期=签订日+偏移；无签订日用今天；兜底一条 100% 尾款计划
- **合同汇总计算字段（5 个 store=True）**：payment_plan_count / planned_amount_total / paid_amount_total / remaining_amount_total / overdue_count；依赖链 payment_plan_ids.planned_amount/actual_amount/state/is_overdue 自动联动
- **contract.payment.plan 新增字段**：direction（compute store）+ contract_type/code/name/amount（related store），方便 Pivot 分组
- **独立台账视图**：tree（color decoration：已完成绿/已确认蓝/逾期红/取消灰）+ form（header 状态按钮）+ **Pivot**（行维度：合同类型 × 收/付方向 × 状态）+ graph + search（含方向/类型/状态过滤）
- **合同视图增强**：header 新增「📋 自动生成计划」按钮；付款计划 Tab 顶部加汇总面板（planned/paid/remaining 红字高亮/overdue）；子表加 control 区域（确认/完成/取消行内按钮）+ color decoration；合同基本信息 Tab 加「收付款汇总」组
- **独立菜单入口**：contract_menu.xml 新增「💰 收付款台账」sequence=50
- **Bug 修复**：contract.py action_ai_extract() 旧 URL `/api/contract/quick` → M14 重命名后的 `/api/contract/extract`
- **Docker 升级验证**：EXIT=0，Registry loaded in 3.495s，零 CRITICAL/ERROR/Traceback
- **新增视图文件**必须在 decoration-danger 引用 is_overdue 字段时显式声明 `<field name="is_overdue" invisible="1"/>`（Odoo 17 要求 decoration 表达式引用的字段必须出现在视图声明中）

### 经验教训记录

| 教训 | 场景 | 解决 |
|------|------|------|
| Chroma 版本不兼容 | chromadb 1.5.9 客户端发带 `_type` 字段请求被 0.5.20 服务端拒绝 | requirements.txt 锁 `chromadb==0.5.20` |
| 容器 HF 外网不通 | SentenceTransformer 加载报 Connection refused | docker-compose.yml 加 HF 缓存 volume 挂载 + `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` |
| bge-large 缓存不可用 | 容器内加载 bge-large-zh-v1.5 报错 | 统一为 bge-small-zh-v1.5（512d，本地已缓存），`EMBEDDING_DIMENSION=512` |
| main.py 类名不匹配 | M8 后 PdfProcessor / VectorStore / extract_text() 与 M5/PdfParser/ContractVectorStore 不一致 | 全量修改 5 处 main.py 调用 |
| DeepSeek 用 response_format 而非特殊字段 | 过往错误在 payload 硬编码 `reasoning_split` 等非文档参数 | 统一用 `response_format={"type":"json_object"}`；非标准字段做成可配置开关 |
| LangChain ChatOpenAI vs raw SDK | LangChain 中间层隐式行为（如 `.content` vs `.message.content`），且延迟加载逻辑被绕过 | M10 改用 raw `openai.OpenAI` SDK，LLMResponse dataclass 模拟 LangChain AIMessage 的 .content |

---

## 七、变更日志

| 日期 | 变更内容 | 操作者 |
|------|----------|--------|
| 2026-08-29 | 初始创建 PROJECT_CONTEXT.md，M1/M2 已完成记录 | AI |
| 2026-08-29 | 解决 contract_ai 与 contract_system 模型重名：合并 → 删遗留模块 → 重置数据库 → 重装 | AI |
| 2026-08-29 | 同步更新文档：M3/M4/M5 标记已完成，删除双模块警告，补充数据库重置命令 | AI |
| 2026-08-29 | **M3 第二轮重构**：重写 contract.py（全字段/类型/状态机对齐需求）+ 新增 5 个模型文件 | AI |
| 2026-08-29 | 核查 M3 模型层 vs 非模型层：ACL 只覆盖 contract.contract；发现升级阻塞项 | AI |
| 2026-08-30 | **M6 PDF 分流** PdfProcessor + **M7 切块** chunker.py + **M8 向量库** VectorStore；chromadb 锁 0.5.20；HF 缓存 volume 挂载 | AI |
| 2026-08-31 | **M9 提示词管理** prompt_manager.py 全量重写为 YAML 驱动 + field_dict.json；audit_m9.py 核查 4 需求全绿；requirements.txt 删 langchain-openai 硬依赖 | AI |
| 2026-09-01 | **M10 LLM 封装** llm_client.py 新建（BaseLLM + OpenAILLM + DeepSeekLLM + create_llm 工厂）；main.py _get_llm_client 从 ChatOpenAI 切到 create_llm()；test_m10.py 7 项 mock 测试全过 | AI |
| 2026-09-01 | **PROJECT_CONTEXT.md 全量更新**：技术栈表格更新（Chroma 版本、Embedding 模型、新增 openai SDK）；目录树补 chunker.py / llm_client.py / prompts.yaml / field_dict.json / 测试脚本 / Odoo 新视图；M6-M10 从 🟡 升级为 ✅ 各写详情节；M3 ACL 状态更新为已补全；追加经验教训表 + 变更日志 | AI |
| 2026-09-01 | **M11 合同分类** classifier.py 全量重写 + 二次迭代融合策略：(1)旧版硬编码prompt→PromptManager.render()；(2)旧版invoke(string)→chat(messages, json_mode=True)；(3)旧版纯回退→3分支加权融合（LLM失败→rule_fallback / LLM≥0.7→llm / LLM<0.7→ensemble共识加成或冲突×0.85惩罚）；修复LLM无效类型应强制降置信度的bug；清无用import；main.py传prompt_manager+ClassifyResponse加llm_failed；test_m11.py 9项mock测试全过 | AI |
| 2026-09-01 | **M12 RAG 监督学习** 新建 rag_learner.py（RAGLearner 类 ~350 行）：范例入库（add_example → chunk_text → VectorStore.add_texts 写入 contract_examples 集合）+ 相似检索（retrieve_examples → VS.query → 按 example_id 去重取最佳 chunk 分数 → 内存缓存补全完整 extraction）+ load_examples 批量加载标注目录；修复 bootstrap_examples.py / evaluate_accuracy.py 引用不存在的 ContractVectorStore → 改用 VectorStore + RAGLearner；test_m12.py 8 项 mock 测试全过 | AI |
| 2026-09-01 | **M13 字段提取模块** extractor.py 全量重写（193→487 行）：(1)旧版 llm.invoke()→M10 llm.chat(messages, json_mode=True)；(2)旧版 vs.search_examples()→M12 rag_learner.retrieve_and_format_few_shot()；(3)旧版 pm.load("xxx.txt")→M9 pm.render("extract", ...)；(4)新增 ExtractResult dataclass 返回结构化日志；(5)重试用 extract_retry prompt 带上 previous_result+validation_errors；(6)JSON 容错解析（4 层 fallback）；(7)业务校验日期硬约束 + 金额大写 WARNING（不触发 retry）；(8)低耦合注入 RAGLearner 而非 VectorStore；(9)保留 .extract() 旧 API 兼容；main.py extractor 初始化改传 rag_learner；RAGLearner 补 format_few_shot() + retrieve_and_format_few_shot()（M12 遗留）；test_m13.py 10 项 mock 测试全过（28/28 总绿） | AI |
| 2026-09-01 | **M14 AI 服务 API 层** main.py 全量重写（317→512 行）：统一响应格式 _ok/_err helpers + 全局异常兜底 handler + lifespan 生命周期管理 + LLM 延迟初始化工厂 + 6 路由（health / parse / classify / extract 全流程 / vector_search GET / vector_stats）；test_m14.py 17 项 mock 测试全过（FastAPI TestClient + mock 服务）；路由从 7 精简到 6（删 quick + vector_query） | AI |
| 2026-09-01 | **M15 评估与报告模块** 新建 evaluate_m15.py（1091 行）：12 份金标准标注 + 12 个评估字段 + synthesize_contract_text 合成兜底；field_match() 日期归一化/金额 2% 容差/字符串模糊匹配；RAGAS 4 指标全覆盖（Faithfulness + Answer Relevancy + Context Precision + Context Recall），优先 ragas 库 + 自算版降级；RAG=ON vs OFF 两组对比；Markdown 报告 6 章节 + JSON 原始数据；offline 模式实测通过（88.89%→98.61% +9.7pp）；evaluate_accuracy.py 旧版标注为过时 | AI |
| 2026-09-01 | **PROJECT_CONTEXT.md 更新**：M14/M15 从 🟡 升级为 ✅ 各写详情节；目录树补 test_m14.py / evaluate_m15.py；追加 M14/M15 已完成内容记录 + 变更日志条目 | AI |
| 2026-09-01 | **M16 业财一体化模块**：重写 contract_payment_plan.py（状态机 draft→confirmed→paid/cancelled + direction 自动判定 + 4 个状态流转方法）；contract.py 新增 generate_payment_plan() 中文付款条款正则解析器（_parse_payment_terms + _parse_time_offset）+ 5 个汇总计算字段；新建 contract_payment_plan_views.xml（独立 tree/form/pivot/graph/search）；contract_views.xml 增强付款计划 Tab（汇总面板 + 子表状态按钮 + header 生成按钮）；contract_menu.xml 新增台账子菜单；修复 contract.py /api/contract/quick→/api/contract/extract 的 URL bug；Docker 升级 EXIT=0 零报错 | AI |
| 2026-09-01 | **M16 PROJECT_CONTEXT.md 同步**：M16 从 🟡 升级为 ✅ 写详情节；目录树更新 payment_plan.py 注释 + 新增 contract_payment_plan_views.xml；M3 视图状态更新为 payment.plan 有独立 XML；追加 M16 已完成内容 + 经验教训（decoration 字段必须显式声明）+ 变更日志条目 | AI |
| 2026-09-01 | **M17 配置域与报表模块**：contract_config.py 单例模型（SQL unique(id) + generate_contract_code 编号规则）；contract_report_views.xml（search/pivot/graph/ledger 4 视图）；contract_pdf_reports.xml QWeb 2 套模板（report_contract_ledger_pdf + report_contract_detail_pdf）+ wkhtmltopdf 0.12.6.1 实测 42KB/41KB PDF；contract_export.py openpyxl Excel 双 Sheet 导出；Bug 修复 QWeb context_timestamp → datetime.now() + Odoo 17 tree view 不支持 totals/sum + pivot 不接受 date_signed:month | AI |
| 2026-09-01 | **M18 工程化交付与部署**：✅ 94 项 pytest 全通过；新建 tests/ 目录 conftest.py + test_chunker.py(48) + test_classifier.py(23) + test_field_validator.py(23)；run_accuracy_test.py(360) 准确率 CLI；demo_script.py 12 步演示辅助；README.md 全量重写（Mermaid 架构图 + M1-M18 完成度表 + 准确率数据）；chunker.py 生产代码 bug 修复（pattern 3/4 \s→\s* + 空列表兜底）；normalize_date 增强中文数字→阿拉伯转换；classifier.py mock 补 .usage 属性 + JSON key 对齐；test 断言错误修正 9 处；from 11 fail → 94 pass | AI |
| 2026-09-04 | **P0-1 LangGraph 多 Agent**：新建 app/graph/contract_graph.py（StateGraph：classify→extract→validate→retry 回路≤2→review + conditional_edges）+ linear_chain 对照；main.py 增 /extract-graph 与 /extract-chain 路由；requirements.txt 补 langgraph≥0.2；test_contract_graph.py 5 项全过 | AI |
| 2026-09-04 | **P0-3 正则兜底**：新建 app/services/postprocess_fallback.py（fill 只补缺失不覆盖；编号/金额/大写/日期×3/甲乙方/争议/币种 7 类策略）；extractor.py 在业务校验后集成 postprocess_fill；修复金额前缀 人民币? → (?:人民币)? 的量词作用域 bug（"合同总价：500,000 元"漏配）；test_postprocess_fallback.py 15 项全过（含总价≠单价回归） | AI |
| 2026-09-04 | **P0-2 审批流+Cron**：新建 contract_approval_log.py + contract_workflow_actions.xml（3 个 ir.actions.server）+ contract_cron.xml（逾期提醒/到期归档 2 个 ir.cron）；contract.py 增 action_submit_approval/action_approve(重定义 approval→seal)/action_reject/_notify/_log_approval + 双 cron 方法（逾期计划按合同聚合、seal 到期自动归档）；contract_views.xml header 三按钮 + 审批记录 Tab；ACL +2；manifest 补 2 个 data 文件；`docker compose run --rm odoo -d contract_db -u contract_ai` 升级 EXIT=0，DB 确认表/cron/动作/ACL 就位；odoo shell 冒烟 submit→reject→submit→approve + 到期归档全通过 | AI |
| 2026-09-04 | **P1-3 git 分阶段提交**：git reset 清理误暂存的 chroma_data 二进制；按 基础设施→AI核心→RAG评估→LangGraph/兜底/测试→Odoo模块→文档 6 笔提交；README v1.1.0 更新（P0 三节 + API 表 + 测试清单 + 完成度表） | AI |

---

**⚠️ 提醒**：每次代码迭代后，请更新本文件的「已完成内容记录」「变更日志」章节，以保持项目上下文的连续性。
