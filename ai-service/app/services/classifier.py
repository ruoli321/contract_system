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
import time
from dataclasses import dataclass

logger = logging.getLogger("classifier")


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
    method_used: str           # "llm" / "ensemble" / "rule_fallback"


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
    # 规则词典：每个合同类型的关键词
    # ═════════════════════════════════════════════════════════
    RULE_KEYWORDS = {
        "采购合同": ["采购", "购买", "买卖合同", "供货", "采购方", "需方", "采购协议", "采购订单"],
        "销售合同": ["销售", "出售", "卖方", "供方", "销售协议", "客户合同", "销售订单"],
        "服务合同": ["服务", "咨询", "技术支持", "运维", "劳务", "项目服务", "服务协议", "实施", "培训"],
        "租赁合同": ["租赁", "出租", "承租", "租金", "租期", "场地租赁", "设备租赁", "租赁协议"],
    }

    # 标题优先匹配（标题里有就基本坐实，权重 +5 分）
    RULE_TITLE_PATTERNS = {
        "采购合同": ["采购合同", "采购协议", "设备采购", "材料采购", "买卖合同", "采购订单"],
        "销售合同": ["销售合同", "销售协议", "产品销售", "销售订单"],
        "服务合同": ["服务合同", "服务协议", "技术服务", "咨询服务", "劳务合同", "运维服务"],
        "租赁合同": ["租赁合同", "租赁协议", "场地租赁", "设备租赁", "房屋租赁"],
    }

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
        rule_type, rule_conf = self._rule_classify(contract_text, contract_title)
        logger.debug(f"  📋 规则分类: {rule_type} (置信 {rule_conf:.2f})")

        # ── 通道 2：LLM 语义分类（可能失败）──
        llm_type, llm_conf, llm_failed = self._llm_classify(contract_text, contract_title)
        if llm_failed:
            logger.warning(f"  ⚠️  LLM 调用失败，将回退到规则结果")
        else:
            logger.debug(f"  🤖 LLM 分类: {llm_type} (置信 {llm_conf:.2f})")

        # ── 融合：严格按回退策略 ──
        result = self._fuse(rule_type, rule_conf, llm_type, llm_conf, llm_failed)

        elapsed = time.time() - t_start
        logger.info(
            f"✅ 分类完成 | 最终类型={result.contract_type} | "
            f"置信度={result.confidence:.2f} | 方法={result.method_used} | "
            f"耗时={elapsed:.2f}s"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    # 通道 1：规则匹配
    # ═══════════════════════════════════════════════════════════

    def _rule_classify(self, text: str, title: str) -> tuple[str, float]:
        """
        规则关键词匹配。永远成功，作为 baseline 和兜底。

        Returns:
            (contract_type, confidence)
        """
        scores: dict[str, float] = {}

        # 1. 标题匹配（权重高 +5 分）
        if title:
            for ctype, patterns in self.RULE_TITLE_PATTERNS.items():
                for p in patterns:
                    if p in title:
                        scores[ctype] = scores.get(ctype, 0) + 5

        # 2. 正文关键词频率（每个关键词出现次数累加）
        for ctype, keywords in self.RULE_KEYWORDS.items():
            for kw in keywords:
                count = text.count(kw)
                if count > 0:
                    scores[ctype] = scores.get(ctype, 0) + count

        # 没有任何关键词命中 → "其他"，低置信度
        if not scores:
            return "其他", 0.3

        # 找得分最高的类型
        best_type = max(scores, key=scores.get)
        sorted_scores = sorted(scores.values(), reverse=True)
        max_score = sorted_scores[0]
        total = sum(scores.values())

        # 置信度计算：第一名明显高于第二名时更信任
        if len(sorted_scores) >= 2 and sorted_scores[0] > sorted_scores[1] * 2:
            # 压倒性优势 → 较高置信度
            confidence = min(0.85, max(0.5, max_score / total + 0.2))
        elif max_score >= 5:
            # 多次命中 → 中等置信度
            confidence = 0.6
        elif max_score >= 2:
            confidence = 0.5
        else:
            confidence = 0.4

        return best_type, round(confidence, 3)

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

    def _build_llm_messages(self, text: str, title: str) -> list[dict]:
        """
        构造发给 LLM 的 messages 列表。
        优先用 PromptManager.render("classify", ...) 获取 system+user；
        若 prompt_manager 未注入则用降级硬编码 prompt。
        """
        # 截断过长文本（避免超 token）
        text_truncated = text[:3000] if len(text) > 3000 else text

        if self.pm is not None:
            try:
                rendered = self.pm.render(
                    "classify",
                    contract_text=text_truncated,
                    contract_title=title or "（无）",
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
