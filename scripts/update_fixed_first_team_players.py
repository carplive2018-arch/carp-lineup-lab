from __future__ import annotations

import html
import re
import urllib.request
from pathlib import Path

MEMBERS_URL = "https://www.carp.co.jp/team/members/"
PUBLIC_PY_PATH = Path("app/api/public.py")
print("SCRIPT_MARKER_2026_05_12_A")

NAME_ALIASES = {
    "E.モンテロ": "モンテロ",
    "S.ファビアン": "ファビアン",
}

POSITION_SECTIONS = {"捕手", "内野手", "外野手"}
SECTION_STOPS = {
    "監督・コーチ",
    "投手",
    "二軍メンバー",
    "三軍メンバー",
    "選手",
    "コーチ",
}


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _clean_text(value: str) -> str:
    value = value.replace("\u3000", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\r\n*-•")


def _html_to_lines(source: str) -> list[str]:
    text = html.unescape(source)

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "\n", text)
    text = re.sub(
        r"(?i)</?(br|p|div|li|ul|ol|h1|h2|h3|h4|h5|h6|section|article|main|header|footer|tr|td|th)>",
        "\n",
        text,
    )
    text = re.sub(r"(?i)<[^>]+>", "", text)

    lines: list[str] = []
    for raw in text.splitlines():
        line = _clean_text(raw)
        if line:
            lines.append(line)
    return lines


def _is_uniform_number_line(line: str) -> bool:
    return bool(re.fullmatch(r"-|\d+", line))


def _extract_first_team_position_players(lines: list[str]) -> list[str]:
    try:
        start_index = lines.index("一軍メンバー")
    except ValueError:
        return []

    end_index = len(lines)
    for i in range(start_index + 1, len(lines)):
        if lines[i] in {"二軍メンバー", "三軍メンバー"}:
            end_index = i
            break

    names: list[str] = []
    current_section: str | None = None

    for line in lines[start_index + 1:end_index]:
        if line in POSITION_SECTIONS:
            current_section = line
            continue

        if line in SECTION_STOPS:
            current_section = None
            continue

        if current_section not in POSITION_SECTIONS:
            continue

        if _is_uniform_number_line(line):
            continue

        name = NAME_ALIASES.get(line, line)
        if name not in names:
            names.append(name)

    return names


def _extract_player_profile_names(public_py_text: str) -> set[str]:
    return set(
        re.findall(
            r'^\s*"([^"]+)"\s*:\s*\{"eligible_positions"\s*:',
            public_py_text,
            flags=re.MULTILINE,
        )
    )


def _replace_last_fixed_block(public_py_text: str, names: list[str]) -> str:
    marker = "LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS"
    marker_index = public_py_text.find(marker)
    if marker_index == -1:
        raise RuntimeError("LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS が app/api/public.py に見つかりませんでした。")

    brace_start = public_py_text.find("{", marker_index)
    if brace_start == -1:
        raise RuntimeError("LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS の開始 { が見つかりませんでした。")

    depth = 0
    brace_end = None
    for i in range(brace_start, len(public_py_text)):
        ch = public_py_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                brace_end = i
                break

    if brace_end is None:
        raise RuntimeError("LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS の終了 } が見つかりませんでした。")

    new_block_lines = ["LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS = {"]
    new_block_lines.extend(f'    "{name}",' for name in names)
    new_block_lines.append("}")
    new_block = "\n".join(new_block_lines)

    return public_py_text[:marker_index] + new_block + public_py_text[brace_end + 1:]


def main() -> None:
    public_py_text = PUBLIC_PY_PATH.read_text(encoding="utf-8")
    profile_names = _extract_player_profile_names(public_py_text)

    source = _fetch_html(MEMBERS_URL)
    lines = _html_to_lines(source)
    names = _extract_first_team_position_players(lines)

    if profile_names:
        names = [name for name in names if name in profile_names]

    if not names:
        print("WARNING: 一軍メンバーの野手名候補を取得できなかったため、既存の LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS をそのまま使います。")
        return

    updated_text = _replace_last_fixed_block(public_py_text, names)

    if updated_text == public_py_text:
        print("No change: LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS は更新不要でした。")
        return

    PUBLIC_PY_PATH.write_text(updated_text, encoding="utf-8")
    print(f"Updated LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS: {len(names)} players")


if __name__ == "__main__":
    main()
