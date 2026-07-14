"""Strategic Development department knowledge MCP simulator."""

from enterprise_wecom_digital_employee.department_knowledge.models import (
    KnowledgeDocument,
    KnowledgeSearchHit,
    SearchMode,
)
from enterprise_wecom_digital_employee.department_knowledge.repository import DepartmentKnowledgeRepository
from enterprise_wecom_digital_employee.department_knowledge.settings import DepartmentKnowledgeSettings

__all__ = [
    "DepartmentKnowledgeRepository",
    "DepartmentKnowledgeSettings",
    "KnowledgeDocument",
    "KnowledgeSearchHit",
    "SearchMode",
]
