"""Topic resolution — manifests plus config in, a `WiringPlan` or a clear error out.

design/broker.md §4. A pure function: no processes, no filesystem, no imports of plugin code.
That is deliberate — exclusivity is enforced *before any process starts*, where an error
message is most useful and where there is no race to lose.

Every fatal here is read by someone who cannot see the code that caused it, so each one names
the plugins involved and shows the paste-ready TOML that resolves it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from .config import LoadedConfig
from .loader import Discovery
from .manifest import Manifest, Publish, Subscribe
from .topics import (
    PAYLOAD_NAMES,
    STANDARD_TOPICS,
    Exclusivity,
    Kind,
    TopicDescriptor,
)
from .contracts import PoseFrame

__all__ = ["WiringError", "Subscription", "PluginPlan", "WiringPlan", "resolve", "HOST"]

HOST = "<host>"

_DEFAULT_NONCONFLATING_DEPTH = 64
_MIN_STREAM_DEPTH = 4


class WiringError(Exception):
    """The graph cannot be wired. Fatal, and the message is the documentation."""


@dataclass(frozen=True, slots=True)
class Subscription:
    topic: str
    conflate: bool
    depth: int
    mode: str
    required: bool


@dataclass(frozen=True, slots=True)
class PluginPlan:
    plugin_id: str
    manifest: Manifest | None            # None for HOST
    publishes: tuple[str, ...]
    subscribes: tuple[Subscription, ...]
    stream_depth: int
    provides_topology: str | None        # resolved to a single id where determinable
    config: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WiringPlan:
    topics: dict[str, TopicDescriptor]
    publishers: dict[str, tuple[str, ...]]      # topic -> publisher ids
    plugins: dict[str, PluginPlan]
    absent: tuple[str, ...]
    warnings: tuple[str, ...]

    def subscribers(self, topic: str) -> tuple[str, ...]:
        return tuple(
            pid for pid, plan in self.plugins.items()
            if any(s.topic == topic for s in plan.subscribes)
        )


def _describe(manifest: Manifest | None, pid: str) -> str:
    if manifest is None:
        return f"  {pid:<22}(the embedding application)"
    where = "(built in)" if manifest.root == "bundled" else str(manifest.directory)
    return f"  {pid:<22}{manifest.version:<8}{where:<34}{manifest.name}"


def _descriptor_from_publish(pub: Publish, declared_by: str) -> TopicDescriptor:
    payload = PAYLOAD_NAMES[pub.payload] if pub.payload else None
    if pub.kind is None or pub.exclusivity is None or payload is None:
        raise WiringError(
            f"Plugin {declared_by!r} declares the non-standard topic {pub.topic!r} but does "
            "not describe it. A topic the standard set does not define needs kind, "
            "exclusivity, payload and doc:\n\n"
            f"    [[publishes]]\n"
            f'    topic       = "{pub.topic}"\n'
            '    kind        = "stream"      # or "event"\n'
            '    exclusivity = "shared"      # or "exclusive"\n'
            f'    payload     = "record"      # one of {sorted(PAYLOAD_NAMES)}\n'
            '    doc         = "One line describing what this carries."\n'
        )
    return TopicDescriptor(
        name=pub.topic, kind=pub.kind, exclusivity=pub.exclusivity,
        schema=f"{pub.payload}/1", payload=payload, unit=pub.unit,
        record_kind=pub.record_kind, standard=False, declared_by=declared_by,
        doc=pub.doc,
    )


def _merge(a: TopicDescriptor, b: TopicDescriptor) -> TopicDescriptor:
    """Step 1. Declarations of the same name must agree on the contract-bearing fields."""
    for attr, label in (
        ("kind", "kind"),
        ("exclusivity", "exclusivity"),
        ("schema", "payload type"),
        ("unit", "unit"),
        ("record_kind", "record_kind"),
    ):
        av, bv = getattr(a, attr), getattr(b, attr)
        if av == bv:
            continue
        if a.standard or b.standard:
            std, other = (a, b) if a.standard else (b, a)
            raise WiringError(
                f"climb-cv cannot start: plugin {other.declared_by!r} re-declares the "
                f"standard topic {a.name!r} with a different {label}.\n\n"
                f"  standard set:            {label} = {getattr(std, attr)!r}\n"
                f"  {other.declared_by!r} declares: {label} = {getattr(other, attr)!r}\n\n"
                "The standard set defines this topic so that any publisher interoperates "
                "with any subscriber. A plugin needing different semantics should declare "
                "its own topic name instead."
            )
        raise WiringError(
            f"climb-cv cannot start: two plugins declare the topic {a.name!r} with "
            f"different {label}, so it is not one topic.\n\n"
            f"  {a.declared_by!r}: {label} = {av!r}\n"
            f"  {b.declared_by!r}: {label} = {bv!r}\n\n"
            f"Both are publishing to the same name, so subscribers cannot rely on either. "
            f"The two authors need to agree, or one should rename its topic.\n\n"
            "You can force one interpretation locally:\n\n"
            f'    [topics."{a.name}"]\n'
            f'    {attr} = {av!r}\n'
        )
    return a if a.standard else b


def _did_you_mean(name: str, known: set[str]) -> str:
    close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.6)
    return f" Did you mean {close[0]!r}?" if close else ""


def resolve(
    discovery: Discovery,
    cfg: LoadedConfig,
    *,
    host_publishes: tuple[str, ...] = (),
    host_subscribes: tuple[Subscription, ...] = (),
) -> WiringPlan:
    """Steps 1–8 of broker.md §4.2."""
    warnings: list[str] = []
    manifests = {m.id: m for m in discovery.plugins}

    # --- step 1: descriptors -------------------------------------------------------
    descriptors: dict[str, TopicDescriptor] = dict(STANDARD_TOPICS)
    for m in discovery.plugins:
        for pub in m.publishes:
            if pub.topic in STANDARD_TOPICS and pub.payload is None:
                continue  # publishing a standard topic needs no re-description
            desc = _descriptor_from_publish(pub, m.id)
            existing = descriptors.get(pub.topic)
            descriptors[pub.topic] = _merge(existing, desc) if existing else desc

    # Local overrides for author-declared topics, so two upstream authors disagreeing does
    # not leave the user unable to run anything (broker.md §4.4).
    for name, section in cfg.topics.items():
        desc = descriptors.get(name)
        if desc is None or desc.standard:
            continue
        changes = {}
        if "kind" in section:
            changes["kind"] = Kind(section["kind"])
        if "exclusivity" in section:
            changes["exclusivity"] = Exclusivity(section["exclusivity"])
        if changes:
            from dataclasses import replace

            was = desc
            descriptors[name] = replace(desc, **changes)
            if (
                was.exclusivity is Exclusivity.EXCLUSIVE
                and descriptors[name].exclusivity is Exclusivity.SHARED
            ):
                warnings.append(
                    f"{cfg.source}: [topics.{name}] widens exclusivity from exclusive to "
                    "shared. Subscribers written against one-message-per-cycle may now "
                    "receive several per cycle from different publishers, which is a "
                    "different delivery contract than they were written for."
                )

    # --- step 2: candidate publishers ----------------------------------------------
    candidates: dict[str, list[str]] = {}
    for m in discovery.plugins:
        for pub in m.publishes:
            candidates.setdefault(pub.topic, []).append(m.id)
    for topic in host_publishes:
        candidates.setdefault(topic, []).append(HOST)

    all_subs: dict[str, list[tuple[str, Subscribe | Subscription]]] = {}
    for m in discovery.plugins:
        for sub in m.subscribes:
            all_subs.setdefault(sub.topic, []).append((m.id, sub))
    for sub in host_subscribes:
        all_subs.setdefault(sub.topic, []).append((HOST, sub))

    known_names = set(descriptors) | set(candidates) | set(all_subs)

    # An unknown topic name is fatal regardless of `required`: a name nothing declares is a
    # typo, and `required = false` means "this feature may be absent", not "I may have
    # misspelled this". Splitting the two jobs is what lets a known-but-absent topic be fine.
    for topic, subs in all_subs.items():
        if topic in descriptors or topic in candidates:
            continue
        who = ", ".join(sorted({pid for pid, _ in subs}))
        raise WiringError(
            f"climb-cv cannot start: {who} subscribes to {topic!r}, which no plugin "
            f"publishes and which is not in the standard topic set."
            f"{_did_you_mean(topic, set(descriptors))}\n\n"
            f"Known topics: {sorted(descriptors)}"
        )

    # --- steps 3–4: resolve publishers ---------------------------------------------
    publishers: dict[str, tuple[str, ...]] = {}
    absent: list[str] = []
    for topic in sorted(set(descriptors) | set(candidates)):
        desc = descriptors.get(topic)
        cands = candidates.get(topic, [])
        if desc is None:
            continue
        if not cands:
            absent.append(topic)
            publishers[topic] = ()
            continue
        if desc.exclusivity is Exclusivity.SHARED:
            publishers[topic] = tuple(cands)
            continue

        pinned = cfg.topics.get(topic, {}).get("publisher")
        if pinned is not None:
            if pinned in cands:
                publishers[topic] = (pinned,)
                continue
            if pinned in manifests or pinned == HOST:
                warnings.append(
                    f"{cfg.source}: [topics.{topic}] pins publisher {pinned!r}, which does "
                    "not publish that topic. Ignoring the pin."
                )
            else:
                # Skipped for a benign reason (disabled, wrong platform) must not be fatal:
                # a config written on another machine should not stop the app.
                warnings.append(
                    f"{cfg.source}: [topics.{topic}] pins publisher {pinned!r}, which is "
                    "not available in this setup (disabled, skipped, or not installed). "
                    f"Falling back to the remaining candidate(s): {sorted(cands)}."
                )
            if len(cands) == 1:
                publishers[topic] = (cands[0],)
                continue

        if len(cands) == 1:
            publishers[topic] = (cands[0],)
            continue

        lines = "\n".join(_describe(manifests.get(c), c) for c in sorted(cands))
        raise WiringError(
            f"climb-cv cannot start: {len(cands)} plugins both want to publish the "
            f"exclusive topic {topic!r}.\n\n{lines}\n\n"
            f"{topic!r} is exclusive: exactly one publisher, because "
            f"{_why_exclusive(desc)}\n\n"
            "Choose one in climbcv.toml:\n\n"
            f'    [topics."{topic}"]\n'
            f'    publisher = "{sorted(cands)[0]}"\n\n'
            "Or disable the one you don't want:\n\n"
            f"    [plugins.{sorted(cands)[1]}]\n"
            "    enabled = false\n"
        )

    # --- step 5: starved required subscriptions ------------------------------------
    for topic in absent:
        starved = [pid for pid, sub in all_subs.get(topic, []) if sub.required]
        if not starved:
            continue
        overridable = (
            f'    [topics."{topic}"]\n    required = false\n'
        )
        raise WiringError(
            f"climb-cv cannot start: {', '.join(sorted(starved))} require the topic "
            f"{topic!r}, but nothing publishes it in this setup.\n\n"
            f"{_absent_hint(topic, discovery)}\n"
            "If those plugins can run usefully without it, you can say so:\n\n"
            f"{overridable}"
        )

    # --- step 6: topology ----------------------------------------------------------
    resolved_topology: dict[str, str | None] = {}
    for topic, pubs in publishers.items():
        desc = descriptors[topic]
        if desc.payload is not PoseFrame:
            continue
        subs = all_subs.get(topic, [])
        for pid in pubs:
            m = manifests.get(pid)
            if m is None:
                continue
            if not m.provides_topology:
                raise WiringError(
                    f"climb-cv cannot start: plugin {pid!r} publishes {topic!r}, whose "
                    "payload is a pose, but does not declare provides_topology.\n\n"
                    "A pose payload's landmark indices are meaningless without knowing "
                    "which topology they belong to — index 11 is a shoulder under "
                    "mediapipe.pose.33 and a hip under coco.17, and getting it wrong draws "
                    "a wrong skeleton every frame with no error.\n\n"
                    "    [plugin]\n"
                    '    provides_topology = "mediapipe.pose.33"\n\n'
                    "A stage that passes through whatever it receives may declare several:\n\n"
                    '    provides_topology = ["mediapipe.pose.33", "coco.17"]\n'
                )
        for sub_pid, sub in subs:
            m = manifests.get(sub_pid)
            if m is None:
                continue
            if m.requires_topology is None:
                raise WiringError(
                    f"climb-cv cannot start: plugin {sub_pid!r} subscribes to {topic!r}, "
                    "whose payload is a pose, but does not declare requires_topology.\n\n"
                    "Declare the topology whose landmark indices you index:\n\n"
                    "    [plugin]\n"
                    '    requires_topology = "mediapipe.pose.33"\n\n'
                    "Or, if you do not index individual joints (a recorder, a filter that "
                    "treats columns uniformly), opt out explicitly:\n\n"
                    '    requires_topology = "any"\n'
                )
            if m.requires_topology == "any":
                continue
            for pub_pid in pubs:
                pm = manifests.get(pub_pid)
                if pm is None or not pm.provides_topology:
                    continue
                if m.requires_topology not in pm.provides_topology:
                    raise WiringError(
                        f"climb-cv cannot start: topology mismatch on {topic!r}.\n\n"
                        f"  {pub_pid!r} provides: {list(pm.provides_topology)}\n"
                        f"  {sub_pid!r} requires: {m.requires_topology!r}\n\n"
                        f"{sub_pid!r} indexes landmarks that {pub_pid!r} does not produce, so "
                        "every joint it reads would be a different body part. Use a pose "
                        f"plugin providing {m.requires_topology!r}, or a version of "
                        f"{sub_pid!r} that supports {list(pm.provides_topology)}."
                    )

    for pid, m in manifests.items():
        # A single-valued provides_topology is the resolved value; a multi-valued one is
        # resolved from the upstream publisher at runtime by the stage itself, since a
        # pass-through publishes whatever it received.
        resolved_topology[pid] = (
            m.provides_topology[0] if len(m.provides_topology) == 1 else None
        )

    # --- B1 / F-1: multi-publisher and shared-topic latest() warnings ---------------
    for topic, pubs in publishers.items():
        if len(pubs) < 2:
            continue
        for pid, _sub in all_subs.get(topic, []):
            warnings.append(
                f"Plugin {pid!r} subscribes to {topic!r}, which has {len(pubs)} publishers "
                f"in this setup: {', '.join(sorted(pubs))}.\n"
                f"  If {pid} reads it with self.latest({topic!r}) it will see whichever "
                "published most recently and the two will alternate. Use "
                f"self.latest_by_source({topic!r}), which returns {{source_id: payload}}, "
                "to see both."
            )

    # Guardian-02 finding 9: the warning above fires on *deployment shape*, so an author
    # testing with one publisher installed never sees it and ships anyway. Exclusivity is a
    # static declared property, so warn on the author's own machine too.
    for topic, subs in all_subs.items():
        desc = descriptors.get(topic)
        if desc is None or desc.exclusivity is not Exclusivity.SHARED:
            continue
        for pid, sub in subs:
            if getattr(sub, "mode", "handler") == "latest" and len(publishers.get(topic, ())) < 2:
                warnings.append(
                    f"Plugin {pid!r} reads the SHARED topic {topic!r} with latest(). Only "
                    "one publisher is installed here, so it works today — but latest() "
                    "collapses across publishers, and a user who installs a second one will "
                    f"see them alternate. self.latest_by_source({topic!r}) is stable under "
                    "either setup."
                )

    # --- step 7: queue depths ------------------------------------------------------
    plans: dict[str, PluginPlan] = {}
    max_depth = int(cfg.framework["max_stream_depth"])
    configured = int(cfg.framework["stream_depth"])

    def build(pid: str, manifest: Manifest | None,
              subs: list[Subscribe | Subscription]) -> PluginPlan:
        stream_subs = sum(
            1 for s in subs
            if descriptors[s.topic].kind is Kind.STREAM and getattr(s, "conflate", True)
        )
        if configured > 0:
            depth = min(configured, max_depth)
        else:
            # Scales with subscription count: a constant depth lets a 30fps `frame` burst
            # evict another topic's pending message before its handler ever runs.
            depth = min(max(_MIN_STREAM_DEPTH, 2 * stream_subs), max_depth)
        wired = tuple(
            Subscription(
                topic=s.topic,
                conflate=getattr(s, "conflate", True),
                depth=min(
                    getattr(s, "depth", 0) or _DEFAULT_NONCONFLATING_DEPTH, max_depth
                ) if not getattr(s, "conflate", True) else depth,
                mode=getattr(s, "mode", "handler"),
                required=s.required,
            )
            for s in subs
            if descriptors[s.topic] is not None
        )
        return PluginPlan(
            plugin_id=pid,
            manifest=manifest,
            publishes=tuple(
                p.topic for p in (manifest.publishes if manifest else ())
            ) if manifest else tuple(host_publishes),
            subscribes=wired,
            stream_depth=depth,
            provides_topology=resolved_topology.get(pid),
            config=cfg.section(pid),
        )

    for pid, m in manifests.items():
        plans[pid] = build(pid, m, list(m.subscribes))
    if host_publishes or host_subscribes:
        plans[HOST] = build(HOST, None, list(host_subscribes))

    return WiringPlan(
        topics=descriptors,
        publishers=publishers,
        plugins=plans,
        absent=tuple(absent),
        warnings=tuple(warnings),
    )


def _why_exclusive(desc: TopicDescriptor) -> str:
    if desc.name.startswith("pose."):
        return (
            "subscribers compute geometry from a single skeleton and two would contradict "
            "each other."
        )
    if desc.name == "frame":
        return (
            "there is one feed: with two publishers, every downstream frame_seq reference "
            "would be ambiguous."
        )
    if desc.name == "app.status":
        return "lifecycle statements come from the framework, so a plugin cannot forge them."
    return (
        "its payload is a singleton observation of a unique subject, so two publishers "
        "would contradict rather than add."
    )


def _absent_hint(topic: str, discovery: Discovery) -> str:
    for skip in discovery.skipped:
        if skip.level == "INFO":
            return (
                f"The most likely reason: {skip.identifier} was skipped — {skip.reason}\n"
            )
    return (
        "Either no installed plugin publishes it, or the one that would was disabled or "
        "skipped.\n"
    )
