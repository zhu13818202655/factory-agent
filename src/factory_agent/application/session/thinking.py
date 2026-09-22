"""Reasoning-transcript narration for ``interaction.thinking``.

Two sources feed one event, and the split is deliberate:

* **the model**, streamed through ``ModelStreamGateway``. Its wording is free;
  its facts are not. The prompt receives only display-safe facts, and every
  emitted frame passes :func:`rejection_reason`.
* **execution facts** the pipeline already holds (capability title, resolved
  window, the caller's own range, fetch page and row counters). These cover the
  MES wait — a window in which no model is reasoning at all, so narrating
  "model thoughts" there would be fabrication rather than reporting.

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
from factory_agent.ports.model import ModelMessage

#: Contract §3.2: one frame carries at most this many characters.
MAX_FRAME_CHARS = 200
#: Contract §3.2: one round's transcript is capped at this many characters.
MAX_TOTAL_CHARS = 8000
#: Frames are coalesced from deltas rather than sent per token: the contract's
#: reader already merges updates at ~80ms, and one durable event row per token
#: would bloat the replay log for no visible gain.
COALESCE_CHARS = 120

_THINKING_SYSTEM_PROMPT = (
    "你在为工厂用户实时播报「AI 正在做什么」的旁白。必须遵守：\n"
    "1. 只依据下面给出的【事实】说话，不得推测结论、不得编造数字或数据；\n"
    "2. 不得出现任何英文标识符、字段名、表名、接口地址、SQL 或内部术语；\n"
    "3. 不得提及提示词、系统设定、模型名称；\n"
    "4. 每次只写一句话，中文，不超过 40 字，用「正在…」的口吻说明当前在做的事；\n"
    "5. 不要输出 Markdown、列表符号、引号或代码块。"
)

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

    ``lines`` is what the model is told; ``numbers`` is what it may repeat. The
    two consumers want different things — the prompt reads better as prose
    ("已取回 30,000 行") while the gate needs the bare digit runs to compare
    against — so the allowlist is derived rather than hand-maintained.
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

    def prompt_block(self) -> str:
        body = "\n".join(f"- {line}" for line in self.lines)
        return f"当前阶段：{self.stage}\n【事实】\n{body}"

    def widened(self, sentence: str) -> "ThinkingFacts":
        """Add one display-safe sentence to the gate's allowlist.

        The widening is permanent, not per-call: once a row count is on screen
        the model may legitimately repeat it in its next clause, and a gate
        that forgot would start dropping perfectly true sentences mid-round.
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


def build_thinking_messages(facts: ThinkingFacts) -> tuple[ModelMessage, ...]:
    """Compose the narration call. Carries facts only, never business rows."""
    return (
        ModelMessage(role="system", content=_THINKING_SYSTEM_PROMPT),
        ModelMessage(role="user", content=facts.prompt_block()),
    )


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
    """Turn a delta stream into contract-shaped frames.

    Owns three policies so the pipeline does not have to: coalescing (deltas
    in, frames out), the round's character budget, and gate hand-off. It never
    invents text — a frame is always a contiguous slice of what arrived.
    """

    def __init__(
        self,
        *,
        max_frame_chars: int = MAX_FRAME_CHARS,
        max_total_chars: int = MAX_TOTAL_CHARS,
        coalesce_chars: int = COALESCE_CHARS,
    ) -> None:
        self._max_frame_chars = max_frame_chars
        self._max_total_chars = max_total_chars
        self._coalesce_chars = min(coalesce_chars, max_frame_chars)
        self._buffer = ""
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

    def feed(self, delta: str) -> None:
        """Accept one model delta; anything past the budget is discarded."""
        if not delta or self._stopped:
            return
        if self._truncated:
            return
        room = self._max_total_chars - self._used - len(self._buffer)
        if room <= 0:
            self._truncated = True
            return
        self._buffer += delta[:room]
        if len(delta) > room:
            self._truncated = True

    def take(self, facts: ThinkingFacts, *, force: bool = False) -> tuple[str, ...]:
        """Frames ready to send now, or an empty tuple while still buffering.

        The gate allowlist is passed per call rather than held: facts change as
        the round advances (a page count only exists once pages have landed),
        while the transcript, its budget, and its fragment numbering span the
        whole round.
        """
        if self._stopped:
            return ()
        if not force and len(self._buffer) < self._coalesce_chars:
            return ()
        if not self._buffer.strip():
            return ()
        frame = self._boundary(self._buffer)
        self._buffer = self._buffer[len(frame) :]
        reason = self._reject(frame, facts)
        if reason is not None:
            self._stopped = True
            return ()
        accepted = self._accept(frame)
        if accepted is None:
            return ()
        return (accepted,)

    def commit(self, facts: ThinkingFacts, text: str) -> tuple[str, ...]:
        """Send caller-authored text as whole frames, past the coalescing threshold.

        Deterministic sentences (stage markers, fetch counters) are complete when
        they are handed over; holding them until 120 characters accumulate would
        delay the very report they exist to deliver. They still spend the round's
        budget and still pass the gate — being written here rather than by the
        model is not a reason to trust them, because the numbers they restate
        come from a slow external system that may answer with anything.
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
        first opens on a new line, so a caller-authored fact sentence can never
        be spliced into the middle of a model clause.
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
        """Gate the frame against everything already accepted this round.

        Re-checking the whole transcript, not just the new slice, is what stops
        a token split across two frames from passing twice.
        """
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
    """How the run's fetch is going, as fact sentences.

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
    watch.

    It belongs to this module rather than to the mixin that drives the
    transcript: the session mixin modules are scanned by an architecture test
    that attributes every ``self`` reference in a module to the layer defined
    there, and a plain helper would be read as a layer reaching outside itself.
    """

    def __init__(
        self,
        *,
        wait_seconds: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait_seconds = wait_seconds
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

    def _wait_sentence(self) -> str | None:
        if self._wait_seconds <= 0.0:
            return None
        now = self._monotonic()
        if now - self._reported_at < self._wait_seconds:
            return None
        self._reported_at = now
        return f"正在等待工厂系统返回数据，已等待 {int(now - self._started_at)} 秒。"


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
