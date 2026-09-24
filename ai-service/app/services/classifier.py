# ═══════════════════════════════════════════
# M11 · 合同分类模块（规则 + LLM 双通道 + 加权融合策略）
#
# 融合策略（3 分支）：
#   (a) LLM 调用失败 → 纯回退规则（rule_fallback）
#   (b) LLM 成功 + conf ≥ 0.7 → 信任 LLM；规则一致则加成，冲突则轻微惩罚（llm）
#   (c) LLM 成功 + conf < 0.7 → 加权融合（ensemble）：
#         - 两者一致 → 共识加成：加权平均 + 0.1，上限 0.9
#         - 两者冲突 → 选高置信者，冲突惩罚：max(llm_conf, rule_conf) * 0.7
#
# Prompt 来源：M9 PromptManager + prompts/v1/prompts.yaml "classify" 条目
# LLM 调用：M10 BaseLLM.chat(messages, json_mode=True) 强制 JSON
# ═══════════════════════════════════════════
import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("classifier")

# ── M21 阈值（环境变量可调）──
# 双通道冲突/低置信 → 触发一次仲裁调用的门槛
ARBITRATE_THRESHOLD = 0.60
# 仲裁结果被采纳的最低置信度
ARBITRATE_ADOPT_THRESHOLD = 0.75
# 仲裁后仍低于此值 / 一致但置信度低于此值 → needs_review 转人工
CLASSIFY_REVIEW_THRESHOLD = 0.50


# ═════════════════════════════════════════════════════════════
# 分类结果 dataclass（保留双通道详情便于调试和答辩展示）
# ═════════════════════════════════════════════════════════════

@dataclass
class ClassifyResult:
    """分类结果"""
    contract_type: str          # 最终选定的合同类型
    confidence: float           # 最终置信度 0-1
    rule_result: str            # 规则通道判断
    rule_confidence: float      # 规则通道置信度
    llm_result: str             # LLM 通道判断
    llm_confidence: float      # LLM 通道置信度
    llm_failed: bool           # LLM 是否调用失败（异常）
    method_used: str           # "llm" / "ensemble" / "rule_fallback" / "arbitration"
    # ── M21：可靠性信号（gatekeeper 汇总进 quality.review_reasons）──
    needs_review: bool = False                 # 分类不可靠，需人工确认
    review_reasons: list = field(default_factory=list)
    rule_evidence: dict = field(default_factory=dict)  # 规则通道命中证据（仲裁/排查用）


# ═════════════════════════════════════════════════════════════
# ContractClassifier · 主类
# ═════════════════════════════════════════════════════════════

class ContractClassifier:
    """
    合同分类器：规则关键词 baseline + LLM 语义分类 + 明确回退策略

    用法：
        clf = ContractClassifier(llm_client=llm, prompt_manager=pm)
        result = clf.classify(contract_text, contract_title)
    """

    # 有效合同类型（与 field_dict.json 保持一致）
    VALID_TYPES = ["采购合同", "销售合同", "服务合同", "租赁合同", "其他"]

    # ═════════════════════════════════════════════════════════
    # 规则词典（2026-09 v3 升级：词频等权 → 分层加权 + 角色定义行）
    #   旧版所有关键词 +1/次，"采购方"角色词与"采购"泛词同权，
    #   采购合同里出现 3 次"销售方"就能把销售得分带偏。
    #   分层加权：强特征（角色决定词）+3 / 中特征（标的物）+2 / 弱特征（泛词）+1
    # ═════════════════════════════════════════════════════════
    RULE_KEYWORDS_WEIGHTED = {
        "采购合同": [
            ("采购方", 3), ("需方", 3), ("买方", 3),
            ("设备采购", 2), ("材料采购", 2), ("供货", 2), ("采购订单", 2), ("采购协议", 2),
            ("采购", 1), ("购买", 1), ("买卖合同", 1),
        ],
        "销售合同": [
            ("销售方", 3), ("供方", 3), ("卖方", 3), ("供应商", 3),
            ("产品销售", 2), ("销售订单", 2), ("销售协议", 2),
            ("销售", 1), ("出售", 1),
        ],
        "服务合同": [
            ("受托方", 3), ("服务方", 3), ("运维服务", 3),
            ("技术服务", 2), ("咨询服务", 2), ("服务协议", 2), ("项目服务", 2),
            ("劳务", 2), ("实施服务", 2), ("培训服务", 2),
            ("服务", 1), ("咨询", 1), ("技术支持", 1), ("运维", 1), ("实施", 1), ("培训", 1),
        ],
        "租赁合同": [
            ("出租方", 3), ("承租方", 3), ("出租人", 3), ("承租人", 3),
            ("场地租赁", 2), ("设备租赁", 2), ("房屋租赁", 2), ("租赁协议", 2), ("租金", 2), ("押金", 2),
            ("租赁", 1), ("出租", 1), ("承租", 1), ("租期", 1),
        ],
    }

    # 甲方角色定义行（+5：合同第一条的结构化信息，比词频可靠一个量级）
    #   典型写法："甲方（采购方）：XX公司" / "甲方（以下简称"采购方"）" / "需方（甲方）：XX公司"
    #   tempered 正则：甲方与角色词之间 25 字内不得出现"乙方"
    #   （防"甲方乙方经友好协商，……采购方"这类跨句误配）
    _ROLE_GAP = r"(?:(?!乙方)[^。；;\n]){0,25}?"
    ROLE_LINE_PATTERNS = [
        # 正向：甲方（角色词）
        (re.compile(r"甲方" + _ROLE_GAP + r"(采购方|买方|需方|订购方)"), "采购合同"),
        (re.compile(r"甲方" + _ROLE_GAP + r"(供应方|卖方|供方|销售方)"), "销售合同"),
        (re.compile(r"甲方" + _ROLE_GAP + r"(委托方|服务方)"), "服务合同"),
        (re.compile(r"甲方" + _ROLE_GAP + r"(出租方|承租方|出租人|承租人)"), "租赁合同"),
        # 反向：角色词（甲方）——"采购方（以下简称甲方）"
        (re.compile(r"(采购方|买方|需方|订购方)" + _ROLE_GAP + r"甲方"), "采购合同"),
        (re.compile(r"(供应方|卖方|供方|销售方)" + _ROLE_GAP + r"甲方"), "销售合同"),
        (re.compile(r"(委托方|服务方)" + _ROLE_GAP + r"甲方"), "服务合同"),
        (re.compile(r"(出租方|承租方|出租人|承租人)" + _ROLE_GAP + r"甲方"), "租赁合同"),
    ]

    # 标题优先匹配（标题里有就基本坐实，权重 +8 分——压过任何词频噪声）
    RULE_TITLE_PATTERNS = {
        "采购合同": ["采购合同", "采购协议", "设备采购", "材料采购", "买卖合同", "采购订单"],
        "销售合同": ["销售合同", "销售协议", "产品销售", "销售订单"],
        "服务合同": ["服务合同", "服务协议", "技术服务", "咨询服务", "劳务合同", "运维服务"],
        "租赁合同": ["租赁合同", "租赁协议", "场地租赁", "设备租赁", "房屋租赁"],
    }

    # ── 义务方向信号（v4 新增 +3：与 LLM"义务推断"判定哲学对齐）──
    #   采购方向 = 甲方付货款 或 乙方供货给甲方（钱货对流，甲方=买方）
    #   销售方向 = 乙方付货款 或 甲方供货
    #   主语锚定：主语前 (?<!向)——"向乙方交付设备"中的乙方是间接宾语
    #   （真主语是甲方），不锚定会把"甲方向乙方交付"误判成乙方交货
    #   词表刻意只含货物价款/货物交付——不含租金/服务费/违约金，
    #   避免租赁付款（甲方支付租金）和服务费污染方向判定，
    #   违约责任条款（"乙方向甲方支付违约金"）因"违约金"不在款类词表而不误配
    _OBLI_PAY = r"(支付|付款)[^。；;\n]{0,15}(价款|货款|预付款|余款|尾款|剩余|定金|订金|款项)"
    _OBLI_DELIVER = r"(供应|交付|送达|提供)[^。；;\n]{0,20}(货物|设备|产品|商品|材料|仪器|服务器)"
    OBLIGATION_BUY_PATTERNS = [
        re.compile(r"(?<!向)甲方[^。；;\n]{0,40}" + _OBLI_PAY),
        re.compile(r"(?<!向)乙方[^。；;\n]{0,40}" + _OBLI_DELIVER),
    ]
    OBLIGATION_SELL_PATTERNS = [
        re.compile(r"(?<!向)乙方[^。；;\n]{0,40}" + _OBLI_PAY),
        re.compile(r"(?<!向)甲方[^。；;\n]{0,40}" + _OBLI_DELIVER),
    ]

    # 义务条款行特征词（供 _extract_obligation_excerpt 定位付款/交付条款）
    OBLIGATION_LINE_PATTERN = re.compile(
        r"(支付|付款|价款|预付|尾款|交付|供应|送达|租金|货到|分期)"
    )

    # LLM 置信度阈值（≥ 此值才信任 LLM，否则回退规则）
    LLM_CONFIDENCE_THRESHOLD = 0.7

    def __init__(self, llm_client, prompt_manager=None):
        """
        Args:
            llm_client:      BaseLLM 实例（M10 的 create_llm() 返回值）
            prompt_manager:  PromptManager 实例（M9），可选；不传则用降级硬编码 prompt
        """
        self.llm = llm_client
        self.pm = prompt_manager
        logger.info(
            f"ContractClassifier 初始化 | "
            f"llm={type(llm_client).__name__} | "
            f"prompt_manager={'✅' if prompt_manager else '⚠️ 未注入（将用降级 prompt）'} | "
            f"置信度阈值={self.LLM_CONFIDENCE_THRESHOLD}"
        )

    # ═══════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════

    def classify(self, contract_text: str, contract_title: str = "") -> ClassifyResult:
        """
        合同分类主入口：规则 baseline → LLM 语义分类 → 回退策略

        Args:
            contract_text: 合同正文文本
            contract_title: 合同标题（可选，标题匹配权重更高）

        Returns:
            ClassifyResult（含最终类型、置信度、双通道详情）
        """
        t_start = time.time()
        logger.info(
            f"🔍 开始分类 | 文本长度={len(contract_text)} | "
            f"标题='{contract_title[:30]}{'...' if len(contract_title) > 30 else ''}'"
        )

        # ── 通道 1：规则匹配（快速、零成本、永远成功）──
        rule_type, rule_conf, rule_evidence = self._rule_classify(contract_text, contract_title)
        logger.debug(f"  📋 规则分类: {rule_type} (置信 {rule_conf:.2f})")

        # ── 通道 2：LLM 语义分类（可能失败）──
        llm_type, llm_conf, llm_failed = self._llm_classify(contract_text, contract_title)
        if llm_failed:
            logger.warning(f"  ⚠️  LLM 调用失败，将回退到规则结果")
        else:
            logger.debug(f"  🤖 LLM 分类: {llm_type} (置信 {llm_conf:.2f})")

        # ── 融合：严格按回退策略 ──
        result = self._fuse(rule_type, rule_conf, llm_type, llm_conf, llm_failed)
        result.rule_evidence = rule_evidence

        # ── M21：低置信/双通道冲突 → 仲裁一次（带规则证据让 LLM 复核）──
        conflicted = (not llm_failed) and rule_type != llm_type
        if not llm_failed and result.confidence < ARBITRATE_THRESHOLD:
            arb_type, arb_conf, arb_failed = self._arbitrate(
                contract_text, contract_title, rule_evidence, rule_type, llm_type
            )
            if not arb_failed and arb_conf >= ARBITRATE_ADOPT_THRESHOLD:
                result.contract_type = arb_type
                result.confidence = round(max(result.confidence, arb_conf), 3)
                result.method_used = "arbitration"
                logger.info(
                    f"  ⚖️ 仲裁采纳: {arb_type} (conf={arb_conf:.2f})"
                )
            else:
                result.needs_review = True
                result.review_reasons.append(
                    f"分类置信度低({result.confidence:.2f})且仲裁未果"
                    f"(规则={rule_type}/LLM={llm_type})，请人工确认合同类型"
                )
        # 一致但置信度仍低 → 转人工（不仲裁，避免无意义消耗）
        if not result.needs_review and result.confidence < CLASSIFY_REVIEW_THRESHOLD:
            result.needs_review = True
            result.review_reasons.append(
                f"分类置信度低({result.confidence:.2f})，请人工确认合同类型"
            )
        if conflicted and not result.needs_review and result.confidence < 0.75:
            result.needs_review = True
            result.review_reasons.append(
                f"分类双通道冲突(规则={rule_type} vs LLM={llm_type})，请人工确认"
            )

        elapsed = time.time() - t_start
        logger.info(
            f"✅ 分类完成 | 最终类型={result.contract_type} | "
            f"置信度={result.confidence:.2f} | 方法={result.method_used} | "
            f"needs_review={result.needs_review} | 耗时={elapsed:.2f}s"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    # 通道 1：规则匹配
    # ═══════════════════════════════════════════════════════════

    def _rule_classify(self, text: str, title: str) -> tuple[str, float, dict]:
        """
        规则匹配（2026-09 v3 升级）：标题 +8 / 甲方角色定义行 +5 / 分层词频 +3/+2/+1。
        永远成功，作为 baseline 和兜底。

        Returns:
            (contract_type, confidence, evidence)
            evidence: 命中证据 dict（仲裁 prompt 与人工排查用）
        """
        scores: dict[str, float] = {}
        evidence: dict = {"title_hits": [], "role_line_hits": [], "keyword_scores": {}, "obligation": None}

        # 1. 标题匹配（+8 分：标题命中基本坐实，压过词频噪声）
        if title:
            for ctype, patterns in self.RULE_TITLE_PATTERNS.items():
                for p in patterns:
                    if p in title:
                        scores[ctype] = scores.get(ctype, 0) + 8
                        evidence["title_hits"].append(f"{ctype}←'{p}'")

        # 2. 甲方角色定义行（+5 分：结构化信息，只扫前 3000 字——与 LLM 输入窗口一致）
        head = text[:3000]
        for regex, ctype in self.ROLE_LINE_PATTERNS:
            if regex.search(head):
                scores[ctype] = scores.get(ctype, 0) + 5
                evidence["role_line_hits"].append(ctype)

        # 3. 正文关键词分层加权（强特征 ×3 / 中特征 ×2 / 弱特征 ×1）
        for ctype, weighted_kws in self.RULE_KEYWORDS_WEIGHTED.items():
            for kw, weight in weighted_kws:
                count = text.count(kw)
                if count > 0:
                    scores[ctype] = scores.get(ctype, 0) + count * weight
                    evidence["keyword_scores"].setdefault(ctype, []).append(f"{kw}×{count}")

        # 4. 义务方向信号（v4 新增 +3：钱货流向，补齐"无角色标注"合同的规则盲区。
        #    全文扫描——义务条款常在中后部，不受 3000 字窗口限制）
        buy_hits = sum(1 for p in self.OBLIGATION_BUY_PATTERNS if p.search(text))
        sell_hits = sum(1 for p in self.OBLIGATION_SELL_PATTERNS if p.search(text))
        if buy_hits > sell_hits:
            scores["采购合同"] = scores.get("采购合同", 0) + 3
            evidence["obligation"] = "buy"
        elif sell_hits > buy_hits:
            scores["销售合同"] = scores.get("销售合同", 0) + 3
            evidence["obligation"] = "sell"

        # 没有任何信号命中 → "其他"，低置信度
        if not scores:
            return "其他", 0.3, evidence

        # 找得分最高的类型
        best_type = max(scores, key=scores.get)
        sorted_scores = sorted(scores.values(), reverse=True)
        max_score = sorted_scores[0]
        total = sum(scores.values())

        # 置信度计算：分档锚点与新权重体系自洽
        if len(sorted_scores) >= 2 and sorted_scores[0] > sorted_scores[1] * 2:
            # 压倒性优势 → 较高置信度
            confidence = min(0.85, max(0.55, max_score / total + 0.25))
        elif max_score >= 8:
            # 标题命中 → 中高置信度
            confidence = 0.75
        elif max_score >= 5:
            # 角色定义行命中 → 中等偏上
            confidence = 0.65
        elif max_score >= 2:
            confidence = 0.5
        else:
            confidence = 0.4

        return best_type, round(confidence, 3), evidence

    # ═══════════════════════════════════════════════════════════
    # 通道 2：LLM 语义分类
    # ═══════════════════════════════════════════════════════════

    def _llm_classify(
        self, text: str, title: str
    ) -> tuple[str, float, bool]:
        """
        LLM 语义分类。使用 PromptManager 渲染的提示词 + json_mode=True。

        Returns:
            (contract_type, confidence, failed)
            failed=True 表示 LLM 调用异常（网络/超时/JSON 解析失败等）
        """
        # 构造 messages（system + user）
        messages = self._build_llm_messages(text, title)
        logger.debug(f"  LLM messages: system={messages[0]['content'][:60]}... | user={messages[1]['content'][:60]}...")

        try:
            # ⭐ 使用 json_mode=True 强制 LLM 返回合法 JSON
            response = self.llm.chat(messages, temperature=0.0, json_mode=True)
            content = response.content

            # 解析 JSON（json_mode=True 后理论上已经是合法 JSON，但做一层保护）
            data = json.loads(content.strip())

            # 提取类型 + 置信度
            ctype = data.get("type", "其他")
            invalid_type = False
            if ctype not in self.VALID_TYPES:
                logger.warning(f"  LLM 返回了无效类型 '{ctype}'，降级为 '其他'")
                ctype = "其他"
                invalid_type = True

            conf_raw = data.get("confidence", 0.5)
            try:
                conf = float(conf_raw)
            except (TypeError, ValueError):
                logger.warning(f"  LLM 返回的 confidence 无法解析: {conf_raw}，默认 0.3")
                conf = 0.3

            # 范围保护
            conf = max(0.1, min(1.0, conf))

            # ⚠️ LLM 返回无效类型 → 强制降置信度，触发回退规则
            if invalid_type:
                conf = 0.3
                logger.info(f"  无效类型 → 置信度强制降为 {conf}，将触发规则回退")

            # 记录 LLM token 用量（如果有）
            usage = response.usage
            if usage:
                logger.debug(f"  LLM token 用量: {usage}")

            return ctype, round(conf, 3), False

        except Exception as e:
            # 捕获所有异常：网络错误、超时、JSON 解析失败、LLM 返回异常等
            logger.warning(
                f"  LLM 分类调用失败: {type(e).__name__}: {e}"
            )
            return "其他", 0.3, True

    # ═══════════════════════════════════════════════════════════
    # M21 · 仲裁（低置信/双通道冲突时的一次复核调用）
    # ═══════════════════════════════════════════════════════════

    def _arbitrate(
        self, text: str, title: str, rule_evidence: dict,
        rule_type: str, llm_type: str,
    ) -> tuple[str, float, bool]:
        """
        把规则通道的命中证据喂给 LLM 复核一次。

        Returns:
            (contract_type, confidence, failed)
        """
        evidence_text = json.dumps(rule_evidence, ensure_ascii=False)
        system = (
            "你是合同分类仲裁专家。规则引擎和语义模型对合同类型判断不一致或置信度不足，"
            "请你结合规则引擎给出的命中证据与合同原文，做出最终裁定。"
            "只能从 [采购合同、销售合同、服务合同、租赁合同、其他] 中选一个类型。"
        )
        user = (
            f"合同标题：{title or '（无）'}\n\n"
            f"规则引擎命中证据（JSON）：{evidence_text}\n"
            f"规则引擎判断：{rule_type}\n"
            f"语义模型判断：{llm_type}\n\n"
            f"合同正文（前 3000 字）：\n{text[:3000]}\n\n"
            f"严格输出纯 JSON：\n"
            f'{{"type": "xxx", "confidence": 0.xx, "reason": "一句话理由"}}'
        )
        try:
            response = self.llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=0.0, json_mode=True,
            )
            data = json.loads(response.content.strip())
            ctype = data.get("type", "其他")
            if ctype not in self.VALID_TYPES:
                return "其他", 0.3, True
            conf = max(0.1, min(1.0, float(data.get("confidence", 0.5))))
            logger.info(f"  ⚖️ 仲裁结果: {ctype} (conf={conf:.2f}) reason={data.get('reason', '')}")
            return ctype, round(conf, 3), False
        except Exception as e:
            logger.warning(f"  仲裁调用失败: {type(e).__name__}: {e}")
            return "其他", 0.3, True

    def _extract_obligation_excerpt(self, text: str, max_lines: int = 12, max_chars: int = 1200) -> str:
        """
        从全文（不受 3000 字窗口限制）定位付款/交付义务条款行。

        v4 修复：LLM 输入只有前 3000 字，而义务条款（付款方式）常在合同中后部
        ——第二步方向判定拿不到证据时会退回标题兜底，等于白升级。
        按行抽取（合同条款多为一行一条），保序去重，截断到 1200 字。
        """
        hits: list[str] = []
        seen: set[str] = set()
        for raw in text.splitlines():
            line = raw.strip()
            if len(line) < 4 or line in seen:
                continue
            if self.OBLIGATION_LINE_PATTERN.search(line):
                hits.append(line)
                seen.add(line)
            if len(hits) >= max_lines:
                break
        if not hits:
            return "（未检出明显义务条款）"
        return "\n".join(hits)[:max_chars]

    def _build_llm_messages(self, text: str, title: str) -> list[dict]:
        """
        构造发给 LLM 的 messages 列表。
        优先用 PromptManager.render("classify", ...) 获取 system+user；
        若 prompt_manager 未注入则用降级硬编码 prompt。
        """
        # 截断过长文本（避免超 token）
        text_truncated = text[:3000] if len(text) > 3000 else text
        # 义务条款摘录（从全文抽，覆盖 3000 字窗口外的中后部付款/交付条款）
        obligation_excerpt = self._extract_obligation_excerpt(text)

        if self.pm is not None:
            try:
                rendered = self.pm.render(
                    "classify",
                    contract_text=text_truncated,
                    contract_title=title or "（无）",
                    obligation_excerpt=obligation_excerpt,
                )
                return [
                    {"role": "system", "content": rendered["system"]},
                    {"role": "user", "content": rendered["user"]},
                ]
            except Exception as e:
                logger.warning(
                    f"  PromptManager.render 失败，降级到硬编码 prompt: {e}"
                )

        # ── 降级方案：硬编码 prompt（保证即使没有 PromptManager 也能工作）──
        system = (
            "你是合同分类专家，擅长根据合同标题、条款内容、甲乙方角色判断合同类型。"
            "只能从 [采购合同、销售合同、服务合同、租赁合同、其他] 中选一个类型，同时给出 0-1 的置信度。"
        )
        user = (
            f"请判断以下合同属于哪一类。\n\n"
            f"分类规则：\n"
            f"- 采购合同：买东西的合同（买方/采购方/需方 ↔ 卖方/供应商）\n"
            f"- 销售合同：卖东西的合同（卖方/供方/销售方 ↔ 买方/客户）\n"
            f"- 服务合同：提供/接受服务的合同（咨询、技术、运维、劳务等）\n"
            f"- 租赁合同：出租/承租场地、设备等的合同\n"
            f"- 其他：不属于以上四类的\n\n"
            f"合同标题：{title or '（无）'}\n\n"
            f"合同片段：\n{text_truncated}\n\n"
            f"严格输出纯 JSON：\n"
            f'{{"type": "xxx", "confidence": 0.xx}}'
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ═══════════════════════════════════════════════════════════
    # 融合策略：3 分支
    # ═══════════════════════════════════════════════════════════

    def _fuse(
        self,
        rule_type: str, rule_conf: float,
        llm_type: str, llm_conf: float,
        llm_failed: bool,
    ) -> ClassifyResult:
        """
        加权融合双通道结果。3 个分支：

        (a) LLM 失败 → 纯回退规则
        (b) LLM 成功 + conf ≥ 0.7 → 信任 LLM（一致加成 / 冲突轻微惩罚）
        (c) LLM 成功 + conf < 0.7 → 加权融合 ensemble
              · 一致 → 共识加成：加权平均 + 0.1，上限 0.9
              · 冲突 → 选高置信者 × 0.7 冲突惩罚
        """

        # ── (a) LLM 调用失败 → 纯回退规则 ──
        if llm_failed:
            logger.info(f"  🔄 LLM 调用失败 → 纯回退规则")
            return ClassifyResult(
                contract_type=rule_type,
                confidence=round(rule_conf, 3),
                rule_result=rule_type, rule_confidence=rule_conf,
                llm_result=llm_type, llm_confidence=llm_conf,
                llm_failed=True,
                method_used="rule_fallback",
            )

        # ── (b) LLM 成功 + 高置信 → 信任 LLM ──
        if llm_conf >= self.LLM_CONFIDENCE_THRESHOLD:
            if llm_type == rule_type:
                # 一致 → 加成（说明规则也支持，更可信）
                final_conf = min(0.98, llm_conf + 0.05)
                logger.info(f"  ✅ LLM 高置信 + 规则一致 → 信任 LLM (加成 conf={final_conf:.2f})")
            else:
                # 冲突 → 信任 LLM 但轻微惩罚（说明规则有不同意见）
                final_conf = llm_conf
                logger.info(f"  ✅ LLM 高置信 + 规则冲突 → 仍信任 LLM (conf={final_conf:.2f})")

            return ClassifyResult(
                contract_type=llm_type,
                confidence=round(final_conf, 3),
                rule_result=rule_type, rule_confidence=rule_conf,
                llm_result=llm_type, llm_confidence=llm_conf,
                llm_failed=False,
                method_used="llm",
            )

        # ── (c) LLM 成功 + 低置信 → 加权融合 ensemble ──
        # 规则和 LLM 都"不太确定" → 让两者互相增强或牵制
        if llm_type == rule_type:
            # 共识！虽然各自置信不高，但两者都指向同一结论 → 互相增强
            # 加权平均（规则因为是确定性 baseline，权重稍高 0.55 vs 0.45）
            weighted = rule_conf * 0.55 + llm_conf * 0.45
            final_conf = min(0.90, weighted + 0.1)  # 共识加成 +0.1，上限 0.9
            logger.info(
                f"  🔗 ensemble 共识: LLM({llm_type},conf={llm_conf}) + "
                f"规则({rule_type},conf={rule_conf}) → 加权平均={weighted:.2f} + 0.1 → {final_conf:.2f}"
            )
            final_type = llm_type  # 两者一致，用谁都行

        else:
            # 冲突！两者给出不同答案，且都不太确定
            # 选置信度较高的那个，但因为"对方有异议"，乘以冲突惩罚系数 0.85
            # （×0.7 太激进会让结果低于两个通道，×0.85 既表达惩罚又保留合理性）
            if llm_conf >= rule_conf:
                final_type = llm_type
                base_conf = llm_conf
            else:
                final_type = rule_type
                base_conf = rule_conf
            final_conf = round(base_conf * 0.85, 3)
            logger.info(
                f"  ⚖️ ensemble 冲突: LLM({llm_type},conf={llm_conf}) vs "
                f"规则({rule_type},conf={rule_conf}) → 选 {final_type}(base={base_conf:.2f} × 0.85 = {final_conf:.2f})"
            )

        return ClassifyResult(
            contract_type=final_type,
            confidence=round(final_conf, 3),
            rule_result=rule_type, rule_confidence=rule_conf,
            llm_result=llm_type, llm_confidence=llm_conf,
            llm_failed=False,
            method_used="ensemble",
        )


# ═════════════════════════════════════════════════════════════
# M21 · 提取后反查：类型强矛盾检测
#
# 定位：事后发现机制——分类错了（如"租赁"误判成"采购"）能在提取
#       完成后被抓出来。刻意只做"强矛盾"（全文零特征词）防止误伤：
#       租赁合同用"使用费"表述、采购合同不出现"采购"字样都常见，
#       弱矛盾只会造成人工审核噪音。
# 原则：只标记（needs_review），永不自动改判、永不触发重试。
# ═════════════════════════════════════════════════════════════

STRONG_TYPE_SIGNATURES = {
    "租赁合同": ["租赁", "出租", "承租", "租金", "租期", "押金", "使用费", " lease"],
    "采购合同": ["采购", "购买", "买方", "需方", "供货", "买卖", "订购"],
    "销售合同": ["销售", "卖方", "供方", "出售", "购销", "买方", "购买"],
    "服务合同": ["服务", "咨询", "运维", "技术", "劳务", "培训", "实施", "受托"],
}


def check_type_consistency(contract_type: str, text: str) -> list[str]:
    """
    校验分类结果与全文特征是否强矛盾。

    Returns:
        矛盾原因列表（空列表 = 无强矛盾）
    """
    if not contract_type or contract_type not in STRONG_TYPE_SIGNATURES or not text:
        return []
    if any(kw in text for kw in STRONG_TYPE_SIGNATURES[contract_type]):
        return []
    reason = (
        f"分类强矛盾: 判定为'{contract_type}'但全文无任何该类特征词，"
        f"分类可能错误，请人工确认"
    )
    logger.warning(f"🏷️ {reason}")
    return [reason]
