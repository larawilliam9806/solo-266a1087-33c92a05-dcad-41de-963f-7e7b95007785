"""Form/answer parsing and the item-analysis engine.

The engine is deliberately pure: it takes the current forms, answer rows,
human review decisions and the optional roster/distribution tables, and
returns a complete, deterministic snapshot.  The caller persists that
snapshot in one transaction together with the change that triggered it, so
forms, mapping corrections, review decisions, reconciliation data and
statistics always become visible at the same moment.
"""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict


class ValidationError(ValueError):
    """Raised for bad CSV input or an invalid form/mapping edit."""


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

FORM_HEADERS = {
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question", "题号\n"},
    "letter": {"选项字母", "字母", "涂选字母", "option", "letter", "选项"},
    "std_code": {"标准题码", "题码", "标准题号", "std_code", "code", "standard_code"},
    "std_option": {"标准选项", "标准答案选项", "std_option", "standard_option"},
}
ANSWER_HEADERS = {
    "card_no": {"卡号", "答题卡号", "卡编号", "card_no", "card", "card_id"},
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question"},
    "letter": {"涂选字母", "选项字母", "字母", "letter", "option", "作答"},
    "scanned_code": {"实扫题码", "扫描题码", "实扫题号", "scanned_code", "scan_code", "scanned"},
}
ROSTER_HEADERS = {
    "card_no": {"卡号", "答题卡号", "卡编号", "card_no", "card", "card_id"},
    "room": {"考场", "考场号", "试室", "试室号", "room", "exam_room", "room_no"},
}
DIST_HEADERS = {
    "room": {"考场", "考场号", "试室", "试室号", "room", "exam_room", "room_no"},
    "form_name": {"已登记卷式", "卷式", "卷式名称", "卷型", "form", "form_name", "form_type"},
    "copies": {"份数", "数量", "份", "copies", "count", "num"},
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("无法识别文件编码，请使用 UTF-8 或 GBK 编码的 CSV")


def _norm(value) -> str:
    return (value or "").strip()


def _read_rows(raw: bytes, spec: dict, kind: str):
    """Return (data_rows, text) so callers can persist the decoded CSV."""
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [[_norm(c) for c in r] for r in reader if any(_norm(c) for c in r)]
    if not rows:
        raise ValidationError(f"{kind}为空")

    def locate(header):
        found = {}
        for idx, cell in enumerate(header):
            key = cell.lower().replace(" ", "")
            if not key:
                continue
            for field, aliases in spec.items():
                if key in {a.lower() for a in aliases} and field not in found:
                    found[field] = idx
        return found

    positions = locate(rows[0])
    if positions:
        missing = [f for f in spec if f not in positions]
        if missing:
            raise ValidationError(f"{kind}缺少列：{', '.join(missing)}")
        data_rows = rows[1:]
    else:
        # Headerless file: accept the canonical column order.
        positions = {field: i for i, field in enumerate(spec)}
        data_rows = rows

    out = []
    for r in data_rows:
        out.append(tuple(r[positions[f]] if positions[f] < len(r) else "" for f in spec))
    return out, text


def decode_csv(raw: bytes) -> str:
    """Decode an uploaded CSV to text so job payloads stay UTF-8 safe."""
    return _decode(raw)


def validate_form_rows(rows):
    """Validate one form's mapping rows. Returns the same rows on success."""
    seen = set()
    q_to_code: dict[str, str] = {}
    code_to_q: dict[str, str] = {}
    code_letters: dict[str, dict[str, str]] = {}
    for q_no, letter, std_code, std_option in rows:
        if not q_no or not letter:
            raise ValidationError(f"卷式行缺少题号或选项字母：{q_no or '?'}/{letter or '?'}")
        if not std_code or not std_option:
            raise ValidationError(f"卷式行缺少标准题码或标准选项：{q_no}/{letter}")
        if (q_no, letter) in seen:
            raise ValidationError(f"卷式内题号 {q_no} 的选项 {letter} 重复")
        seen.add((q_no, letter))
        if q_to_code.setdefault(q_no, std_code) != std_code:
            raise ValidationError(
                f"卷式内题号 {q_no} 映射到多个标准题码（{q_to_code[q_no]} / {std_code}）"
            )
        if code_to_q.setdefault(std_code, q_no) != q_no:
            raise ValidationError(
                f"标准题码 {std_code} 在本卷出现多次（题号 {code_to_q[std_code]} / {q_no}）"
            )
        letters = code_letters.setdefault(std_code, {})
        if std_option in letters:
            raise ValidationError(
                f"标准题码 {std_code} 的标准选项 {std_option} 重复"
            )
        letters[std_option] = letter
    return rows


def parse_form_csv(raw: bytes):
    """Return [(q_no, letter, std_code, std_option), ...] validated per form."""
    rows, _ = _read_rows(raw, FORM_HEADERS, "卷式 CSV")
    if not rows:
        raise ValidationError("卷式 CSV 没有数据行")
    return validate_form_rows(rows)


def parse_answer_csv(raw: bytes):
    """Return [{card_no, q_no, letter, scanned_code}, ...]."""
    rows, _ = _read_rows(raw, ANSWER_HEADERS, "答题 CSV")
    if not rows:
        raise ValidationError("答题 CSV 没有数据行")
    seen = set()
    out = []
    for card_no, q_no, letter, scanned in rows:
        if not card_no or not q_no:
            raise ValidationError("答题行缺少卡号或题号")
        if (card_no, q_no) in seen:
            raise ValidationError(f"卡号 {card_no} 的题号 {q_no} 出现多行")
        seen.add((card_no, q_no))
        out.append(
            {"card_no": card_no, "q_no": q_no, "letter": letter, "scanned_code": scanned}
        )
    return out


def parse_roster_csv(raw: bytes):
    """清册 CSV -> [(card_no, room), ...]. 每张卡只能登记一次。"""
    rows, _ = _read_rows(raw, ROSTER_HEADERS, "清册 CSV")
    if not rows:
        raise ValidationError("清册 CSV 没有数据行")
    seen: dict[str, str] = {}
    out = []
    for card_no, room in rows:
        if not card_no or not room:
            raise ValidationError("清册行缺少卡号或考场")
        if card_no in seen:
            raise ValidationError(
                f"卡号 {card_no} 在清册中登记多次（{seen[card_no]} / {room}）"
            )
        seen[card_no] = room
        out.append((card_no, room))
    return out


def parse_dist_csv(raw: bytes):
    """发卷 CSV -> [(room, form_name, copies), ...]. 份数为正整数，同场同卷式一行。"""
    rows, _ = _read_rows(raw, DIST_HEADERS, "发卷 CSV")
    if not rows:
        raise ValidationError("发卷 CSV 没有数据行")
    seen = set()
    out = []
    for room, form_name, copies in rows:
        if not room or not form_name:
            raise ValidationError("发卷行缺少考场或已登记卷式")
        if not re.fullmatch(r"\d+", copies) or int(copies) < 1:
            raise ValidationError(
                f"考场 {room} 卷式「{form_name}」的份数须为正整数：{copies or '空'}"
            )
        if (room, form_name) in seen:
            raise ValidationError(f"考场 {room} 的卷式「{form_name}」重复登记，请合并为一行")
        seen.add((room, form_name))
        out.append((room, form_name, int(copies)))
    return out


def validate_recon(roster_rows, dist_rows):
    """Cross-check the two tables: 各场发卷总数须等于清册卡数。"""
    roster_count: dict[str, int] = {}
    for _, room in roster_rows:
        roster_count[room] = roster_count.get(room, 0) + 1
    dist_sum: dict[str, int] = {}
    for room, _, copies in dist_rows:
        dist_sum[room] = dist_sum.get(room, 0) + copies
    for room in sorted(set(roster_count) | set(dist_sum)):
        r = roster_count.get(room, 0)
        d = dist_sum.get(room, 0)
        if r != d:
            raise ValidationError(
                f"考场 {room} 发卷总数 {d} 与清册卡数 {r} 不一致"
            )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _natural_key(text: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


def build_forms(form_rows) -> "OrderedDict[int, dict]":
    """form_rows: iterable of (form_id, form_name, q_no, letter, std_code, std_option)."""
    forms: "OrderedDict[int, dict]" = OrderedDict()
    for form_id, form_name, q_no, letter, std_code, std_option in form_rows:
        form = forms.setdefault(
            form_id,
            {"id": form_id, "name": form_name, "qmap": {}, "codes": {},
             "options": OrderedDict()},
        )
        entry = form["qmap"].setdefault(
            q_no, {"std_code": std_code, "letters": {}, "rev": {}}
        )
        entry["letters"][letter] = std_option
        entry["rev"][std_option] = letter
        form["codes"][std_code] = q_no
        form["options"].setdefault(std_code, set()).add(std_option)
    return forms


def form_is_compatible(form: dict, card_rows: dict) -> bool:
    """Full-card question numbers match and every non-empty code/letter fits."""
    if set(form["qmap"].keys()) != set(card_rows.keys()):
        return False
    for q_no, row in card_rows.items():
        mapping = form["qmap"][q_no]
        if row["scanned_code"] and mapping["std_code"] != row["scanned_code"]:
            return False
        if row["letter"] and row["letter"] not in mapping["letters"]:
            return False
    return True


class Analysis:
    def __init__(self):
        self.question_codes: list[str] = []
        self.options: dict[str, list[str]] = {}
        self.stats: dict[str, dict] = {}
        self.card_results: list[dict] = []
        self.unmatched_cards: list[dict] = []
        self.flagged: list[dict] = []
        self.cards_total = 0
        self.cards_matched = 0
        self.cards_excluded = 0      # 对账模式：清册外或对不上考场的已扫卡
        self.recon: "ReconResult | None" = None

    def summary(self) -> dict:
        s = {
            "cards_total": self.cards_total,
            "cards_matched": self.cards_matched,
            "cards_unmatched": len(self.unmatched_cards),
            "questions": len(self.question_codes),
            "flagged_cells": len(self.flagged),
        }
        if self.recon is not None:
            s["cards_excluded"] = self.cards_excluded
            s["recon"] = {
                "rooms_ok": sum(1 for r in self.recon.rooms if r["ok"]),
                "rooms_failed": sum(1 for r in self.recon.rooms if not r["ok"]),
                "missing": len(self.recon.missing),
                "off_roster": len(self.recon.off_roster),
            }
        return s


# --------------------------------------------------------------------------
# 发卷对账：已扫卡与实发卷式名额的匹配
# --------------------------------------------------------------------------

class ReconResult:
    def __init__(self):
        self.rooms: list[dict] = []            # 按考场排列的对账结果
        self.rooms_by_name: dict[str, dict] = {}
        self.missing: list[dict] = []          # 缺扫卡 {card_no, room}
        self.off_roster: list[str] = []        # 清册外的已扫卡
        self.compat_by_card: dict[str, list] = {}  # 已扫清册卡 -> 相容的实发卷式名


def _max_assignable(cards, compat, caps) -> int:
    """二分匹配：每张卡占一个相容卷式名额，每种卷式有 caps[form] 个名额。

    返回最多能分入名额的卡数。对账只判断可行性，具体分配不作结论。
    """
    slot_form: list[str] = []
    for name in sorted(caps):
        slot_form.extend([name] * caps[name])
    slots_of_form: dict[str, list[int]] = {}
    for idx, name in enumerate(slot_form):
        slots_of_form.setdefault(name, []).append(idx)
    card_slots = {
        c: [i for name in compat.get(c, ()) for i in slots_of_form.get(name, ())]
        for c in cards
    }
    match: dict[int, str] = {}  # slot -> card

    def augment(card, seen) -> bool:
        for si in card_slots[card]:
            if si in seen:
                continue
            seen.add(si)
            if si not in match or augment(match[si], seen):
                match[si] = card
                return True
        return False

    assigned = 0
    for card in sorted(card_slots):
        if augment(card, set()):
            assigned += 1
    return assigned


def reconcile(forms, card_rows_by_card, roster, dist) -> ReconResult:
    """把一个考场的已扫卡分到该场实发卷式的名额里。

    roster: {card_no: room}; dist: {room: {form_name: copies}}。
    考场通过的条件：每张已扫卡都能分到一个相容的实发卷式，且每种卷式
    分入的卡数不超过实发份数（缺扫卡占用剩余名额，所以名额总数够用）。
    """
    forms_by_name = {f["name"]: f for f in forms.values()}
    res = ReconResult()
    roster_by_room: dict[str, list] = {}
    for card_no, room in roster.items():
        roster_by_room.setdefault(room, []).append(card_no)

    for room in sorted(set(roster_by_room) | set(dist), key=_natural_key):
        cards = sorted(roster_by_room.get(room, []), key=_natural_key)
        caps = dist.get(room, {})
        scanned = [c for c in cards if c in card_rows_by_card]
        missing = [c for c in cards if c not in card_rows_by_card]
        compat = {}
        for c in scanned:
            ok_forms = [
                name for name in caps
                if name in forms_by_name
                and form_is_compatible(forms_by_name[name], card_rows_by_card[c])
            ]
            compat[c] = ok_forms
            res.compat_by_card[c] = ok_forms
        assignable = _max_assignable(scanned, compat, caps)
        unknown = [n for n in caps if n not in forms_by_name]
        ok = assignable == len(scanned)
        notes = []
        if not ok:
            notes.append(f"已扫 {len(scanned)} 卡，最多 {assignable} 卡可分入名额")
        if unknown:
            notes.append("卷式未登记：" + "、".join(unknown))
        info = {
            "room": room, "roster": len(cards), "scanned": len(scanned),
            "assignable": assignable, "ok": ok, "detail": "；".join(notes),
            "forms": dict(caps),
        }
        res.rooms.append(info)
        res.rooms_by_name[room] = info
        res.missing.extend({"card_no": c, "room": room} for c in missing)
    res.off_roster = sorted(
        (c for c in card_rows_by_card if c not in roster), key=_natural_key
    )
    return res


def analyse(forms, card_rows_by_card: dict, decisions: dict,
            roster: dict | None = None, dist: dict | None = None) -> Analysis:
    """Compute the whole analysis snapshot.

    forms: OrderedDict from build_forms
    card_rows_by_card: {card_no: {q_no: {"letter":..., "scanned_code":...}}}
    decisions: {(card_no, std_code): {"resolution":..., "note":...}}
    roster/dist: 清册 {card_no: room} 与发卷 {room: {form_name: copies}}；
    为 None 时按原方式统计（全部已登记卷式参与相容判定）。
    """
    result = Analysis()

    # Standard questions and their option universe are defined by live forms.
    codes = set()
    options: dict[str, set[str]] = {}
    for form in forms.values():
        for code, letters in form["options"].items():
            codes.add(code)
            options.setdefault(code, set()).update(letters)
    result.question_codes = sorted(codes, key=_natural_key)
    result.options = {c: sorted(options[c]) for c in result.question_codes}

    all_forms = list(forms.values())
    forms_by_name = {f["name"]: f for f in all_forms}
    if roster:
        result.recon = reconcile(forms, card_rows_by_card, roster, dist or {})
    for code in result.question_codes:
        result.stats[code] = {
            "counted": 0, "blank": 0, "flagged": 0,
            "options": {o: 0 for o in result.options[code]},
        }

    result.cards_total = len(card_rows_by_card)
    for card_no in sorted(card_rows_by_card):
        rows = card_rows_by_card[card_no]
        if result.recon is not None:
            # 对账模式：清册外的卡、对不上考场的卡都不纳入题项统计；
            # 通过的考场只拿该场实发卷式做相容判定（对账时的名额分配
            # 只验证可行性，不作为单卡卷式结论）。
            room = roster.get(card_no)
            if room is None or not result.recon.rooms_by_name[room]["ok"]:
                result.cards_excluded += 1
                continue
            compatible = [
                forms_by_name[name]
                for name in result.recon.compat_by_card.get(card_no, ())
            ]
        else:
            compatible = [f for f in all_forms if form_is_compatible(f, rows)]
        if not compatible:
            detail = "无可相容卷式（整卡题号不一致或非空题码/涂选字母不吻合）"
            result.unmatched_cards.append({"card_no": card_no, "detail": detail})
            result.card_results.append(
                {"card_no": card_no, "std_code": "", "status": "unmatched",
                 "std_option": None, "detail": detail}
            )
            continue
        result.cards_matched += 1
        form_names = "/".join(f["name"] for f in compatible)

        for code in result.question_codes:
            stat = result.stats[code]
            decision = decisions.get((card_no, code))

            containing = [f for f in compatible if code in f["codes"]]
            if not containing:
                # No compatible form printed this question: skip entirely.
                continue

            answers = []
            for form in containing:
                q_no = form["codes"][code]
                letter = rows[q_no]["letter"]
                if letter:
                    std_option = form["qmap"][q_no]["letters"][letter]
                else:
                    std_option = ""
                answers.append((form["name"], letter, std_option))

            if len(containing) != len(compatible):
                missing = [f["name"] for f in compatible if code not in f["codes"]]
                status, option, detail = (
                    "flagged", None,
                    f"部分相容卷式未出该题：{'/'.join(missing)}",
                )
            else:
                distinct = {a[2] for a in answers}
                if len(distinct) == 1:
                    option = answers[0][2]
                    if option:
                        status, detail = "counted", f"相容卷式：{form_names}"
                    else:
                        status, detail = "blank", f"相容卷式：{form_names}（空答）"
                else:
                    shown = "；".join(
                        f"{name}: {letter or '空'}" for name, letter, _ in answers
                    )
                    status, option, detail = (
                        "flagged", None, f"卷式归一作答分歧：{shown}"
                    )

            if decision and status in ("flagged", "blank"):
                # A human conclusion is only meaningful where the automatic
                # verdict could not be trusted (disagreement / unprinted).
                # Once a correction makes the cell consistent, recalculated
                # statistics win and the stale conclusion stops applying.
                res = decision["resolution"]
                if res == "skip":
                    status, option, detail = "skipped", None, (
                        "人工核对：跳过（不计入）" +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )
                elif res == "blank":
                    status, option, detail = "blank", None, (
                        "人工核对：漏答" +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )
                elif res.startswith("opt:") and res[4:] in stat["options"]:
                    chosen = res[4:]
                    status, option, detail = "counted", chosen, (
                        "人工核对：" + chosen +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )

            if status == "skipped":
                continue
            if status == "counted":
                stat["counted"] += 1
                stat["options"][option] += 1
            elif status == "blank":
                stat["blank"] += 1
            else:
                stat["flagged"] += 1
                result.flagged.append(
                    {"card_no": card_no, "std_code": code, "detail": detail}
                )
            result.card_results.append(
                {"card_no": card_no, "std_code": code, "status": status,
                 "std_option": option, "detail": detail}
            )

    result.flagged.sort(key=lambda x: (_natural_key(x["card_no"]), _natural_key(x["std_code"])))
    return result


# --------------------------------------------------------------------------
# DB loading + persistence
# --------------------------------------------------------------------------

def load_forms(conn):
    rows = conn.execute(
        "SELECT f.id, f.name, m.q_no, m.letter, m.std_code, m.std_option "
        "FROM forms f JOIN form_mappings m ON m.form_id = f.id "
        "ORDER BY f.id, m.q_no, m.letter"
    ).fetchall()
    return build_forms(
        [(r["id"], r["name"], r["q_no"], r["letter"], r["std_code"], r["std_option"])
         for r in rows]
    )


def load_card_rows(conn):
    cards: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT card_no, q_no, letter, scanned_code FROM answer_rows "
        "ORDER BY card_no, q_no"
    ):
        cards.setdefault(r["card_no"], {})[r["q_no"]] = {
            "letter": r["letter"], "scanned_code": r["scanned_code"],
        }
    return cards


def load_decisions(conn):
    return {
        (r["card_no"], r["std_code"]): {
            "resolution": r["resolution"], "note": r["note"],
        }
        for r in conn.execute("SELECT card_no, std_code, resolution, note FROM review_decisions")
    }


def load_roster(conn):
    return {
        r["card_no"]: r["room"]
        for r in conn.execute("SELECT card_no, room FROM roster_rows")
    }


def load_dist(conn):
    dist: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT room, form_name, copies FROM dist_rows ORDER BY room, form_name"
    ):
        dist.setdefault(r["room"], {})[r["form_name"]] = r["copies"]
    return dist


def compute(conn) -> Analysis:
    roster = load_roster(conn)
    # 未提交发卷资料时沿用原统计方式。
    return analyse(
        load_forms(conn), load_card_rows(conn), load_decisions(conn),
        roster=roster or None, dist=load_dist(conn) if roster else None,
    )


def persist_run(conn, analysis: Analysis, created_at: str) -> int:
    """Insert a full snapshot and return its run id. Caller owns the transaction."""
    cur = conn.execute(
        "INSERT INTO runs(created_at, summary_json) VALUES (?, '{}')", (created_at,)
    )
    run_id = cur.lastrowid
    import json
    conn.execute(
        "UPDATE runs SET summary_json = ? WHERE id = ?",
        (json.dumps(analysis.summary(), ensure_ascii=False), run_id),
    )
    conn.executemany(
        "INSERT INTO run_question_stats(run_id, std_code, counted, blank, flagged) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, code, st["counted"], st["blank"], st["flagged"])
         for code, st in analysis.stats.items()],
    )
    conn.executemany(
        "INSERT INTO run_option_counts(run_id, std_code, std_option, cnt) VALUES (?, ?, ?, ?)",
        [(run_id, code, opt, cnt)
         for code, st in analysis.stats.items() for opt, cnt in st["options"].items()],
    )
    conn.executemany(
        "INSERT INTO run_card_results(run_id, card_no, std_code, status, std_option, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, r["card_no"], r["std_code"], r["status"], r["std_option"], r["detail"])
         for r in analysis.card_results],
    )
    conn.executemany(
        "INSERT INTO run_unmatched(run_id, card_no, detail) VALUES (?, ?, ?)",
        [(run_id, u["card_no"], u["detail"]) for u in analysis.unmatched_cards],
    )
    if analysis.recon is not None:
        conn.executemany(
            "INSERT INTO run_rooms(run_id, room, ok, roster, scanned, assignable, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, r["room"], 1 if r["ok"] else 0, r["roster"], r["scanned"],
              r["assignable"], r["detail"]) for r in analysis.recon.rooms],
        )
        conn.executemany(
            "INSERT INTO run_missing(run_id, card_no, room) VALUES (?, ?, ?)",
            [(run_id, m["card_no"], m["room"]) for m in analysis.recon.missing],
        )
        conn.executemany(
            "INSERT INTO run_offroster(run_id, card_no) VALUES (?, ?)",
            [(run_id, c) for c in analysis.recon.off_roster],
        )
    return run_id


def prune_runs(conn, keep: int = 4) -> None:
    rows = conn.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM runs WHERE id = ?", (r["id"],))
