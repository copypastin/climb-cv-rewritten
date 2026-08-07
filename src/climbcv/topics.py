"""Topic descriptors and the standard topic set.

design/broker.md §2–§3. A topic is fully described by a `TopicDescriptor`; descriptors are
*declarations* and come from two places uniformly — the framework declares the standard set,
any plugin declares topics it invents. Framework-declared is not framework-owned: the
framework declares `holds.boxes` so any hold detector interoperates with any overlay
without the two authors ever meeting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from . import contracts

__all__ = [
    "Kind",
    "Exclusivity",
    "TopicDescriptor",
    "STANDARD_TOPICS",
    "TOPIC_RE",
    "RESERVED_TOPIC_PREFIXES",
    "PAYLOAD_NAMES",
    "validate_topic_name",
]


TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
TOPIC_MAX_LEN = 64
RESERVED_TOPIC_PREFIXES = ("climbcv.",)


class Kind(Enum):
    """Delivery and backpressure policy."""

    STREAM = "stream"  # conflating by default; latest value wins
    EVENT = "event"    # every message matters; queued, drop-newest when full


class Exclusivity(Enum):
    """How many publishers may exist.

    The criterion, not an enumeration (Decision #12) — an enumeration is a structural
    ceiling in disguise, because the next person adding a topic has no way to decide:

        A topic is EXCLUSIVE iff its payload is a singleton observation of a unique
        subject, such that two simultaneous publishers would make mutually
        CONTRADICTORY, not merely ADDITIVE, statements about the world.

    Two skeletons for one climber contradict each other. The union of two detectors' hold
    boxes is still a valid set of hold boxes. Any plugin may declare a topic exclusive;
    this is not a framework-owned privilege.
    """

    EXCLUSIVE = "exclusive"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class TopicDescriptor:
    name: str
    kind: Kind
    exclusivity: Exclusivity
    schema: str                 # diagnostic id for the payload contract, e.g. "pose/1"
    payload: type               # a climbcv.contracts type
    unit: str | None            # required iff payload is Scalar
    record_kind: str | None     # required iff payload is Record
    standard: bool              # True for the shipped interop vocabulary
    declared_by: str            # "core" | plugin id — for conflict error messages
    doc: str                    # one line, shown by `climbcv topics`

    def contract_fields(self) -> str:
        """The fields that actually constitute this topic's contract, for diagnostics.

        `schema` alone is insufficient for record topics: it is "record/1" for every
        record topic in the ecosystem (guardian-02 blocker 2).
        """
        extra = ""
        if self.unit is not None:
            extra = f", unit={self.unit}"
        elif self.record_kind is not None:
            extra = f", record_kind={self.record_kind}"
        return f"{self.payload.__name__}, schema={self.schema}{extra}"


def validate_topic_name(name: str, where: str) -> None:
    """Raise ValueError with the offending string quoted, per broker.md §2.1."""
    if len(name) > TOPIC_MAX_LEN:
        raise ValueError(
            f"{where}: topic name {name!r} is {len(name)} chars, over the "
            f"{TOPIC_MAX_LEN} limit"
        )
    if not TOPIC_RE.match(name):
        raise ValueError(
            f"{where}: topic name {name!r} is not a valid name. Grammar: "
            "lowercase letters, digits and underscores, dot-separated segments, "
            "each starting with a letter — e.g. 'acme.grip_force'."
        )
    for prefix in RESERVED_TOPIC_PREFIXES:
        if name.startswith(prefix):
            raise ValueError(
                f"{where}: topic name {name!r} uses the reserved prefix {prefix!r}, "
                "which is framework-internal and never author-facing."
            )


PAYLOAD_NAMES: dict[str, type] = {
    "frame": contracts.Frame,
    "pose": contracts.PoseFrame,
    "holdboxes": contracts.HoldBoxes,
    "scalar": contracts.Scalar,
    "shutdown": contracts.Shutdown,
    "status": contracts.Status,
    "record": contracts.Record,
}
"""Manifest `payload = "<name>"` → contract type. Nothing outside this mapping can cross a
topic: a plugin-defined class pickles under the publishing plugin's module name and would
resolve against a different plugin's module in the subscriber. `record` is the escape hatch
that keeps the vocabulary open without that hazard."""


def _std(
    name: str,
    kind: Kind,
    exclusivity: Exclusivity,
    schema: str,
    payload: type,
    doc: str,
    *,
    unit: str | None = None,
    record_kind: str | None = None,
) -> TopicDescriptor:
    return TopicDescriptor(
        name=name, kind=kind, exclusivity=exclusivity, schema=schema, payload=payload,
        unit=unit, record_kind=record_kind, standard=True, declared_by="core", doc=doc,
    )


STANDARD_TOPICS: dict[str, TopicDescriptor] = {
    d.name: d
    for d in (
        _std(
            "frame", Kind.STREAM, Exclusivity.EXCLUSIVE, "frame/1", contracts.Frame,
            "Captured video frames.",
            # There is one feed. "Which one is *the* frame?" has no answer with two
            # publishers, and every downstream frame_seq reference would be ambiguous.
        ),
        _std(
            "pose.raw", Kind.STREAM, Exclusivity.EXCLUSIVE, "pose/1", contracts.PoseFrame,
            "Unsmoothed pose estimate, one per frame.",
        ),
        _std(
            "pose.smoothed", Kind.STREAM, Exclusivity.EXCLUSIVE, "pose/1",
            contracts.PoseFrame, "Smoothed pose estimate, one per frame.",
            # One canonical skeleton: downstream geometry is computed *from* a single
            # skeleton, so two are contradictory rather than additive.
        ),
        _std(
            "holds.boxes", Kind.STREAM, Exclusivity.SHARED, "holds.boxes/1",
            contracts.HoldBoxes,
            "Detected hold bounding boxes. Set-valued and additive — several detectors "
            "may publish; distinguish them with Meta.source.",
            # SHARED per Decision #12. The union of two detectors' boxes is still a valid
            # set of boxes, the plural case is ordinary (a model plus a hand-annotated
            # route map), and reversibility decided the tie: shared->exclusive later is a
            # config change, exclusive->shared breaks every subscriber's delivery contract.
        ),
        _std(
            "device.lid_angle", Kind.STREAM, Exclusivity.EXCLUSIVE, "scalar/1",
            contracts.Scalar, "Laptop lid hinge angle. 0 == closed.",
            unit="degree",
            # Singleton observation of one hinge. A phone-gyro equivalent is a *different*
            # subject and belongs on a different topic.
        ),
        _std(
            "app.shutdown", Kind.EVENT, Exclusivity.SHARED, "shutdown/1",
            contracts.Shutdown, "A request to stop the run. Anyone may ask.",
        ),
        _std(
            "app.status", Kind.EVENT, Exclusivity.EXCLUSIVE, "status/1", contracts.Status,
            "Framework lifecycle notifications. Published by the host only.",
            # EXCLUSIVE, closing guardian-02 finding 11: this topic had no stated
            # exclusivity, and implementing it forces the choice. Exclusive means a plugin
            # declaring app.status gets the ordinary contention error rather than the
            # ability to forge lifecycle events -- Status.plugin_id is an author-supplied
            # payload field, so without this a plugin could publish
            # Status(plugin_id="core.capture", state="crashed") and any status UI keyed on
            # the payload field would believe it. The general rule: app.* is
            # framework-published. Not reversible later -- exclusive->shared is breaking
            # for every subscriber -- which is why it is settled here rather than left to
            # whoever implements the supervisor.
        ),
    )
}
