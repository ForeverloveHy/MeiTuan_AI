from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
import re

from .evidence_matcher import EvidenceMatcher
from .evidence_units import EvidenceUnit
from .schema import KnowledgeClaim, KnowledgeItem



def _compact(text: object) -> str:
    return "".join(str(text or "").lower().split())

def _pattern_values(patterns: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for pat in patterns or []:
        for key in ("all", "any"):
            for value in pat.get(key, []) or []:
                t = str(value or "").strip()
                if t:
                    values.append(t)
        for value in pat.get("regex_any", []) or []:
            t = str(value or "").strip()
            if t:
                values.append(t)
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out




_DIRECTION_LOW_MARKERS = (
    "更便宜", "较便宜", "便宜", "成本更低", "更低", "较低", "低一些",
)
_DIRECTION_HIGH_MARKERS = (
    "更贵", "较贵", "贵一些", "略高", "更高", "较高", "高一些",
)


def _direction_polarities(text: object) -> set[str]:
    """Return coarse comparative direction signals from Chinese schema/text.

    The markers are generic comparative adjectives, not business objects.  We
    deliberately avoid treating a bare single character such as ``低`` or ``高``
    as a direction, because product names may contain those characters.
    """
    t = _compact(text)
    out: set[str] = set()
    if any(_compact(m) and _compact(m) in t for m in _DIRECTION_LOW_MARKERS):
        out.add("low")
    if any(_compact(m) and _compact(m) in t for m in _DIRECTION_HIGH_MARKERS):
        out.add("high")
    return out


def _opposite_direction(a: set[str], b: set[str]) -> bool:
    return ("low" in a and "high" in b) or ("high" in a and "low" in b)

def _numeric_ranges_from_text(text: object) -> list[tuple[float, float]]:
    """Extract numeric ranges from schema values or dialogue text.

    This is intentionally generic and unit-agnostic.  It only compares numbers
    after the surrounding claim/object anchor has already been matched by the
    schema.  Examples: ``5到10秒``, ``5-10秒``, ``1~2``.
    """
    raw = str(text or "")
    compact = _compact(raw)
    out: list[tuple[float, float]] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:到|至|[-~～—－])\s*(\d+(?:\.\d+)?)", raw):
        a = float(m.group(1))
        b = float(m.group(2))
        if a > b:
            a, b = b, a
        out.append((a, b))
    # Some sources normalize spaces/dashes away before reaching the judge.
    if not out:
        for m in re.finditer(r"(\d+(?:\.\d+)?)(?:到|至|[-~～—－])(\d+(?:\.\d+)?)", compact):
            a = float(m.group(1))
            b = float(m.group(2))
            if a > b:
                a, b = b, a
            out.append((a, b))
    seen: set[tuple[float, float]] = set()
    unique: list[tuple[float, float]] = []
    for r in out:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _range_overlaps(a: tuple[float, float], b: tuple[float, float], *, tolerance: float = 0.0) -> bool:
    return not (a[1] < b[0] - tolerance or b[1] < a[0] - tolerance)


def _is_numeric_or_unit_term(text: str) -> bool:
    t = _compact(text)
    if not t:
        return True
    if any(ch.isdigit() for ch in t):
        return True
    return len(t) <= 1

def _symbolic_unit_terms(value: object) -> set[str]:
    """Extract generic placeholder+unit terms from schema or dialogue text.

    LongCat schemas often use anonymized placeholders such as X/Y/W plus a
    Chinese unit.  In natural Chinese there may be no delimiter after the unit
    (for example a following verb is attached).  Keep the full match and also
    short placeholder+unit prefixes so different placeholder values with the same unit can conflict even when the
    sentence continues immediately after the unit.
    """
    text = _compact(value)
    out: set[str] = set()
    for m in re.finditer(r"(?<![a-z0-9_])([a-z])([一-鿿]{1,4})", text):
        prefix, unit = m.group(1), m.group(2)
        out.add(prefix + unit)
        if unit:
            out.add(prefix + unit[:1])
        if len(unit) >= 2:
            out.add(prefix + unit[:2])
    return out


def _has_symbolic_unit_mismatch(support_values: list[object], text: str) -> bool:
    support_terms: set[str] = set()
    bare_support_values = {_compact(v) for v in support_values if _compact(v)}
    for value in support_values:
        support_terms.update(_symbolic_unit_terms(value))
    if not support_terms:
        return False
    text_terms = _symbolic_unit_terms(text)
    if not text_terms:
        return False
    support_units = {re.sub(r"^[a-z][a-z0-9_]{0,3}", "", term) for term in support_terms}
    # If the schema also lists the bare unit as acceptable evidence, a different
    # placeholder with the same unit may belong to a neighboring condition in the
    # same sentence.  Do not turn that ambiguous case into a hard conflict.
    if any(unit and unit in bare_support_values for unit in support_units):
        return False
    for term in text_terms:
        if term in support_terms:
            continue
        unit = re.sub(r"^[a-z][a-z0-9_]{0,3}", "", term)
        if unit and unit in support_units:
            return True
    return False




def _has_alternative_placeholder_without_support(support_values: list[object], text: str) -> bool:
    """Detect a placeholder value asserted where the support placeholder is absent.

    This is weaker than same-unit mismatch and is only used when the schema does
    not also accept the bare unit as evidence.  It handles generic anonymized
    cases where a claim expects one placeholder token, but the utterance anchors
    the claim and asserts a different placeholder condition instead.
    """
    support_terms: set[str] = set()
    bare_support_values = {_compact(v) for v in support_values if _compact(v)}
    for value in support_values:
        support_terms.update(_symbolic_unit_terms(value))
    if not support_terms:
        return False
    support_units = {re.sub(r"^[a-z][a-z0-9_]{0,3}", "", term) for term in support_terms}
    if any(unit and unit in bare_support_values for unit in support_units):
        return False
    text_terms = _symbolic_unit_terms(text)
    if not text_terms:
        return False
    if any(term in text_terms for term in support_terms):
        return False
    support_prefixes = {re.match(r"^[a-z][a-z0-9_]{0,3}", term).group(0) for term in support_terms if re.match(r"^[a-z][a-z0-9_]{0,3}", term)}
    for term in text_terms:
        m = re.match(r"^[a-z][a-z0-9_]{0,3}", term)
        if m and m.group(0) not in support_prefixes:
            return True
    return False

def _safe_anchor_variants(term: str) -> list[str]:
    """Build generic object anchors from schema terms.

    No business words are stored here.  For a schema object such as a four-char
    Chinese label, the first/last two chars are useful when the dialogue uses a
    shortened form.  Attribute/value terms are mostly short or numeric and are
    filtered before this function is called.
    """
    t = _compact(term)
    if not t:
        return []
    out = [t]
    if re.fullmatch(r"[\u4e00-\u9fff]{4,}", t):
        # The prefix often carries the differentiating object label; the suffix
        # is frequently a generic category word and can over-bind siblings.
        out.append(t[:2])
    return [x for i, x in enumerate(out) if x and x not in out[:i]]



def _split_schema_phrase(value: str) -> list[str]:
    """Split a schema phrase into reusable semantic fragments.

    The fragments are derived from the schema phrase itself.  This is not a
    business dictionary; it only lets table values match separated wording
    when those values are already supplied by the current schema.
    """
    t = _compact(value)
    if not t:
        return []
    # ASCII / variable tokens and symbolic placeholders are preserved by a
    # simple mixed segmentation pass.
    parts = re.findall(r"[A-Za-z]+\d*|[A-Z]?\$|[XYZW]\s*[单天点]?|\d+|[\u4e00-\u9fff]{1,4}", str(value or ""), flags=re.I)
    out: list[str] = []
    for part in parts:
        c = _compact(part)
        if not c or len(c) <= 1:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{4}", c):
            out.extend([c[:2], c[2:]])
        else:
            out.append(c)
    # Also keep frequent compact two-character chunks from longer Chinese
    # phrases.  These chunks come from the schema value itself.
    if re.search(r"[\u4e00-\u9fff]", t) and len(t) >= 4:
        for i in range(0, len(t) - 1, 2):
            out.append(t[i:i+2])
    seen: set[str] = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


_CONTRASTIVE_OPERATOR_GROUPS = (
    ("前", "后"),
    ("上", "下"),
    ("高", "低"),
    ("多", "少"),
    ("早", "晚"),
    ("内", "外"),
    ("有", "无"),
    ("可", "不可"),
    ("能", "不能"),
    ("会", "不会"),
    ("已", "未"),
)


def _contrastive_operators_preserved(value: str, text: str) -> bool:
    """Guard loose schema matching against direction/polarity inversions.

    LongCat may generate short refute values such as "A 后 B" while the safe
    support sentence says "A 前 B".  A loose matcher that only counts shared
    fragments would see A+B and accidentally ignore the opposite operator.  The
    operator set below is generic Chinese contrast structure, not a business
    dictionary.  Exact substring matches still pass before this guard is used.
    """
    v = _compact(value)
    t = _compact(text)
    if not v or not t:
        return False
    for group in _CONTRASTIVE_OPERATOR_GROUPS:
        present = [op for op in group if op in v]
        if not present:
            continue
        for op in present:
            # For composite forms such as "不能" and "不可", preserve the full
            # form when it appears in the schema value.  A bare "能" in the
            # dialogue should not satisfy "不能" by loose fragments.
            if len(op) > 1 and op not in t:
                return False
        # If the value explicitly contains one side of a binary operator pair,
        # the loose match must contain that same side; otherwise "前" and "后"
        # or "高" and "低" can be collapsed by shared surrounding words.
        single_ops = [op for op in present if len(op) == 1]
        if single_ops and not all(op in t for op in single_ops):
            return False
    return True


_TEMPORAL_OPERATOR_GROUPS = (
    ("当天", "当日", "今天"),
    ("次日", "第二天", "翌日", "明天"),
    ("立即", "马上", "即刻", "现在就"),
    ("随时",),
)


def _temporal_operators_preserved(value: str, text: str) -> bool:
    """Keep loose phrase matching from erasing temporal polarity.

    The terms are generic time/effect operators.  They are not business labels.
    A long refute phrase such as "same-day handling takes effect same day" must
    not be matched by fragments "handling" + "takes effect" in a safe sentence
    that actually says "before the deadline, next day effective".
    """
    v = _compact(value)
    t = _compact(text)
    if not v or not t:
        return False
    for group in _TEMPORAL_OPERATOR_GROUPS:
        value_hits = [m for m in group if m in v]
        if value_hits and not any(m in t for m in group):
            return False
    # "before/after" can be a single character in Chinese.  Preserve it only
    # when the schema phrase is clearly temporal, otherwise object labels with
    # these characters would become too strict.
    temporal_scope = any(x in v for x in ("生效", "有效", "取消", "处理", "完成", "截止", "时间", "点"))
    if temporal_scope:
        if "之前" in v and not ("之前" in t or "前" in t):
            return False
        if "之后" in v and not ("之后" in t or "后" in t):
            return False
    return True


def _split_strong_clauses(text: str) -> list[str]:
    clauses = [x.strip() for x in re.split(r"[。；;！？!?\n]+", str(text or "")) if x.strip()]
    return clauses or [str(text or "")]


def _schema_phrase_loose_match(value: str, text: str) -> bool:
    v = _compact(value)
    t = _compact(text)
    if not v or not t:
        return False
    neg_markers = ("不", "没", "无", "非", "不用", "不影响", "没关系", "不会", "无需")
    value_has_neg = any(m in v for m in neg_markers)
    text_has_neg = any(m in t for m in neg_markers)
    if value_has_neg and not text_has_neg:
        return False
    if v in t:
        return True
    # Do not let loose fragments erase temporal polarity such as 当天/次日 or
    # 立即/延后.  Exact substring matching above still accepts a literal hit.
    if not _temporal_operators_preserved(value, text):
        return False
    # Very short Chinese schema phrases are usually atomic predicates, not
    # bags of two-character fragments.  For example, a refute phrase shaped
    # like "A生效/取消" must not be satisfied by finding "A" in one part of the
    # sentence and "取消" in another.  Keep loose matching for longer schema
    # phrases where inserted function words are common.
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", v):
        return False
    if not _contrastive_operators_preserved(value, text):
        return False
    parts = [x for x in _split_schema_phrase(value) if len(x) >= 2]
    if value_has_neg:
        parts = [x for x in parts if any(m in x for m in neg_markers) or len(x) >= 3]

    if len(parts) < 2:
        return False
    hits = sum(1 for x in parts if x in t)
    return hits >= min(len(parts), 2) and hits / max(1, len(parts)) >= 0.5


def _schema_gate_match(value: str, text: str) -> bool:
    """Match a schema object/condition gate with safe abbreviation support.

    LongCat sometimes writes a full object label in the graph while natural
    dialogue uses a shortened label.  The abbreviations are derived only from
    the current schema value.  They are used as gates for numeric/direction
    contradiction checks, not as stand-alone business rules.
    """
    if _schema_phrase_loose_match(value, text):
        return True
    t = _compact(text)
    for variant in _safe_anchor_variants(value):
        cv = _compact(variant)
        if len(cv) >= 2 and cv in t:
            return True
    return False


_GENERIC_NEGATION_MARKERS = (
    "不用", "不需要", "无需", "不必", "不看", "不管", "不会", "不能", "无法", "没法", "不适合", "不支持", "不符合", "不影响",
    "没影响", "没有影响", "没关系", "无所谓", "没帮助", "没有帮助", "不帮", "没做到",
    "不用做到", "不要求", "不用完成", "不算", "不是", "非", "随时", "口头", "代办", "代处理",
)
_GENERIC_WRONG_TIME_MARKERS = ("立即", "马上", "当天", "今天", "现在就", "过了", "前也能", "前就", "保存前", "配置前")
_GENERIC_ONLY_MARKERS = ("只要", "只需", "只看", "只用", "就够")
_GENERIC_STRONG_WRONG_MARKERS = (
    "不影响", "没影响", "不会影响", "不会", "没关系", "没帮助", "没有帮助", "不用", "不需要", "无需", "不看", "不管",
    "随时", "立即", "马上", "当天", "口头", "代办", "代处理", "只要", "只需", "只看", "就够",
)


def _negates_schema_value(value: str, text: str) -> bool:
    """Return whether a generic negation appears close to a schema value."""
    v = _compact(value)
    t = _compact(text)
    if not v or not t:
        return False
    # Use compact variants derived from the value itself.
    candidates = [v, *_split_schema_phrase(value)]
    candidates = [c for c in candidates if len(c) >= 2]
    negs = [m for m in _GENERIC_NEGATION_MARKERS if m in t]
    if not negs:
        return False
    for c in candidates:
        pos = t.find(c)
        if pos < 0:
            continue
        for m in negs:
            mpos = t.find(m)
            while mpos >= 0:
                # Negation must be close to the value or form a common
                # "not need/look/complete + value" expression.
                if abs(mpos - pos) <= max(4, len(c) + 2):
                    return True
                mpos = t.find(m, mpos + 1)
    return False



def _looks_like_safe_boundary_statement(text: str) -> bool:
    t = _compact(text)
    if not t:
        return False
    boundary = ("不能保证", "不保证", "无法保证", "不能承诺", "不承诺", "无法承诺")
    reference = ("为准", "以页面", "以系统", "按页面", "按系统", "按配置", "实际展示")
    return any(x in t for x in boundary) and (any(x in t for x in reference) or "为准" in t)


def _looks_like_corrective_support(text: str) -> bool:
    t = _compact(text)
    # Corrective form: "不能/不是 A，要/按/是 B".  This is a safe correction,
    # not an endorsement of A.
    return any(x in t for x in ("不能", "不是", "不可以", "不可", "无法", "不只")) and any(x in t for x in ("要", "按", "是", "来"))


def _has_support_after_correction_pivot(support_values: list[object], text: str) -> bool:
    t = _compact(text)
    if not t:
        return False
    neg_positions = [t.find(m) for m in _GENERIC_NEGATION_MARKERS if t.find(m) >= 0]
    if not neg_positions:
        return False
    first_neg = min(neg_positions)
    pivot = -1
    for p in ("要", "按", "是", "来"):
        start = 0
        while True:
            pos = t.find(p, start)
            if pos < 0:
                break
            # Do not treat the character inside "不是/没是/..." as a positive
            # correction pivot.  We only want the B side that appears after the
            # negated part, e.g. "不是 A，是 B" or "不能 A，要按 B".
            if pos > first_neg and (pos == 0 or t[pos - 1] not in {"不", "没", "无", "非"}):
                pivot = max(pivot, pos)
            start = pos + 1
    if pivot < 0:
        return False
    tail = t[pivot + 1 :]
    for value in support_values:
        cv = _compact(value)
        if cv and cv in tail:
            return True
        for part in _split_schema_phrase(str(value)):
            cp = _compact(part)
            if len(cp) >= 2 and cp in tail:
                return True
    return False








def _is_disjunctive_choice_inquiry(pattern: dict[str, Any], text: str) -> bool:
    """Whether a matched refute pattern is only listing alternatives.

    Some graphs express a wrong combination as all(A)+any(B).  A normal
    clarifying question may mention both A and B as alternatives, e.g.
    "A 还是 B?".  That is not an assertion of the wrong combination.  The
    gate is purely linguistic and pattern-driven: it requires a choice marker
    in the utterance and matched values from the current schema pattern on the
    two sides of that marker.
    """
    t = _compact(text)
    if not t:
        return False
    markers = ("还是", "或者", "或是", "还是说", "哪种", "哪一类", "哪边", "确认一下")
    if not any(m in t for m in markers):
        return False
    values = [str(v or "") for v in _pattern_values([pattern]) if str(v or "").strip()]
    matched = [v for v in values if _schema_phrase_loose_match(v, text)]
    if len(matched) < 2:
        return False
    # Strong assertion markers override the choice guard only when they appear
    # outside a plain question/listing context.  A bare "直接" inside a schema
    # value such as "直接使用" should not matter unless that value itself is hit.
    if "?" in str(text) or "？" in str(text):
        return True
    # Require at least one matched value before and one after a choice marker.
    for marker in ("还是", "或者", "或是", "还是说"):
        pos = t.find(marker)
        if pos < 0:
            continue
        before = t[:pos]
        after = t[pos + len(marker):]
        before_hit = any(_compact(v) and (_compact(v) in before or any(part in before for part in _split_schema_phrase(v) if len(part) >= 2)) for v in matched)
        after_hit = any(_compact(v) and (_compact(v) in after or any(part in after for part in _split_schema_phrase(v) if len(part) >= 2)) for v in matched)
        if before_hit and after_hit:
            return True
    # "确认一下/哪种" style is also a choice question when several schema
    # values are merely enumerated.
    return any(m in t for m in ("确认一下", "哪种", "哪一类", "哪边"))


def _wrong_alias_fragments(alias: object) -> set[str]:
    """Extract generic fragments from schema aliases that explicitly mark a wrong form.

    The alias must itself carry a wrongness marker in its leading label, such as
    ``误说`` or ``错误``.  This keeps ordinary descriptive aliases from becoming
    executable business dictionaries.
    """
    head = str(alias or "").strip().split()[0] if str(alias or "").strip() else ""
    if not head or not any(m in head for m in ("误说", "错误", "违规", "冲突")):
        return set()
    label = head
    for m in ("误说", "错误", "违规", "冲突"):
        label = label.replace(m, "")
    compact = _compact(label)
    out: set[str] = set()
    if re.search(r"[一-鿿]", compact):
        for n in (2, 3):
            for i in range(0, max(0, len(compact) - n + 1)):
                frag = compact[i : i + n]
                if len(frag) == n:
                    out.add(frag)
    for token in re.findall(r"[a-z][a-z0-9_]{2,}", compact):
        out.add(token)
    generic = {"知识", "错误", "违规", "冲突", "流程", "规则", "条件", "节点", "说明", "要求", "口径"}
    return {x for x in out if x and x not in generic}


def _refute_pattern_is_negated(pattern: dict[str, Any], text: str) -> bool:
    """Return True when the utterance is rejecting, not asserting, a refute pattern.

    This is schema-driven: it only inspects the refute pattern's own all/any
    values and its safe ``none`` guards.  It does not know domain-specific
    business terms.
    """
    t = _compact(text)
    for value in list(pattern.get("none") or []):
        cv = _compact(value)
        if cv and cv in t:
            return True
    for value in list(pattern.get("all") or []) + list(pattern.get("any") or []):
        if value and _negates_schema_value(str(value), text):
            return True
    return False


def _refute_fragment_hit(value: str, text: str) -> bool:
    t = _compact(text)
    parts = [x for x in _split_schema_phrase(value) if len(x) >= 2]
    if not parts:
        return False
    # One strong fragment can be enough only for refute phrases.  Safe negation
    # around that fragment means the speaker is rejecting the wrong statement.
    for part in parts:
        if part in t and not _negates_schema_value(part, text):
            return True
    return False



def _refute_non_anchor_fragment_hit(value: str, text: str, anchors: list[str]) -> bool:
    t = _compact(text)
    anchor_text = " ".join(_compact(a) for a in anchors)
    for part in _split_schema_phrase(value):
        cp = _compact(part)
        if len(cp) < 2:
            continue
        if cp in anchor_text or any(cp in a or a in cp for a in anchors):
            continue
        if cp in t and not _negates_schema_value(cp, text):
            return True
    return False


def _schema_before_after_conflict(support_values: list[object], text: str) -> bool:
    """Detect generic prerequisite timing reversal from schema support.

    If the schema support contains an enabling action/value (e.g. save/configure)
    and the utterance says it can take effect before that value, this is a
    contradiction.  The enabling value itself comes from schema support; the
    code only knows generic before/effect operators.
    """
    t = _compact(text)
    if not t:
        return False
    before_effect = any(x in t for x in ("前也能", "前就")) and any(x in t for x in ("生效", "能用", "可用", "使用"))
    if not before_effect:
        return False
    for value in support_values:
        cv = _compact(value)
        if len(cv) < 2:
            continue
        # The prerequisite value has to appear in the utterance; otherwise this
        # could be an unrelated time phrase.
        if cv in t or any(part in t for part in _split_schema_phrase(str(value)) if len(part) >= 2):
            return True
    return False


def _schema_visibility_direct_conflict(claim_values: list[object], support_values: list[object], text: str) -> bool:
    """Detect a generic visible/not-visible branch inversion.

    Generated schemas often express a UI branch as "if visible then direct use;
    if not visible then configure / check later".  If the utterance says "not
    visible but direct use/publish" and the schema contains both the not-visible
    branch and a configure/later branch, this is a contradiction.  These are
    UI-control terms, not task/domain labels.
    """
    t = _compact(text)
    schema_text = _compact(" ".join(str(v or "") for v in [*claim_values, *support_values]))
    has_not_visible_schema = any(x in schema_text for x in ("未显示", "没显示", "没有显示", "看不到"))
    has_repair_schema = any(x in schema_text for x in ("配置", "开通", "明天", "稍后", "再查看"))
    says_not_visible = any(x in t for x in ("未显示", "没显示", "没有显示", "看不到"))
    says_direct = "直接" in t and any(x in t for x in ("发布", "使用", "用", "能用", "可用"))
    says_repair = any(x in t for x in ("配置", "开通", "明天", "稍后", "再查看"))
    return has_not_visible_schema and has_repair_schema and says_not_visible and says_direct and not says_repair

KnowledgeVerdict = Literal["支持", "冲突", "证据不足"]


@dataclass(slots=True)
class KnowledgeCheck:
    """Tri-state local check for one knowledge claim.

    The checker does not infer domain meaning. It only executes patterns supplied
    by the graph-building stage and records whether those patterns support,
    refute, or leave the claim under-specified.
    """

    knowledge_id: str
    node_id: str | None
    name: str
    severity: str
    verdict: KnowledgeVerdict
    evidence: str
    turn_index: int | None
    reason: str
    claim_id: str | None = None
    aliases: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "node_id": self.node_id,
            "name": self.name,
            "severity": self.severity,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "turn_index": self.turn_index,
            "reason": self.reason,
            "claim_id": self.claim_id,
            "aliases": self.aliases or [],
        }


# Compatibility alias: scoring only treats verdict == "冲突" as an error event.
KnowledgeEvent = KnowledgeCheck


class KnowledgeJudge:
    """Schema-driven claim/evidence knowledge checker.

    The judge contains no business lexicon. It emits tri-state checks:
    support / conflict / insufficient. Missing facts that are not claimed by
    the dialogue are left to requirement coverage instead of being guessed here.
    """

    def __init__(self) -> None:
        self.matcher = EvidenceMatcher()

    def judge(self, items: list[KnowledgeItem], units: list[EvidenceUnit]) -> list[KnowledgeCheck]:
        checks: list[KnowledgeCheck] = []
        assistant_units = [u for u in units if u.speaker == "assistant"]
        # Per-dialogue schema context used only to prevent sibling/neighboring
        # knowledge claims from contaminating each other.  It is rebuilt on
        # every call and contains only graph-provided items/claims, not dataset
        # answers or business dictionaries.
        self._active_items = items
        for item in items:
            if item.judge_type in {"claim_evidence", "claim_evidence_conflict"} or item.claims:
                checks.extend(self._claim_evidence(item, assistant_units))
            elif item.judge_type == "pattern_conflict":
                checks.extend(self._pattern_conflicts(item, assistant_units))
            elif item.judge_type == "numeric_range":
                checks.extend(self._numeric_range(item, assistant_units))
            elif item.judge_type == "option_set":
                checks.extend(self._pattern_conflicts(item, assistant_units))
            else:
                checks.extend(self._pattern_conflicts(item, assistant_units))
        return checks


    def _item_aliases(self, item: KnowledgeItem, claim: KnowledgeClaim | None = None) -> list[str]:
        values = [item.id, item.name, *getattr(item, "aliases", [])]
        if claim is not None:
            values.extend([claim.id, claim.name, *getattr(claim, "aliases", [])])
        out: list[str] = []
        seen: set[str] = set()
        for v in values:
            s = str(v or "")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _item_context_terms(self, item: KnowledgeItem, claim: KnowledgeClaim | None = None) -> list[str]:
        """Return schema-derived object/context anchors for one knowledge item.

        These anchors are used to decide whether a sentence is primarily about
        a more specific neighboring claim.  They are extracted from the graph's
        own item/claim labels and aliases only; no domain lexicon is embedded.
        """
        generic = {
            "规则", "条件", "知识", "错误", "流程", "影响", "要求", "说明",
            "口径", "节点", "事实", "声明", "claim", "knowledge",
        }
        values: list[object] = [item.id, item.name, *list(getattr(item, "aliases", []) or [])]
        if claim is not None:
            values.extend([claim.id, claim.name, *list(getattr(claim, "aliases", []) or [])])
        out: list[str] = []
        seen: set[str] = set()
        for raw in values:
            for part in re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z_]{3,}", str(raw or "")):
                cp = _compact(part)
                if not cp or cp in generic or _is_numeric_or_unit_term(cp):
                    continue
                if any(g in cp for g in generic) and len(cp) <= 4:
                    continue
                variant_seeds = [cp]
                for suffix in ("条件", "规则", "知识", "说明", "口径", "节点"):
                    if cp.endswith(suffix) and len(cp) > len(suffix) + 1:
                        variant_seeds.append(cp[: -len(suffix)])
                if re.fullmatch(r"[\u4e00-\u9fff]{4,}", cp):
                    variant_seeds.append(cp[:4])
                for seed in variant_seeds:
                    for v in _safe_anchor_variants(seed):
                        if v and v not in generic and v not in seen:
                            seen.add(v)
                            out.append(v)
        return out

    def _unit_has_more_specific_competing_support(self, current_item: KnowledgeItem, current_claim: KnowledgeClaim, unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        """Suppress a refute hit when the sentence supports a more specific claim.

        LongCat can generate neighboring facts that share a symbolic value or a
        generic attribute.  For example, one generic rule may refute value A,
        while a FAQ/reward/branch-specific rule supports the same value under a
        narrower context.  If the utterance explicitly contains that narrower
        schema context and satisfies its support pattern, the generic claim
        should not turn the sentence into a conflict.

        This is a schema disambiguation guard, not a task-specific exception:
        the competing context and support evidence both come from the active
        graph.
        """
        active_items = getattr(self, "_active_items", []) or []
        text = _compact(unit.text)
        if not text:
            return False
        # Use item-level context terms here, not claim-value terms.  A sentence
        # may contain a value that is supported by a neighboring claim (for
        # example the neighbor's number placeholder), but that alone does not
        # prove the sentence is about the neighboring context.  Suppression is
        # allowed only when the utterance names the neighboring schema item/FAQ
        # context itself.
        current_terms = set(self._item_context_terms(current_item, None))
        for other_item in active_items:
            if other_item.id == current_item.id:
                continue
            other_claims = other_item.claims or [
                KnowledgeClaim(
                    id=other_item.id + ".claim",
                    name=other_item.name,
                    support_patterns=other_item.support_patterns,
                    refute_patterns=other_item.conflict_patterns,
                    severity=other_item.severity,
                )
            ]
            for other_claim in other_claims:
                if other_item.id == current_item.id and other_claim.id == current_claim.id:
                    continue
                if not other_claim.support_patterns:
                    continue
                if not any(self._pattern_loose_match(p, unit, units) for p in other_claim.support_patterns):
                    continue
                other_terms = set(self._item_context_terms(other_item, None))
                distinguishing_terms = [t for t in other_terms if t and t not in current_terms and t in text]
                if not distinguishing_terms:
                    continue
                # Avoid using very generic overlapping terms as the only reason
                # to suppress a conflict.  A short term is acceptable only when
                # it is the exact label supplied by the competing graph item.
                generic_short_context = {"规则", "条件", "知识", "说明", "流程", "要求", "完成", "每天", "连续", "影响", "节点", "误说"}
                if any(len(t) >= 3 or (len(t) >= 2 and t not in generic_short_context) for t in distinguishing_terms):
                    return True
        return False

    def _pattern_conflicts(self, item: KnowledgeItem, units: list[EvidenceUnit]) -> list[KnowledgeCheck]:
        out: list[KnowledgeCheck] = []
        for unit in units:
            if any(self.matcher._match_pattern(pat, unit, units) for pat in item.conflict_patterns):
                out.append(
                    KnowledgeCheck(item.id, item.node_id, item.name, item.severity, "冲突", unit.text, unit.turn_index, "与知识表标准冲突", aliases=self._item_aliases(item))
                )
                break
            if item.support_patterns and any(self.matcher._match_pattern(pat, unit, units) for pat in item.support_patterns):
                out.append(
                    KnowledgeCheck(item.id, item.node_id, item.name, item.severity, "支持", unit.text, unit.turn_index, "对话声明得到知识表支持", aliases=self._item_aliases(item))
                )
                break
        return out

    def _claim_evidence(self, item: KnowledgeItem, units: list[EvidenceUnit]) -> list[KnowledgeCheck]:
        claims = item.claims or [
            KnowledgeClaim(
                id=item.id + ".claim",
                name=item.name,
                support_patterns=item.support_patterns,
                refute_patterns=item.conflict_patterns,
                severity=item.severity,
            )
        ]
        out: list[KnowledgeCheck] = []
        for claim in claims:
            # Refute patterns are explicit contradiction evidence and should not
            # be blocked by claim/topic gates. claim_patterns only identify grey
            # areas or support contexts; they must not hide a clear conflict.
            anchor_terms = self._claim_anchor_terms(claim)
            for unit in units:
                for pat in claim.refute_patterns:
                    if not self._refute_context_allowed(pat, unit, anchor_terms):
                        continue
                    if _is_disjunctive_choice_inquiry(pat, unit.text):
                        continue
                    if _looks_like_corrective_support(unit.text):
                        support_values_for_guard = _pattern_values(claim.support_patterns)
                        # A corrective sentence can safely reject the graph's own
                        # refute phrase and then restate a support-side fragment,
                        # even when the support pattern is not fully matched.  For
                        # example: "不是 A，是 B".  This remains schema-driven:
                        # A comes from the current refute pattern and B from the
                        # current claim's support patterns; no domain term or
                        # sample text is embedded here.
                        safe_correction = (
                            _refute_pattern_is_negated(pat, unit.text)
                            and _has_support_after_correction_pivot(support_values_for_guard, unit.text)
                        )
                        if safe_correction:
                            continue
                        if self._unit_has_claim_support(claim, unit, units) or self._unit_has_claim_partial_support(claim, unit):
                            non_anchor_guard_values = []
                            for value in support_values_for_guard:
                                cv = _compact(value)
                                if cv and any(cv in a or a in cv for a in anchor_terms):
                                    continue
                                non_anchor_guard_values.append(value)
                    if self._refute_pattern_match(pat, unit, units):
                        # If this utterance also satisfies a more specific
                        # neighboring claim from the same schema, let that
                        # specific support own the sentence instead of treating
                        # a shared/refuted value as a generic conflict.
                        if self._unit_has_more_specific_competing_support(item, claim, unit, units):
                            continue
                        out.append(
                            KnowledgeCheck(
                                item.id,
                                item.node_id,
                                item.name,
                                claim.severity or item.severity,
                                "冲突",
                                unit.text,
                                unit.turn_index,
                                claim.reason or "对话中的事实声明被知识表反驳",
                                claim.id,
                                aliases=self._item_aliases(item, claim),
                            )
                        )
                        break
                if any(x.claim_id == claim.id for x in out):
                    break
            if any(x.claim_id == claim.id for x in out):
                continue

            # Schema-derived generic contradiction fallback.  This catches
            # negated forms of support facts without copying dataset answers
            # into the executable schema.
            for unit in units:
                if self._generic_conflict_detected(item, claim, unit):
                    out.append(
                        KnowledgeCheck(
                            item.id,
                            item.node_id,
                            item.name,
                            claim.severity or item.severity,
                            "冲突",
                            unit.text,
                            unit.turn_index,
                            claim.reason or "对话用否定/豁免/立即生效等表达推翻了知识表事实",
                            claim.id,
                            aliases=self._item_aliases(item, claim),
                        )
                    )
                    break
            if any(x.claim_id == claim.id for x in out):
                continue

            relevant_units = units
            if claim.claim_patterns:
                relevant_units = [u for u in units if any(self._pattern_loose_match(p, u, units) for p in claim.claim_patterns)]
                if not relevant_units:
                    continue

            matched = False
            for unit in relevant_units:
                if claim.support_patterns and any(self._pattern_loose_match(p, unit, units) for p in claim.support_patterns):
                    support_values_for_guard = _pattern_values(claim.support_patterns)
                    if any(_negates_schema_value(str(v), unit.text) for v in support_values_for_guard):
                        continue
                    out.append(
                        KnowledgeCheck(
                            item.id,
                            item.node_id,
                            item.name,
                            claim.severity or item.severity,
                            "支持",
                            unit.text,
                            unit.turn_index,
                            claim.reason or "对话中的事实声明得到知识表支持",
                            claim.id,
                            aliases=self._item_aliases(item, claim),
                        )
                    )
                    matched = True
                    break
            if matched:
                continue

            # If the graph explicitly marks a claim as asserted but no
            # support/refute pattern fired, it becomes an arbitration candidate.
            if claim.claim_patterns and relevant_units:
                u = relevant_units[0]
                out.append(
                    KnowledgeCheck(
                        item.id,
                        item.node_id,
                        item.name,
                        claim.severity or item.severity,
                        "证据不足",
                        u.text,
                        u.turn_index,
                        claim.reason or "对话触发了知识声明，但本地 schema 未形成支持或冲突结论",
                        claim.id,
                        aliases=self._item_aliases(item, claim),
                    )
                )
        return out


    def _claim_anchor_terms(self, claim: KnowledgeClaim) -> list[str]:
        candidates: list[str] = []
        generic_attr_markers = {"每天", "至少", "完成", "影响", "之前", "生效", "有助", "帮助", "连续", "要求", "条件"}

        def add_value(value: object, *, allow_short: bool = False) -> None:
            compact = _compact(value)
            if not compact or _is_numeric_or_unit_term(compact):
                return
            if compact in generic_attr_markers:
                return
            if len(compact) >= 3 or allow_short:
                candidates.append(compact)

        # Claim name/id often contains the object label.  Extract Chinese chunks
        # but drop generic attributes.
        for raw in [claim.name, claim.id, *list(getattr(claim, "aliases", []) or [])]:
            for part in re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z_]{3,}", str(raw or "")):
                add_value(part, allow_short=True)

        # Claim patterns are noisier: include only non-generic longer object
        # phrases.  This keeps multi-character object anchors but drops generic attributes.
        for value in _pattern_values(claim.claim_patterns):
            compact = _compact(value)
            if not compact or _is_numeric_or_unit_term(compact):
                continue
            if any(x in compact for x in generic_attr_markers) and len(compact) <= 4:
                continue
            if len(compact) >= 3:
                candidates.append(compact)

        out: list[str] = []
        seen: set[str] = set()
        for c in candidates:
            for v in _safe_anchor_variants(c):
                if v in generic_attr_markers:
                    continue
                if v not in seen:
                    seen.add(v)
                    out.append(v)
        return out

    def _refute_context_allowed(self, pattern: dict[str, Any], unit: EvidenceUnit, anchors: list[str]) -> bool:
        if not anchors:
            return True
        unit_text = _compact(unit.text)

        # If the refute pattern itself carries an explicit object/condition gate
        # and the utterance satisfies that gate, allow it before consulting the
        # broader claim anchor.  This remains schema-driven: the gate terms come
        # only from the graph's own refute pattern.
        all_values = list(pattern.get("all") or [])
        if all_values and all(self._schema_phrase_loose_match(str(v), unit.text) for v in all_values):
            return True

        # Any-only refute patterns are easy to over-apply to sibling facts.
        # Keep the conservative anchor gate here; graph-specific wrong phrases
        # that should fire locally should be expressed with an explicit ``all``
        # object/condition gate in the schema.
        if pattern.get("any") and not pattern.get("all") and not pattern.get("regex_any"):
            meaningful = [a for a in anchors if not (a.startswith("声明") or a.startswith("事实") or a in {"claim", "知识", "规则", "条件"})]
            if not meaningful:
                return True
            return any(a and a in unit_text for a in meaningful)

        # If the refute pattern itself already names the anchor, matching that
        # pattern is sufficient.  Otherwise the candidate utterance must mention
        # the anchored object too.  This prevents sibling facts such as A's
        # correct value from being interpreted as B's conflict.
        pat_text = _compact(" ".join(_pattern_values([pattern])))
        if any(a and a in pat_text for a in anchors):
            return True
        return any(a and a in unit_text for a in anchors)



    def _schema_phrase_loose_match(self, value: str, text: str) -> bool:
        return _schema_phrase_loose_match(value, text)

    def _refute_pattern_match(self, pattern: dict[str, Any], unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        """Match refute patterns with clause-local precision.

        Ordinary evidence groups may span a whole assistant turn, but knowledge
        refutes are assertions.  For patterns shaped as all(object)+any(wrong
        attribute), require the object and wrong attribute to co-occur in the
        same strong clause.  This prevents a safe contrastive sentence like
        "B fits X; A fits Y" from being read as "A fits X" just because the
        whole turn contains A and X.
        """
        all_values = list(pattern.get("all") or [])
        any_values = list(pattern.get("any") or [])
        if all_values and any_values and not pattern.get("regex_any"):
            for clause in _split_strong_clauses(unit.text):
                scoped_unit = EvidenceUnit(
                    turn_index=unit.turn_index,
                    speaker=unit.speaker,
                    text=clause,
                    normalized=clause,
                    kinds=set(unit.kinds),
                    polarity=unit.polarity,
                    numbers=list(unit.numbers),
                    markers=dict(unit.markers),
                )
                if self._pattern_loose_match(pattern, scoped_unit, [scoped_unit]):
                    return True
            return False
        return self._pattern_loose_match(pattern, unit, units)


    def _pattern_loose_match(self, pattern: dict[str, Any], unit: EvidenceUnit, units: list[EvidenceUnit]) -> bool:
        """Exact first, then schema-derived loose phrase matching.

        This remains schema-driven: all values come from the graph.  It mainly
        handles punctuation/inserted-word variants, not hidden answer strings.
        """
        if self.matcher._match_pattern(pattern, unit, units):
            return True
        speaker = pattern.get("speaker")
        if speaker and unit.speaker != speaker:
            return False
        text = str(unit.text or "")
        none_values = [str(x or "").lower() for x in list(pattern.get("none") or [])]
        text_lc = text.lower()
        if none_values and any(n and n in text_lc for n in none_values):
            return False
        all_values = list(pattern.get("all") or [])
        any_values = list(pattern.get("any") or [])
        if all_values and not all(self._schema_phrase_loose_match(str(v), text) for v in all_values):
            return False
        if any_values:
            min_any_hits = int(pattern.get("min_any_hits") or 1)
            hits = sum(1 for v in any_values if self._schema_phrase_loose_match(str(v), text))
            if hits < min_any_hits:
                return False
        if pattern.get("regex_any"):
            return False
        return bool(all_values or any_values)

    def _unit_has_claim_support(self, claim: KnowledgeClaim, unit: EvidenceUnit, units: list[EvidenceUnit] | None = None) -> bool:
        units = units or [unit]
        return bool(claim.support_patterns and any(self._pattern_loose_match(p, unit, units) for p in claim.support_patterns))

    def _unit_has_claim_partial_support(self, claim: KnowledgeClaim, unit: EvidenceUnit) -> bool:
        values = _pattern_values(claim.support_patterns)
        return any(v and self._schema_phrase_loose_match(str(v), unit.text) for v in values)


    def _wrong_alias_conflict(self, item: KnowledgeItem, claim: KnowledgeClaim, unit: EvidenceUnit, support_values: list[object], anchors: list[str]) -> bool:
        text = _compact(unit.text)
        if not text:
            return False
        if _looks_like_corrective_support(unit.text) and _has_support_after_correction_pivot(support_values, unit.text):
            return False
        has_alternative_marker = (("比" in text and "更" in text) or any(x in text for x in ("替代", "代替")))
        if not has_alternative_marker:
            return False
        support_text = _compact(" ".join(str(v or "") for v in support_values))
        anchor_text = _compact(" ".join(str(a or "") for a in anchors))
        aliases = list(getattr(item, "aliases", []) or []) + list(getattr(claim, "aliases", []) or [])
        for alias in aliases:
            fragments = _wrong_alias_fragments(alias)
            if not fragments:
                continue
            hits = [f for f in fragments if f in text]
            if len(hits) < 2:
                continue
            specific_hits = [f for f in hits if f not in support_text and f not in anchor_text]
            if specific_hits:
                return True
        return False


    def _marked_wrong_alias_conflict(self, item: KnowledgeItem, claim: KnowledgeClaim, unit: EvidenceUnit, support_values: list[object], anchors: list[str]) -> bool:
        """Use schema error-label aliases as a weak conflict signal.

        LongCat/binding hints may provide category labels such as "A口径误说".
        These are not sample answer spans, but schema-side error types.  When an
        utterance is anchored to the claim object and hits multiple fragments
        from such a wrongness-marked alias, treat it as a local conflict.  This
        avoids hard-coding domain words while still using the graph's own error
        taxonomy.
        """
        text = _compact(unit.text)
        if not text:
            return False
        anchor_text = _compact(" ".join(str(a or "") for a in anchors))
        support_text = _compact(" ".join(str(v or "") for v in support_values))
        aliases = list(getattr(item, "aliases", []) or []) + list(getattr(claim, "aliases", []) or [])
        for alias in aliases:
            fragments = _wrong_alias_fragments(alias)
            if not fragments:
                continue
            hits = [f for f in fragments if f in text]
            if len(hits) < 2:
                continue
            non_anchor_hits = [f for f in hits if f not in anchor_text and f not in support_text]
            if non_anchor_hits and any(a and a in text for a in anchors):
                return True
        return False



    def _schema_qualitative_direction_conflict(self, claim: KnowledgeClaim, unit: EvidenceUnit, anchors: list[str]) -> bool:
        """Detect object-anchored qualitative direction contradictions.

        This fills a common LongCat schema gap: the schema may say object A is
        "lower/cheaper" and object B is "higher/more expensive", but omit every
        natural-language refute variant.  When the utterance is anchored to the
        same support pattern's object gate and asserts the opposite comparative
        direction, it is a local knowledge conflict.

        No product/domain words are embedded here; object gates come from the
        support pattern ``all`` values or claim anchors, and direction words are
        generic comparative adjectives.
        """
        text_dirs = _direction_polarities(unit.text)
        if not text_dirs:
            return False
        unit_text = _compact(unit.text)
        anchor_hit = any(a and a in unit_text for a in anchors)
        for pat in claim.support_patterns or []:
            values = _pattern_values([pat])
            support_dirs = _direction_polarities(" ".join(str(v or "") for v in values))
            if not support_dirs:
                continue
            all_values = list(pat.get("all") or [])
            non_numeric_gates = [v for v in all_values if not _numeric_ranges_from_text(v) and not _is_numeric_or_unit_term(str(v))]
            if non_numeric_gates:
                if not all(_schema_gate_match(str(v), unit.text) for v in non_numeric_gates):
                    continue
            elif not anchor_hit:
                continue
            # If the same sentence also explicitly states the supported
            # direction for the same object, keep it out of hard conflict.
            if not _opposite_direction(support_dirs, text_dirs):
                continue
            if support_dirs & text_dirs:
                return False
            return True
        return False

    def _schema_numeric_range_conflict(self, claim: KnowledgeClaim, unit: EvidenceUnit, anchors: list[str]) -> bool:
        """Detect object-anchored numeric range contradictions from schema support.

        A common schema gap is that LongCat writes a correct support range
        (object + 5-10) but omits a complete refute list for other ranges.  When
        the utterance is anchored to the same object and asserts a range that is
        disjoint from every supported range, this is a local knowledge conflict.

        The mechanism is schema-driven: object gates come from support pattern
        ``all`` values or claim anchors, and numbers come from support pattern
        values.  It does not contain business terms such as product names or
        dialogue examples.
        """
        text_ranges = _numeric_ranges_from_text(unit.text)
        if not text_ranges:
            return False
        unit_text = _compact(unit.text)
        anchor_hit = any(a and a in unit_text for a in anchors)
        for pat in claim.support_patterns or []:
            support_ranges = _numeric_ranges_from_text(" ".join(str(v or "") for v in _pattern_values([pat])))
            if not support_ranges:
                continue
            all_values = list(pat.get("all") or [])
            non_numeric_gates = [v for v in all_values if not _numeric_ranges_from_text(v) and not _is_numeric_or_unit_term(str(v))]
            if non_numeric_gates:
                if not all(_schema_gate_match(str(v), unit.text) for v in non_numeric_gates):
                    continue
            elif not anchor_hit:
                continue
            # If the same sentence also contains a supported range for this
            # object, treat it as support/ambiguous rather than conflict.
            if any(_range_overlaps(tr, sr) for tr in text_ranges for sr in support_ranges):
                return False
            # Otherwise, a disjoint range tied to the same object contradicts
            # the claim.
            if all(not _range_overlaps(tr, sr) for tr in text_ranges for sr in support_ranges):
                return True
        return False

    def _generic_conflict_detected(self, item: KnowledgeItem, claim: KnowledgeClaim, unit: EvidenceUnit) -> bool:
        """Detect common contradiction forms with schema anchors.

        The rule is intentionally generic: it needs a claim/object anchor from
        the schema and a generic contradiction operator in the utterance.  It
        does not contain task-specific business values.
        """
        text = _compact(unit.text)
        if not text:
            return False
        if _looks_like_safe_boundary_statement(unit.text):
            return False
        anchors = self._claim_anchor_terms(claim)
        item_name_text = " ".join([item.id, item.name])
        for part in re.findall(r"[一-鿿]{2,6}|[A-Za-z_]{3,}", item_name_text):
            cp = _compact(part)
            if cp and cp not in {"规则", "条件", "知识", "错误", "流程", "影响"}:
                for v in _safe_anchor_variants(cp):
                    if v not in anchors and v not in {"规则", "条件", "知识", "错误", "流程", "影响"}:
                        anchors.append(v)
        claim_values = _pattern_values(claim.claim_patterns)
        support_values = _pattern_values(claim.support_patterns)
        refute_values = _pattern_values(claim.refute_patterns)
        schema_values = claim_values + support_values + refute_values + [claim.name, claim.id]
        support_values_for_guard = _pattern_values(claim.support_patterns)
        if _looks_like_corrective_support(unit.text):
            # Corrective forms such as "not A, use B" should not be converted
            # into a conflict merely because A is listed in refute_patterns.  B
            # is still checked only against the current schema's support side.
            has_supporting_correction_tail = _has_support_after_correction_pivot(support_values_for_guard, unit.text)
            safe_refute_correction = any(
                _refute_pattern_is_negated(pat, unit.text)
                for pat in claim.refute_patterns
            ) and has_supporting_correction_tail
            if safe_refute_correction or (has_supporting_correction_tail and not claim.refute_patterns):
                return False

            if self._unit_has_claim_support(claim, unit, [unit]) or self._unit_has_claim_partial_support(claim, unit):
                # Do not suppress when the correction explicitly negates a required
                # non-anchor value.  Negating a wrong predicate after the object
                # anchor, then restating the support value, is a safe correction.
                anchor_text = " ".join(anchors)
                def _is_anchor_value(value: object) -> bool:
                    cv = _compact(value)
                    return bool(cv and any(cv in a or a in cv for a in anchors))
                non_anchor_guard_values = [v for v in support_values_for_guard if not _is_anchor_value(v)]
                if not any(_negates_schema_value(str(v), unit.text) for v in non_anchor_guard_values):
                    return False

        # Need a schema anchor for ordinary contradictions.  The only exception
        # is a temporal claim that is explicitly contradicted by immediate/after-
        # cutoff wording; this is handled below.
        anchor_hit = any(a and a in text for a in anchors)
        loose_schema_hits = [v for v in schema_values if v and self._schema_phrase_loose_match(str(v), unit.text)]

        # If the utterance directly satisfies a support pattern and does not
        # negate that support value, generic contradiction operators such as
        # “即可/只需” should not turn the correct support sentence into a
        # conflict.  The concrete support terms still come only from schema.
        if self._unit_has_claim_support(claim, unit, [unit]):
            support_values_for_guard = _pattern_values(claim.support_patterns)
            if not any(_negates_schema_value(str(v), unit.text) for v in support_values_for_guard):
                return False

        if self._schema_numeric_range_conflict(claim, unit, anchors):
            return True
        if self._schema_qualitative_direction_conflict(claim, unit, anchors):
            return True

        if anchor_hit and self._wrong_alias_conflict(item, claim, unit, support_values, anchors):
            return True
        has_neg = any(m in text for m in _GENERIC_NEGATION_MARKERS)
        has_only = any(m in text for m in _GENERIC_ONLY_MARKERS)
        safe_only = any(safe in text for safe in ("不只", "不是只", "并不只"))
        has_wrong_time = any(m in text for m in _GENERIC_WRONG_TIME_MARKERS)
        temporal_support = any(any(x in _compact(v) for x in ("次日", "第二天", "之前")) for v in support_values)

        # Explicit refute values may be separated by function words, but must be
        # anchored to the claim object.  Without this, an attribute phrase in one
        # sibling fact can contaminate another sibling fact.
        # Explicit refute patterns are checked above with full pattern
        # semantics.  Do not let individual fragments from those patterns fire
        # here; otherwise a safe corrective sentence such as “要在 App 里取消”
        # can be confused with a wrong pattern from a neighboring claim.
        support_value_negated = any(_negates_schema_value(str(v), unit.text) for v in support_values)
        claim_text_for_scope = _compact(" ".join([claim.id, claim.name, item.name, *claim_values, *support_values]))
        consequence_markers = ("影响", "帮助", "有助", "结果", "生效", "有效", "条件", "要求")
        if support_value_negated and any(x in claim_text_for_scope for x in consequence_markers):
            # A negated operation phrase may share an object word with a
            # consequence claim.  Example shape: "cannot do X" should not refute
            # a separate claim "X affects Y" unless the utterance also talks
            # about the consequence/effect dimension.  These markers are generic
            # consequence operators, not task-specific objects.
            if not any(x in text for x in consequence_markers):
                return False
        if _schema_before_after_conflict(support_values, unit.text):
            return True
        placeholder_mismatch = anchor_hit and (
            _has_symbolic_unit_mismatch(support_values, unit.text)
            or _has_alternative_placeholder_without_support(support_values, unit.text)
        )
        if placeholder_mismatch:
            # A different anonymized value with the same unit is not, by
            # itself, a contradiction.  LongCat graphs may contain neighboring
            # facts such as one day-count for a base rule and another day-count
            # for an extra reward.  Treat placeholder mismatch as a hard local
            # conflict only when the utterance also has a contradiction operator
            # (negation/only/immediate/after-cutoff) or when an explicit refute
            # pattern has already fired above.  Otherwise leave it to support/
            # insufficient evidence instead of killing positive samples.
            if has_neg or (has_only and not safe_only) or (temporal_support and has_wrong_time):
                return True
        if support_value_negated:
            # A negated support value is only a hard conflict when the utterance
            # is anchored to this claim's object.  Otherwise sibling facts can
            # contaminate each other: a correct correction about object A may
            # negate a generic attribute also used by object B.  Claims with no
            # meaningful anchor keep the legacy behavior because there is no
            # safer object gate available.
            meaningful_anchors = [a for a in anchors if not (a.startswith("声明") or a.startswith("事实") or a in {"claim", "知识", "规则", "条件"})]
            if anchor_hit or not meaningful_anchors:
                return True
            strong_negated_consequence = any(x in text for x in ("不影响", "没影响", "不会影响", "没关系", "不算", "不需要", "不用", "无需"))
            if strong_negated_consequence and not _looks_like_corrective_support(unit.text):
                return True

        if safe_only and not support_value_negated:
            # "not only / not just" is usually a corrective expansion, not a
            # waiver of the schema value.  If the utterance did not actually
            # negate a support value, keep it out of hard conflict.
            return False
        if not (has_neg or has_only or (temporal_support and has_wrong_time)):
            return False

        # Claims with temporal support are contradicted by immediate/after-cutoff
        # markers only when the utterance is actually about the temporal claim.
        # A broad entity anchor can occur in neighboring facts: e.g. a dialogue
        # may say "today the contract is effective" while another FAQ claim says
        # "cancelling takes effect next day". The latter must not be refuted by
        # the former merely because both contain the same entity and an effect
        # word. Require either the claim gate itself or a non-temporal support
        # value supplied by the schema to appear in the utterance.
        if temporal_support:
            def _temporal_specific_value(value: object) -> bool:
                cv = _compact(value)
                if not cv or _is_numeric_or_unit_term(cv):
                    return False
                temporal_markers = (
                    "当天", "当日", "今天", "次日", "第二天", "翌日", "明天",
                    "立即", "马上", "即刻", "现在就", "随时", "生效", "有效",
                    "之前", "之后", "前一天", "后一天", "时间", "截止",
                )
                return not any(m in cv for m in temporal_markers)

            temporal_claim_gate_hit = bool(
                claim.claim_patterns
                and any(self._pattern_loose_match(p, unit, [unit]) for p in claim.claim_patterns)
            )
            temporal_support_topic_hit = any(
                _temporal_specific_value(v) and self._schema_phrase_loose_match(str(v), unit.text)
                for v in support_values
            )
            temporal_context_hit = temporal_claim_gate_hit or temporal_support_topic_hit
            if anchor_hit and temporal_context_hit and (has_wrong_time or "随时" in text or "过了" in text):
                return True
            if temporal_context_hit and any(x in text for x in ("立即", "马上", "过了")):
                return True
        if not anchor_hit:
            return False

        claim_text = _compact(" ".join([claim.id, claim.name, item.name, *claim_values, *support_values]))
        if "影响" in claim_text and not any(x in text for x in ("影响", "帮助", "有助", "没关系", "不影响", "不会影响", "没影响", "没帮助", "没有帮助")):
            return False

        placeholder_mismatch = anchor_hit and (
            _has_symbolic_unit_mismatch(support_values, unit.text)
            or _has_alternative_placeholder_without_support(support_values, unit.text)
        )
        if placeholder_mismatch and (has_neg or (has_only and not safe_only) or (temporal_support and has_wrong_time)):
            return True

        # Contradict a support claim when the sentence mentions a required value
        # but negates/waives it.
        if support_values:
            value_hits = [v for v in support_values if not _is_numeric_or_unit_term(str(v)) and self._schema_phrase_loose_match(str(v), unit.text)]
            def is_anchor_value(v: object) -> bool:
                cv = _compact(v)
                return any(cv and (cv in a or a in cv) for a in anchors)
            non_anchor_hits = [v for v in value_hits if not is_anchor_value(v)]
            negated_value_hits = [v for v in non_anchor_hits if _negates_schema_value(str(v), unit.text)]
            if negated_value_hits:
                return True
            # Some claims use the object itself as the required value, e.g.
            # "App" as the required exit entry.  Negating that anchored value
            # ("不用进App") is a conflict, but a corrective sentence
            # ("不能代取消，要按App") is suppressed above.
            if value_hits and any(_negates_schema_value(str(v), unit.text) for v in value_hits):
                return True
            # "only/just" forms often contradict quota/condition claims even
            # when the omitted expected value is not adjacent to the marker.
            if non_anchor_hits and has_only and not safe_only:
                return True
            if anchor_hit and has_only and not safe_only:
                return True

        # If the claim itself is about impact/help/qualification, generic
        # "no impact/no help" utterances are contradictions once anchored.
        claim_text = _compact(" ".join([claim.id, claim.name, item.name, *claim_values, *support_values]))
        if any(x in claim_text for x in ("影响", "帮助", "有助", "条件", "要求")):
            if any(x in text for x in _GENERIC_STRONG_WRONG_MARKERS):
                return True
        return False

    def _numeric_range(self, item: KnowledgeItem, units: list[EvidenceUnit]) -> list[KnowledgeCheck]:
        expected = item.expected
        target_patterns = expected.get("target_patterns") or []
        min_v = expected.get("min")
        max_v = expected.get("max")
        out: list[KnowledgeCheck] = []
        for unit in units:
            if target_patterns and not any(str(p).lower() in str(unit.text or "").lower() for p in target_patterns):
                continue
            if not unit.numbers:
                out.append(KnowledgeCheck(item.id, item.node_id, item.name, item.severity, "证据不足", unit.text, unit.turn_index, "命中知识目标，但没有可比较的数值证据", aliases=self._item_aliases(item)))
                continue
            conflict = False
            support = False
            for num in unit.numbers:
                values = [num.get("value")] if num.get("type") == "number" else [num.get("start"), num.get("end")]
                values = [v for v in values if v is not None]
                if not values:
                    continue
                ok = True
                for v in values:
                    if (min_v is not None and float(v) < float(min_v)) or (max_v is not None and float(v) > float(max_v)):
                        ok = False
                if ok:
                    support = True
                else:
                    conflict = True
            if conflict:
                out.append(KnowledgeCheck(item.id, item.node_id, item.name, item.severity, "冲突", unit.text, unit.turn_index, "数值超出知识表允许范围", aliases=self._item_aliases(item)))
            elif support:
                out.append(KnowledgeCheck(item.id, item.node_id, item.name, item.severity, "支持", unit.text, unit.turn_index, "数值落在知识表允许范围内", aliases=self._item_aliases(item)))
        return out

# ---- Runtime extension helpers are attached below without domain-specific rules. ----
