from typing import Any, Protocol

from sageql_core import ApprovedObservation, KnowledgeSnippet, RunContext, RunControl

from factory_agent.ports.analysis import AnalysisRequest
from factory_agent.ports.analysis_data import LoadedDataset
from factory_agent.ports.result_table import ResultTable
from factory_agent.ports.sandbox import InteractionSandboxPort


class AnalysisHostPort(Protocol):
    request: AnalysisRequest
    sandbox: InteractionSandboxPort
    results: dict[str, ResultTable]
    knowledge: dict[str, KnowledgeSnippet]

    async def validate(self, context: RunContext, control: RunControl | None = None) -> None: ...

    def get_dataset(self, reference: str) -> LoadedDataset: ...

    def authorize_metric(self, field: str) -> None: ...

    def register_result(self, result: ResultTable) -> str: ...

    def observation(self, **values: Any) -> ApprovedObservation: ...
