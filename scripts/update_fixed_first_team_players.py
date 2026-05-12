from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen


PUBLIC_PY_PATH = Path("app/api/public.py")
MEMBERS_URL = "https://www.carp.co.jp/team/members"


def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")


def clean_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<br\\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("&nbsp;", " ")
    value = value.replace("\\u3000", " ")
    value = re.sub(r"\\s+", " ", value).strip()
    return value


def normalize_name(value: str) -> str:
    return clean_text(value).replace(" ", "").replace("　", "")


def name_aliases(name: str) -> set[str]:
    cleaned = clean_text(name)
    nospace = cleaned.replace(" ", "").replace("　", "")
    aliases = {cleaned, nospace}

    if " " in cleaned:
        parts = [p for p in cleaned.split(" ") if p]
        if parts:
            aliases.add(parts[0])

    if "." in cleaned:
        tail = cleaned.split(".")[-1].strip()
        if tail:
            aliases.add(tail)
            aliases.add(normalize_name(tail))

    return {a for a in aliases if a}


def extract_profile_names(public_py_text: str) -> list[str]:
    names = re.findall(r'"([^"]+)":\\s*\\{"eligible_positions"', public_py_text)
    seen = set()
    ordered = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def extract_first_team_block(html: str) -> str:
    text = clean_text(html)
    normalized = text.replace(" ", "").replace("　", "")

    m = re.search(r"一軍メンバー(.*?)二軍メンバー", normalized)
    if m:
        return m.group(1)

    return normalized


def detect_first_team_position_players(block: str, candidate_names: list[str]) -> list[str]:
    result = []

    for name in candidate_names:
        for alias in name_aliases(name):
            if normalize_name(alias) in block:
                result.append(name)
                break

    return result


def replace_last_fixed_block(public_py_text: str, names: list[str]) -> str:
    new_block = "LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS = {\\n"
    for name in names:
        new_block += f'    "{name}",\\n'
    new_block += "}"

    pattern = r"LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS\\s*=\\s*\\{.*?\\}"
    replaced, count = re.subn(pattern, new_block, public_py_text, count=1, flags=re.DOTALL)

    if count != 1:
        raise RuntimeError("LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS ブロックを見つけられませんでした。")

    return replaced


def main() -> None:
    public_py_text = PUBLIC_PY_PATH.read_text(encoding="utf-8")
    candidate_names = extract_profile_names(public_py_text)

    html = fetch_html(MEMBERS_URL)
    first_team_block = extract_first_team_block(html)
    detected_names = detect_first_team_position_players(first_team_block, candidate_names)

    if not detected_names:
        raise RuntimeError("一軍メンバーの野手を検出できませんでした。")

    updated_text = replace_last_fixed_block(public_py_text, detected_names)

    if updated_text != public_py_text:
        PUBLIC_PY_PATH.write_text(updated_text, encoding="utf-8")
        print("Updated LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS")
    else:
        print("No changes needed")


if __name__ == "__main__":
    main()
