"""Proposed addition to DataHub: `datahub/specific/mlmodel.py`.

`datahub.specific` ships patch builders for chart, dashboard, dataJob,
dataProduct, dataset, form, and structuredProperty — but not for any ML entity.
The aspect-helper mixins that make those builders work (`HasOwnershipPatch`,
`HasTagsPatch`, `HasStructuredPropertiesPatch`, …) are entity-agnostic, so an
`mlModel` builder is composition rather than new machinery.

Without it, mutating structured properties on an `mlModel` means either
hand-rolling a `MetadataPatchProposal` subclass downstream — which is what
Undertow does today — or falling back to a full-aspect overwrite that clobbers
concurrent writers.

Mirrors `DataProductPatchBuilder`'s shape exactly; no new patterns introduced.
"""

from typing import Optional

from datahub.emitter.mcp_patch_builder import MetadataPatchProposal
from datahub.metadata.schema_classes import (
    KafkaAuditHeaderClass,
    SystemMetadataClass,
)
from datahub.specific.aspect_helpers.custom_properties import HasCustomPropertiesPatch
from datahub.specific.aspect_helpers.domains import HasDomainsPatch
from datahub.specific.aspect_helpers.institutional_memory import (
    HasInstitutionalMemoryPatch,
)
from datahub.specific.aspect_helpers.ownership import HasOwnershipPatch
from datahub.specific.aspect_helpers.structured_properties import (
    HasStructuredPropertiesPatch,
)
from datahub.specific.aspect_helpers.tags import HasTagsPatch
from datahub.specific.aspect_helpers.terms import HasTermsPatch


class MLModelPatchBuilder(
    HasOwnershipPatch,
    HasCustomPropertiesPatch,
    HasStructuredPropertiesPatch,
    HasTagsPatch,
    HasTermsPatch,
    HasDomainsPatch,
    HasInstitutionalMemoryPatch,
    MetadataPatchProposal,
):
    """Patch builder for `mlModel` entities."""

    def __init__(
        self,
        urn: str,
        system_metadata: Optional[SystemMetadataClass] = None,
        audit_header: Optional[KafkaAuditHeaderClass] = None,
    ) -> None:
        super().__init__(
            urn,
            system_metadata=system_metadata,
            audit_header=audit_header,
        )
