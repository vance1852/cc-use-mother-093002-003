"""定义技术首发与受限披露服务的领域词汇。"""

# 披露申请状态：草拟 -> 核验 -> 会签 -> 限时预览 -> 正式公开，另含撤回与更正两个终态。
APPLICATION_STATUSES = frozenset({
    "draft",
    "verifying",
    "countersign",
    "preview",
    "published",
    "withdrawn",
    "corrected",
})

# 会签职责及其对应的操作者角色，三者不能相互代替批准。
DUTIES = ("research", "legal", "media")
DUTY_ROLE = {"research": "researcher", "legal": "legal", "media": "media"}

# 受众关系类别。
RELATIONSHIPS = frozenset({"investor", "media", "partner", "regulator", "other"})

# 字段敏感级别。
SENSITIVITIES = frozenset({"public", "restricted", "confidential"})

# 专利或监管前置事项类别。
PREREQUISITE_KINDS = frozenset({"patent", "regulatory"})
PREREQUISITE_STATUSES = frozenset({"pending", "cleared"})

# 冻结原因：权利异议或新的监管限制。
FREEZE_REASONS = frozenset({"rights_objection", "regulatory_restriction"})

# 下载凭证状态。
CREDENTIAL_STATUSES = frozenset({"active", "exhausted", "expired", "revoked", "frozen"})

VERSION_STATUSES = frozenset({"active", "superseded", "withdrawn"})
