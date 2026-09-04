# ════════════════════════════════════════════════
# contract_ai.models — Python 模型导入顺序
# ════════════════════════════════════════════════

from . import contract                    # contract.contract 合同主模型
from . import contract_approval_log       # contract.approval.log 审批日志（P0-2）
from . import contract_payment_plan       # contract.payment.plan 收付款计划（M16 重写）
from . import contract_counterparty       # contract.counterparty 相对方档案
from . import contract_signatory          # contract.signatory 签约方档案
from . import contract_template           # contract.template 合同模板
from . import contract_element            # contract.element 提取的原子字段（M13）
from . import contract_config             # contract.config 系统配置（M17 编号规则）
