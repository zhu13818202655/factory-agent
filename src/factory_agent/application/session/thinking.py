"""Reasoning-transcript narration for ``interaction.thinking``.

Every frame is a **reviewed deterministic sentence**, never model output: the
pipeline states what it is doing from facts it already holds (the stage just
entered, the capability's reviewed title, the resolved window, the caller's
own range, the pager's page and row counters, the elapsed wait). Nothing on
the transcript is generated, so the narration costs zero LLM spend and cannot
fabricate a fact — the gate below exists because the sentences restate
numbers a slow external system produced, not because a model wrote them.

The gate is what makes the feature admissible at all. The transcript is shown
to every role (契约 §7-9: 内容脱敏), while this codebase forbids internal field
names, SQL, endpoint paths, credentials, and other tenants' data from leaving
their packages. So the safe direction is inverted from ordinary validation: the
narrator may say only what it was handed, and anything else is dropped rather
than rewritten — a half-edited sentence can invert its own meaning.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from factory_agent.ports import MesFetchProgress

#: Contract §3.2: one frame carries at most this many characters.
MAX_FRAME_CHARS = 200
#: Contract §3.2: one round's transcript is capped at this many characters.
MAX_TOTAL_CHARS = 8000

#: Reviewed blocklist. Each entry is a lowercased substring whose presence means
#: the frame escaped the fact boundary — an internal artifact name, or a
#: verbatim excerpt of a prompt. Kept deliberately short and auditable: a rule
#: nobody can explain does not belong in a redaction list.
_FORBIDDEN_LITERALS: tuple[str, ...] = (
    "select ",
    " from ",
    " where ",
    "join ",
    "group by",
    "order by",
    "insert ",
    "update ",
    "delete ",
    "drop ",
    "http://",
    "https://",
    "app_key",
    "appkey",
    "access_token",
    "accesstoken",
    "x-factory",
    "duckdb",
    "litellm",
    "sqlite",
    "prompt",
    "系统提示",
    "提示词",
    "字段名",
    "表名",
    "recipe",
    "data_api",
    ".yaml",
    ".json",
    "result.total",
    "code=",
    "message=",
)

#: Internal identifiers: the snake_case of capability ids, recipe step ids and
#: result column names, plus the ``fr0NN`` capability numbering. A narration
#: names the capability by its reviewed Chinese title instead.
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![a-z0-9])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![a-z0-9])"),
    re.compile(r"(?<![a-z0-9])fr\d{3}(?![a-z0-9])"),
)

#: A four-or-more digit figure reads as a business number; one the narrator was
#: not handed is a fabricated one.
_NUMBER_PATTERN = re.compile(r"\d[\d,\u00a0 ]*\d|\d")
_LONG_NUMBER_DIGITS = 4

#: Trailing run that could still be growing into an identifier. A frame
#: boundary must not cut one in half, or two individually clean frames would
#: reassemble into exactly the token the gate exists to block.
_TRAILING_TOKEN = re.compile(r"[A-Za-z0-9_.]+$")


@dataclass(frozen=True, slots=True)
class ThinkingFacts:
    """Display-safe facts for one narration step, and the gate's allowlist.

    ``lines`` is what the narrator may restate; the gate needs the bare digit
    runs the lines carry, so the allowlist is derived rather than
    hand-maintained.
    """

    stage: str
    lines: tuple[str, ...] = ()

    @property
    def numbers(self) -> frozenset[str]:
        found: set[str] = set()
        for line in self.lines:
            for match in _NUMBER_PATTERN.finditer(line):
                found.add(_digits(match.group()))
        return frozenset(found)

    def widened(self, sentence: str) -> "ThinkingFacts":
        """Add one display-safe sentence to the gate's allowlist.

        The widening is permanent, not per-call: once a row count is on screen
        it stays a fact of the round, and a gate that forgot would start
        dropping perfectly true sentences mid-round.
        """
        line = sentence.strip()
        if line in self.lines:
            return self
        return ThinkingFacts(stage=self.stage, lines=(*self.lines, line))

    def merged_with(self, other: "ThinkingFacts") -> "ThinkingFacts":
        """Union of two fact sets, keeping ``other``'s stage and first-seen order.

        The gate re-checks the *whole* transcript on every frame
        (:meth:`ThinkingCoalescer._reject`), so a step that replaced the
        allowlist with its own would start rejecting sentences that were
        already true when they were written — turning a stale allowlist into
        silence, since a rejected frame abandons the source. Steps therefore
        accumulate facts rather than own them.
        """
        lines = list(self.lines)
        for line in other.lines:
            if line not in lines:
                lines.append(line)
        return ThinkingFacts(stage=other.stage, lines=tuple(lines))


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def rejection_reason(text: str, facts: ThinkingFacts) -> str | None:
    """Why this text must not be sent, or ``None`` when it is safe.

    Returns a stable reason code for logging. The offending text is never
    logged: the reason a span was rejected is precisely the content that must
    not be recorded anywhere.
    """
    stripped = text.strip()
    if not stripped:
        return "empty"
    lowered = stripped.lower()
    for literal in _FORBIDDEN_LITERALS:
        if literal in lowered:
            return "internal_artifact"
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(lowered):
            return "internal_identifier"
    allowed = facts.numbers
    for match in _NUMBER_PATTERN.finditer(stripped):
        digits = _digits(match.group())
        if len(digits) >= _LONG_NUMBER_DIGITS and digits not in allowed:
            return "unsupported_number"
    return None


class ThinkingCoalescer:
    """Turn caller-authored sentences into contract-shaped frames.

    Owns three policies so the pipeline does not have to: the round's
    character budget, the frame boundary, and gate hand-off. It never invents
    text — a frame is a whole sentence as it was handed over, split only when
    it exceeds the frame limit.
    """

    def __init__(
        self,
        *,
        max_frame_chars: int = MAX_FRAME_CHARS,
        max_total_chars: int = MAX_TOTAL_CHARS,
    ) -> None:
        self._max_frame_chars = max_frame_chars
        self._max_total_chars = max_total_chars
        self._sent: list[str] = []
        self._last_sent = ""
        self._used = 0
        self._truncated = False
        self._stopped = False

    @property
    def text(self) -> str:
        """The transcript as the client will reassemble it (契约 §4)."""
        return "".join(self._sent)

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def stopped(self) -> bool:
        """True once the gate rejected something; the source is abandoned."""
        return self._stopped

    def commit(self, facts: ThinkingFacts, text: str) -> tuple[str, ...]:
        """Send caller-authored text as whole frames.

        Deterministic sentences (stage markers, fetch counters, wait clocks)
        are complete when they are handed over. They still spend the round's
        budget and still pass the gate, because the numbers they restate come
        from a slow external system that may answer with anything.
        """
        if self._stopped or self._truncated or not text.strip():
            return ()
        frames: list[str] = []
        rest = text
        while rest:
            room = self._max_total_chars - self._used
            if room <= 0:
                self._truncated = True
                return tuple(frames)
            piece = self._boundary(rest[: min(self._max_frame_chars, room)])
            if not piece:
                return tuple(frames)
            rest = rest[len(piece) :]
            reason = self._reject(piece, facts)
            if reason is not None:
                self._stopped = True
                return tuple(frames)
            accepted = self._accept(piece)
            if accepted is not None:
                frames.append(accepted)
            if self._used >= self._max_total_chars and rest:
                self._truncated = True
                return tuple(frames)
        return tuple(frames)

    def _accept(self, frame: str) -> str | None:
        """Record one frame, or ``None`` when it is empty or a repeat.

        Also owns the transcript's line shape (契约 §3.3): every row but the
        first opens on a new line, so a sentence can never be spliced into the
        middle of the row before it.
        """
        if not self._sent:
            frame = frame.lstrip("\n")
        stripped = frame.strip()
        if not stripped or stripped == self._last_sent:
            return None
        self._used += len(frame)
        self._last_sent = stripped
        self._sent.append(frame)
        return frame

    def _reject(self, frame: str, facts: ThinkingFacts) -> str | None:
        """Gate the frame against everything already accepted this round."""
        return rejection_reason(self.text + frame, facts)

    def _boundary(self, text: str) -> str:
        """Longest prefix within the frame limit that cuts no token in half."""
        window = text[: self._max_frame_chars]
        match = _TRAILING_TOKEN.search(window)
        if match is not None and match.start() > 0:
            return window[: match.start()]
        return window


def transcript_line(text: str) -> str:
    """Author one deterministic row on its own transcript line (契约 §3.3)."""
    return f"\n{text.strip()}"


class FetchProgressWatch:
    """How a wait is going, as fact sentences.

    Two sources, because the pager's counters alone cannot cover every wait:

    * every distinct ``(page, rows)`` mark the pager publishes, surfaced once
      each so a slow walk reports progress rather than repeating itself;
    * while no mark arrives, a sentence stating how long the caller has been
      waiting, emitted every ``wait_seconds``.

    The second source exists because a fetch that never pages — one large
    request answered in a single round trip — produces no counters at all, and
    that is the slowest and quietest window in the whole run. It reports the
    *total* wait rather than the time since the previous sentence: a repeating
    "已等待 5 秒" would both understate the wait and be dropped downstream as a
    repeat of the frame before it.

    ``wait_seconds=0`` keeps the counters-only behaviour, so the temporal
    sentence is opt-in from the pipeline rather than a property of every
    watch. ``wait_sentence`` rewords the temporal sentence per stage — the
    parse wait and the fetch wait describe different work, and both are fixed
    reviewed scripts rather than model output. It receives the elapsed whole
    seconds, so the sentence is never a verbatim repeat.

    The same watch also covers waits that have no pager at all (the parse
    call): with no observation ever made it degenerates into a pure wait
    clock.

    It belongs to this module rather than to the mixin that drives the
    transcript: the session mixin modules are scanned by an architecture test
    that attributes every ``self`` reference in a module to the layer defined
    there, and a plain helper would be read as a layer reaching outside itself.
    """

    def __init__(
        self,
        *,
        wait_seconds: float = 0.0,
        wait_sentence: Callable[[int], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait_seconds = wait_seconds
        self._wait_sentence_factory = wait_sentence or self._default_wait_sentence
        self._monotonic = monotonic
        started = self._monotonic()
        self._started_at = started
        self._reported_at = started
        self._latest: MesFetchProgress | None = None
        self._reported: tuple[int, int] | None = None
        self.observations = 0

    def observe(self, progress: MesFetchProgress) -> None:
        self._latest = progress
        self.observations += 1

    def fresh_sentence(self) -> str | None:
        latest = self._latest
        if latest is not None:
            mark = (latest.page, latest.rows)
            if mark != self._reported:
                self._reported = mark
                # A landed page is the newest thing there is to say, so it also
                # restarts the wait: the next temporal sentence becomes due only
                # after another full quiet interval.
                self._reported_at = self._monotonic()
                return self._page_sentence(latest)
        return self._wait_sentence()

    @staticmethod
    def _page_sentence(latest: MesFetchProgress) -> str:
        page = f"第 {latest.page} 页"
        rows = f"{latest.rows:,}"
        if latest.total:
            return f"正在逐页取回数据：已取回 {rows} 行，共 {latest.total:,} 行（{page}）"
        return f"正在逐页取回数据：已取回 {rows} 行（{page}）"

    @staticmethod
    def _default_wait_sentence(elapsed: int) -> str:
        return f"正在等待工厂系统返回数据，已等待 {elapsed} 秒。"

    def _wait_sentence(self) -> str | None:
        if self._wait_seconds <= 0.0:
            return None
        now = self._monotonic()
        if now - self._reported_at < self._wait_seconds:
            return None
        self._reported_at = now
        return self._wait_sentence_factory(int(now - self._started_at))


@dataclass(slots=True)
class ThinkingTranscript:
    """One round's transcript: its current facts, budget, and fragment numbering.

    ``seq`` is the contract's fragment counter and restarts at 1 every round,
    independently of the durable event id that carries each frame (契约 §3.2).
    The transcript outlives every step it describes, because a round emits one
    continuous block rather than one block per stage.
    """

    facts: ThinkingFacts
    #: ``False`` when narration is switched off: the round then emits nothing at
    #: all, so a deployment that does not want the extra call pays nothing for it.
    enabled: bool = True
    seq: int = 0
    last_event_sequence: int = 0
    coalescer: ThinkingCoalescer = field(default_factory=ThinkingCoalescer)
    #: Set by the first close, so a later terminal cannot emit a second ``done``.
    closed: bool = False

    @property
    def text(self) -> str:
        """The transcript as the client reassembles it (契约 §4)."""
        return self.coalescer.text

    @property
    def produced(self) -> bool:
        """Whether the round narrated anything a client could render."""
        return self.seq > 0
