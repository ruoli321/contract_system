# ════════════════════════════════════════════════
# B5 付款条款解析 + B1 大写金额换算 测试
# ════════════════════════════════════════════════
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.payment_schedule import parse_payment_terms
from app.services.chinese_amount import chinese_uppercase_to_amount


# ════════════════════ B5 付款条款解析 ════════════════════

class TestPaymentSchedule:
    def test_standard_three_installments(self):
        """典型采购合同：30% 预付 + 60% 到货验收 + 10% 质保金"""
        terms = (
            "合同签订后5个工作日内支付30%预付款174000元；"
            "设备到货并验收合格后10个工作日内支付60%即348000元；"
            "剩余10%即58000元作为质保金，于质保期届满后30日内无息支付"
        )
        nodes = parse_payment_terms(terms)
        assert len(nodes) == 3

        assert nodes[0]["milestone"] == "prepayment"
        assert nodes[0]["ratio"] == 0.30
        assert nodes[0]["amount"] == 174000.0
        assert nodes[0]["days_after_sign"] == 5

        assert nodes[1]["milestone"] in ("on_delivery", "on_completion")
        assert nodes[1]["ratio"] == 0.60

        assert nodes[2]["milestone"] == "final"
        assert nodes[2]["ratio"] == 0.10
        assert nodes[2]["amount"] == 58000.0

    def test_ratio_only(self):
        """只有比例没有金额：30%预付款 + 70%验收款"""
        nodes = parse_payment_terms("合同签订后支付30%预付款；验收合格后支付70%验收款")
        assert len(nodes) == 2
        assert nodes[0]["ratio"] == 0.30
        assert nodes[0]["amount"] is None
        assert nodes[1]["ratio"] == 0.70

    def test_amount_only_fixed(self):
        """固定金额（租赁月付）"""
        nodes = parse_payment_terms("每月支付租金 5,000 元，共 12 期")
        assert len(nodes) == 1
        assert nodes[0]["amount"] == 5000.0

    def test_chinese_ratio(self):
        """中文比例：百分之三十"""
        nodes = parse_payment_terms("合同生效后支付百分之三十预付款")
        assert len(nodes) == 1
        assert nodes[0]["ratio"] == 0.30

    def test_empty_and_no_match(self):
        """空文本 / 无付款信息 → 空列表"""
        assert parse_payment_terms("") == []
        assert parse_payment_terms(None) == []
        assert parse_payment_terms("本合同无付款安排") == []

    def test_wan_unit(self):
        """万元变体：支付 5 万元"""
        nodes = parse_payment_terms("签订后支付 5 万元预付款")
        assert len(nodes) == 1
        assert nodes[0]["amount"] == 50000.0

    def test_rent_quarterly_expand(self):
        """租金展开：月租+年租+按季度（押一付三）→ 4 期 × 季度租金"""
        terms = (
            "月租金45000元，年租金540000元，按季度支付，"
            "采用押一付三方式，乙方应于每季度首月5日前将当季租金足额支付至甲方指定账户"
        )
        nodes = parse_payment_terms(terms)
        assert len(nodes) == 4
        assert nodes[0]["milestone"] == "installment"
        assert nodes[0]["amount"] == 135000.0
        assert sum(n["amount"] for n in nodes) == 540000.0
        assert nodes[0]["name"] != nodes[1]["name"]  # 期数区分

    def test_rent_monthly_only(self):
        """只有月租金 + 按月支付 → 12 期"""
        nodes = parse_payment_terms("月租金3000元，按月支付")
        assert len(nodes) == 12
        assert nodes[0]["amount"] == 3000.0

    def test_rent_yearly_no_period(self):
        """只有年租金、无支付周期 → 1 期年租金"""
        nodes = parse_payment_terms("年租金 120,000 元")
        assert len(nodes) == 1
        assert nodes[0]["amount"] == 120000.0
        assert nodes[0]["milestone"] == "installment"

    def test_rent_split_sentences_with_deposit(self):
        """LLM 重组场景：租金金额与周期分离在不同分句 + 押金句应被排除"""
        terms = (
            "月租金45000元，年租金540000元；"
            "按季度支付，押一付三，每季度首月5日前支付当季租金；"
            "押金100000元，合同签订后5日内支付。"
        )
        nodes = parse_payment_terms(terms)
        assert len(nodes) == 4
        assert all(n["milestone"] == "installment" for n in nodes)
        assert nodes[0]["amount"] == 135000.0
        assert sum(n["amount"] for n in nodes) == 540000.0  # 押金 100000 不计入

    def test_rent_uppercase_paren_amount(self):
        """真实合同格式：大写金额附括号小写（月租金…￥45,000.00）→ 4 期展开

        回归：此前正则只认「月租金45000元」直接数字形式，导致
        「月租金为人民币肆万伍仟元整（￥45,000.00）」全文展开失败、
        付款计划生成 0 节点（contract id=117）。
        """
        terms = (
            "该房屋月租金为人民币肆万伍仟元整（￥45,000.00），"
            "年租金总额为人民币伍拾肆万元整（￥540,000.00），"
            "租赁期内甲方不得单方上调租金。"
            "租金按季度支付，采用押一付三方式，乙方应于每季度首月5日前"
            "将当季租金足额支付至甲方指定账户。"
            "甲方指定收款账户：开户银行为中国建设银行北京朝阳支行，"
            "银行账号为11050189563600001234，户名为北京城建置业发展有限公司。"
        )
        nodes = parse_payment_terms(terms)
        assert len(nodes) == 4
        assert nodes[0]["milestone"] == "installment"
        assert nodes[0]["amount"] == 135000.0
        assert sum(n["amount"] for n in nodes) == 540000.0
        # 银行账号等无关数字不得被误解析为付款节点
        assert all("11050189563600001234" not in n["name"] for n in nodes)


# ════════════════════ B1 大写金额换算 ════════════════════

class TestChineseAmount:
    def test_basic_wan(self):
        assert chinese_uppercase_to_amount("人民币壹佰贰拾捌万元整") == 1280000.0

    def test_plain(self):
        assert chinese_uppercase_to_amount("伍拾万元整") == 500000.0

    def test_with_ling(self):
        assert chinese_uppercase_to_amount("伍拾万零伍佰元") == 500500.0

    def test_yi(self):
        assert chinese_uppercase_to_amount("壹亿贰仟万元整") == 120000000.0

    def test_yuan_jiao_fen(self):
        assert chinese_uppercase_to_amount("壹佰元贰角叁分") == 100.23

    def test_full_digits(self):
        assert chinese_uppercase_to_amount("壹拾贰万叁仟肆佰伍拾陆元整") == 123456.0

    def test_invalid(self):
        assert chinese_uppercase_to_amount("500,000 元") is None
        assert chinese_uppercase_to_amount("") is None
        assert chinese_uppercase_to_amount(None) is None

    def test_cross_check_sample(self):
        """金标准样例：总价 580,000 元 ↔ 大写伍拾捌万元整"""
        assert chinese_uppercase_to_amount("伍拾捌万元整") == 580000.0
