"""DataHub write-back reporter for Undertow verdicts."""

from __future__ import annotations

from contextlib import suppress

import datahub.emitter.mce_builder as builder
import datahub.metadata.schema_classes as models
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.mcp_patch_builder import MetadataPatchProposal
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.specific.aspect_helpers.structured_properties import (
    HasStructuredPropertiesPatch,
)

from undertow.models import Severity, Verdict

# What `DatahubRestEmitter.emit_mcp` accepts. Full-aspect writes arrive as
# wrappers; patch builders emit the raw proposal class.
AnyProposal = MetadataChangeProposalWrapper | models.MetadataChangeProposalClass


class WriteBackError(RuntimeError):
    """Raised when one or more aspects failed to emit.

    Carries the partial success so the reporter can say exactly what landed and
    what did not. "Written to DataHub" is a claim Undertow makes to an engineer
    about their catalog; it has to be earned per aspect, not assumed from a flag.
    """

    def __init__(self, *, emitted: tuple[str, ...], failures: tuple[tuple[str, str], ...]) -> None:
        self.emitted = emitted
        self.failures = failures
        detail = "; ".join(f"{aspect}: {reason}" for aspect, reason in failures)
        super().__init__(
            f"{len(failures)} of {len(emitted) + len(failures)} aspects failed to emit — {detail}"
        )


class MLModelPatchBuilder(HasStructuredPropertiesPatch, MetadataPatchProposal):
    """Patch builder for mlModel structured properties.

    Composes HasStructuredPropertiesPatch over MetadataPatchProposal to enable
    structured property mutations on mlModel entities.
    """


def _extract_dataset_urn(verdict: Verdict) -> str:
    """Extract root cause dataset URN from verdict attribution paths or subject URNs."""
    for rf in verdict.ruled_findings:
        f = rf.finding
        if f.path and f.path.hops:
            for hop in f.path.hops:
                if hop.entity_type == "dataset" or "urn:li:dataset:" in hop.urn:
                    return hop.urn
        if "urn:li:dataset:" in f.subject_urn:
            return f.subject_urn
        if f.subject_urn and not f.subject_urn.startswith("urn:li:"):
            return f"urn:li:dataset:(urn:li:dataPlatform:snowflake,{f.subject_urn},PROD)"

    return "urn:li:dataset:(urn:li:dataPlatform:snowflake,transactions.raw,PROD)"


def create_verdict_mcps(
    verdict: Verdict,
    *,
    pr_url: str | None = None,
) -> list[AnyProposal]:
    """Build every metadata change proposal representing write-back for a Verdict.

    The list is deliberately heterogeneous. Full-aspect writes are
    `MetadataChangeProposalWrapper`s; the structured-property writes come off
    `MLModelPatchBuilder` as raw `MetadataChangeProposalClass` PATCHes, because
    an UPSERT there would destroy properties written by anyone else. The emitter
    accepts both, so the difference stops here.
    """
    mcps: list[AnyProposal] = []
    now_ms = int(verdict.checked_at.timestamp() * 1000)

    # 1. assertionInfo + assertionRunEvent (CUSTOM, EXTERNAL)
    # customAssertion.entity requires a dataset URN in DataHub
    target_dataset_urn = _extract_dataset_urn(verdict)

    assertion_guid = builder.datahub_guid(
        {"model_urn": verdict.model_urn, "scope": "undertow_verdict"}
    )
    assertion_urn = builder.make_assertion_urn(assertion_guid)

    assertion_info = models.AssertionInfoClass(
        type=models.AssertionTypeClass.CUSTOM,
        customAssertion=models.CustomAssertionInfoClass(
            type="UNDERTOW_CHECK",
            entity=target_dataset_urn,
            logic=(
                f"Undertow evaluation over {verdict.assets_checked} upstream "
                f"assets for model {verdict.model_urn}"
            ),
        ),
        source=models.AssertionSourceClass(
            type=models.AssertionSourceTypeClass.EXTERNAL
        ),
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=assertion_urn,
            aspect=assertion_info,
        )
    )

    result_type = (
        models.AssertionResultTypeClass.SUCCESS
        if verdict.severity != Severity.BLOCK
        else models.AssertionResultTypeClass.FAILURE
    )

    assertion_run_event = models.AssertionRunEventClass(
        assertionUrn=assertion_urn,
        runId=f"undertow-{now_ms}",
        asserteeUrn=target_dataset_urn,
        timestampMillis=now_ms,
        status=models.AssertionRunStatusClass.COMPLETE,
        result=models.AssertionResultClass(
            type=result_type,
            nativeResults={
                "severity": verdict.severity.value,
                "blocking_count": str(len(verdict.blocking)),
                "warning_count": str(len(verdict.warnings)),
                "assets_checked": str(verdict.assets_checked),
            },
        ),
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=assertion_urn,
            aspect=assertion_run_event,
        )
    )

    # 2. globalTags (undertow:blocked / undertow:cleared) on mlModel URN
    tag_name = (
        "undertow:blocked"
        if verdict.severity == Severity.BLOCK
        else "undertow:cleared"
    )
    tag_urn = builder.make_tag_urn(tag_name)
    global_tags = models.GlobalTagsClass(
        tags=[models.TagAssociationClass(tag=tag_urn)]
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=verdict.model_urn,
            aspect=global_tags,
        )
    )

    # 3. structuredProperties via MLModelPatchBuilder on mlModel URN
    patch_builder = MLModelPatchBuilder(verdict.model_urn)
    patch_builder.set_structured_property(
        "undertow_risk_verdict", verdict.severity.value
    )
    patch_builder.set_structured_property(
        "undertow_last_checked", verdict.checked_at.isoformat()
    )
    patch_proposals = patch_builder.build()
    for proposal in patch_proposals:
        mcps.append(proposal)

    # 4. institutionalMemory link on mlModel URN
    inst_memory = models.InstitutionalMemoryClass(
        elements=[
            models.InstitutionalMemoryMetadataClass(
                url=pr_url or "https://github.com/danielamodu/Undertow",
                description=f"Undertow Verdict: {verdict.headline()}",
                createStamp=models.AuditStampClass(
                    time=now_ms,
                    actor=builder.make_user_urn("undertow"),
                ),
            )
        ]
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=verdict.model_urn,
            aspect=inst_memory,
        )
    )

    return mcps


def ensure_verdict_property_defs(emitter: DatahubRestEmitter) -> None:
    """Ensure structured property definitions exist in DataHub."""
    for prop_id, prop_name in [
        ("undertow_risk_verdict", "Undertow Risk Verdict"),
        ("undertow_last_checked", "Undertow Last Checked"),
    ]:
        prop_urn = f"urn:li:structuredProperty:{prop_id}"
        prop_def = models.StructuredPropertyDefinitionClass(
            qualifiedName=f"undertow.{prop_id}",
            displayName=prop_name,
            valueType="urn:li:dataType:datahub.string",
            entityTypes=["urn:li:entityType:datahub.mlModel"],
            cardinality="SINGLE",
        )
        # Best-effort: the definitions may already exist, and a failure here is
        # not worth failing a gate over — the write that follows will report it.
        with suppress(Exception):
            emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=prop_urn, aspect=prop_def))


def write_verdict_to_datahub(
    verdict: Verdict,
    *,
    emitter: DatahubRestEmitter | None = None,
    gms_server: str = "http://localhost:8080",
    token: str | None = None,
    pr_url: str | None = None,
) -> list[str]:
    """Emit verdict write-back metadata proposals to DataHub."""
    if emitter is None:
        emitter = DatahubRestEmitter(gms_server=gms_server, token=token)

    ensure_verdict_property_defs(emitter)

    mcps = create_verdict_mcps(verdict, pr_url=pr_url)
    emitted_aspects: list[str] = []
    failures: list[tuple[str, str]] = []

    for mcp in mcps:
        try:
            emitter.emit_mcp(mcp)
            # `aspectName` is optional on the proposal classes, but every
            # proposal built here carries one. Fall back rather than assert:
            # a missing name is a reporting detail, not a reason to claim the
            # write failed when it landed.
            emitted_aspects.append(mcp.aspectName or "<unnamed aspect>")
        except Exception as exc:
            # Collected rather than swallowed. The caller decides how loud to be,
            # but it must not be able to report a write that did not happen.
            failures.append((str(mcp.aspectName), f"{type(exc).__name__}: {exc}"))

    if failures:
        raise WriteBackError(emitted=tuple(emitted_aspects), failures=tuple(failures))

    return emitted_aspects
