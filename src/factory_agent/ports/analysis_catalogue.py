from dataclasses import dataclass

from sageql_core import DatasetDescriptor, FieldDescriptor, KnowledgeSnippet

from factory_agent.domain import Role

SEMANTIC_VERSION = "factory-analytics-v2.1"
FACT_DATASETS = frozenset({"reported_production", "production_progress"})
DICTIONARY_DATASETS = frozenset(
    {
        "styles",
        "style_types",
        "production_types",
        "processes",
        "style_processes",
        "style_colors",
        "style_sizes",
        "bill_processes",
        "hanging_lines",
    }
)


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    description: str
    fields: tuple[str, ...]
    metrics: tuple[str, ...]
    grain: str


_FACT_FIELDS = (
    "plan_code",
    "bed_code",
    "style_id",
    "style_code",
    "process_id",
    "process_name",
    "color",
    "size",
    "department",
    "business_date",
)
DATASETS: dict[str, DatasetSpec] = {
    "reported_production": DatasetSpec(
        "按报工时间的 V2 报工贡献；quantity 为件·工序。工资字段敏感，仅在可信宿主计算。",
        (*_FACT_FIELDS, "quantity", "payroll_amount", "source_type"),
        ("quantity", "payroll_amount", "process_id", "size", "plan_code"),
        "reported-contribution-default-detail",
    ),
    "production_progress": DatasetSpec(
        "按真实制单日期筛选的 V2 进度；实收为查询时累计值，单位件；"
        "未扫码贡献不证明完整配置工序集合。",
        (*_FACT_FIELDS, "bill_code", "package_code", "issued_quantity", "received_quantity"),
        ("issued_quantity", "received_quantity", "process_id", "size", "bill_code"),
        "flowcard-process-package-default-detail",
    ),
    "styles": DatasetSpec(
        "本租户款号目录；type_id 的年份档案桶不表示服装品类。",
        ("style_id", "style_code", "name", "type_id", "unit"),
        ("style_id",),
        "style",
    ),
    "style_types": DatasetSpec(
        "本租户货号档案分组。",
        ("type_id", "parent_type_id", "name", "deleted"),
        ("type_id",),
        "style-type",
    ),
    "production_types": DatasetSpec(
        "本租户真实生产类型字典。",
        ("type_id", "name", "deleted", "counts_cutting", "counts_stock"),
        ("type_id",),
        "type",
    ),
    "processes": DatasetSpec(
        "本租户工序字典；车种与工序名称是不同字段。",
        ("process_id", "name", "type_name", "vehicle_type", "flow_type", "deleted", "sort_order"),
        ("process_id",),
        "process",
    ),
    "style_processes": DatasetSpec(
        "款号配置工序；种类按非空编码去重，记录数独立计量。"
        "来源存在敏感工价字段，未进入通用分析字段或新公式。",
        (
            "configuration_id",
            "style_id",
            "style_code",
            "process_id",
            "name",
            "type_id",
            "type_name",
            "sort_order",
            "active",
            "last_process",
        ),
        ("process_id", "configuration_id"),
        "style-process-configuration",
    ),
    "style_colors": DatasetSpec(
        "完整款号颜色配置；不得当作床号实际颜色。",
        ("configuration_id", "style_id", "color"),
        ("color", "configuration_id"),
        "style-color",
    ),
    "style_sizes": DatasetSpec(
        "完整款号尺码配置；保留原始编码和版型，不合并 XXL 与 2XL。",
        ("configuration_id", "style_id", "size", "fit", "weight"),
        ("size", "configuration_id"),
        "style-size-fit",
    ),
    "bill_processes": DatasetSpec(
        "获准制单实体的完整配置工序；不能以款号配置替代。",
        (
            "configuration_id",
            "bill_code",
            "style_id",
            "style_code",
            "process_id",
            "name",
            "type_id",
            "type_name",
            "sort_order",
            "last_process",
        ),
        ("process_id", "configuration_id"),
        "bill-process-configuration",
    ),
    "hanging_lines": DatasetSpec(
        "本租户吊挂线别组别字典；无已确认部门关联，不据此扩大事实权限。",
        ("configuration_id", "system_name", "line_code", "group_name"),
        ("configuration_id", "line_code"),
        "hanging-line-group",
    ),
}

NUMERIC_FIELDS = frozenset({"quantity", "payroll_amount", "issued_quantity", "received_quantity"})
BOOLEAN_FIELDS = frozenset({"active", "deleted", "last_process", "counts_cutting", "counts_stock"})
INTEGER_FIELDS = frozenset({"sort_order"})
SENSITIVE_FIELDS = frozenset({"payroll_amount"})
FIELD_TITLES = {
    "plan_code": "计划号",
    "bed_code": "床号",
    "style_code": "款号",
    "style_id": "款号编码",
    "process_id": "工序编码",
    "process_name": "工序",
    "business_date": "业务日期",
    "quantity": "报工量",
    "payroll_amount": "工资合计",
    "size": "尺码",
    "color": "颜色",
    "department": "部门",
    "issued_quantity": "发卡数量",
    "received_quantity": "实收数量",
    "bill_code": "制单号",
    "name": "名称",
    "configuration_id": "配置记录",
    "process_id_count_distinct": "工序种类数",
    "size_count_distinct": "尺码种类数",
}


def descriptor(
    dataset_id: str, role: Role, *, self_only: bool = False, timezone: str = "Asia/Shanghai"
) -> DatasetDescriptor:
    spec = DATASETS[dataset_id]
    return DatasetDescriptor(
        dataset_id=dataset_id,
        description=spec.description,
        fields=tuple(
            FieldDescriptor(
                name=name,
                data_type="decimal"
                if name in NUMERIC_FIELDS
                else "boolean"
                if name in BOOLEAN_FIELDS
                else "integer"
                if name in INTEGER_FIELDS
                else "date"
                if name == "business_date"
                else "string",
                description=FIELD_TITLES.get(name, name),
                sensitive=name in SENSITIVE_FIELDS,
                unit="件·工序"
                if name == "quantity"
                else "件"
                if name in {"issued_quantity", "received_quantity"}
                else "元"
                if name == "payroll_amount"
                else None,
            )
            for name in spec.fields
        ),
        metrics=spec.metrics,
        grain=spec.grain,
        contract_version="mes-v2" if dataset_id in FACT_DATASETS else "mes-dictionary-v1",
        rule_version=SEMANTIC_VERSION,
        available=True,
        scope_state="self"
        if (role is Role.EMPLOYEE or self_only) and dataset_id in FACT_DATASETS
        else "mes_filtered"
        if dataset_id in FACT_DATASETS
        else "authorized",
        timezone=timezone,
    )


KNOWLEDGE: tuple[KnowledgeSnippet, ...] = (
    KnowledgeSnippet(
        reference="knowledge:reported-production:v2",
        version=SEMANTIC_VERSION,
        source="docs/product/需求及方案整理.md",
        text="报工量合计使用 V2 已确认数量贡献，单位件·工序。多员工和多工序不能随意去重；"
        "报工时间与制单日期不同，床号上月报工优先 reported_production，不以进度筛选订单。",
    ),
    KnowledgeSnippet(
        reference="knowledge:progress:v2",
        version=SEMANTIC_VERSION,
        source="docs/api/生产进度-V2.md",
        text="生产进度以真实制单日期为窗口；发卡与实收单位为件，实收是查询时累计值，"
        "不是窗口内新增报工。完工率为实收数合计/发卡数合计×100；"
        "工序贡献、未扫码行和工资里发生的工序都不能证明全床完整配置工序集合。",
    ),
    KnowledgeSnippet(
        reference="knowledge:configuration:v1",
        version=SEMANTIC_VERSION,
        source="docs/product/AI问答对外接口-整理.md",
        text="款号工序读取 style_processes，款号尺码读取 style_sizes；配置问题无需事实时间窗口。"
        "工序种类是非空工序编码去重数，配置记录数单列；停用配置需回显 active 条件或追问。"
        "尺码保留原始编码和版型；配置包含尚未报工项目。",
    ),
    KnowledgeSnippet(
        reference="knowledge:process-type:v1",
        version=SEMANTIC_VERSION,
        source="docs/product/AI问答对外接口-整理.md",
        text="工序是配置的生产步骤，车种是该工序的设备/工艺类型；生产类型是本厂配置字典。"
        "文档示例不能代表本厂当前列表。工序到生产类型的名称关联不证明稳定编码关系；"
        "style_processes 或 bill_processes 的 type_id 可与 production_types.type_id 验证关联。",
    ),
    KnowledgeSnippet(
        reference="knowledge:bed-code:v2",
        version=SEMANTIC_VERSION,
        source="docs/api/工资产量-V2.md",
        text="床号使用字符串精确筛选，不合并前导零。床号可能对应多个制单和计划号。"
        "报工结果只涵盖用户窗口和可见范围；空集不表示床号不存在。床号制单配置需从"
        "获准 production_progress 解析 bill_code，再查 bill_processes，不直接信任用户编号。",
    ),
    KnowledgeSnippet(
        reference="knowledge:scope:v2",
        version=SEMANTIC_VERSION,
        source="docs/product/需求及方案整理.md",
        text="基础字典与配置读取不限制角色，仍限当前租户。生产事实沿用现有角色和数据范围。"
        "员工只分析本人事实；组长、管理读取绑定部门；老板读取本厂。"
        "跨两族事实关联需要真实键、基数、日期与覆盖证明，当前不启用可能重复累计的关联。",
    ),
)
