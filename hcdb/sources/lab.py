"""연구실·창업지원 프로그램 페이지: 사이트마다 구조가 달라서 '설정 파일 + CSS 선택자' 방식.

config/labs.json 의 각 항목:
{
  "name": "OO대 의공학과 창업팀 목록",
  "url": "https://...",
  "render": false,                     # JS 로 그려지는 페이지면 true (Playwright)
  "item": "ul.team-list > li",         # 한 팀/기업을 감싸는 요소
  "fields": {                          # 항목 안에서의 선택자. '@href' 처럼 속성도 가능
    "name": ".team-name",
    "affiliation": ".lab",
    "service_summary": ".desc",
    "url": "a@href"
  },
  "defaults": {"is_pre_startup": 1, "program": "실험실창업"},
  "keyword_filter": true               # 헬스케어 키워드가 있는 항목만
}
"""
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import db
from ..fetch import fetch_doc, http_get, render
from ..fields import COMPANY_FIELDS
from .news import _HEALTH


def _pick(node, sel: str, base: str):
    attr = None
    if "@" in sel:
        sel, attr = sel.split("@", 1)
    el = node.select_one(sel) if sel else node
    if el is None:
        return None
    if attr:
        v = el.get(attr)
        return urljoin(base, v) if attr in ("href", "src") and v else v
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) or None


def scrape(cfg: dict) -> list[dict]:
    html = render(cfg["url"], cfg.get("item")) if cfg.get("render") else http_get(cfg["url"]).text
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for node in soup.select(cfg["item"]):
        rec = {k: _pick(node, s, cfg["url"]) for k, s in cfg["fields"].items()}
        rec["_text"] = node.get_text(" ", strip=True)
        out.append(rec)
    return out


def run(con, config_path: str, dry_run=False) -> None:
    cfgs = json.loads(open(config_path, encoding="utf-8").read())
    with db.run_log(con, "lab", config_path) as st:
        for cfg in cfgs:
            recs = scrape(cfg)
            print(f"[{cfg['name']}] {len(recs)}건")
            # 원문도 남겨둠(근거 추적)
            fetch_doc(con, cfg["url"], "lab", use_render=cfg.get("render", False))
            st["docs"] += 1
            for rec in recs:
                if cfg.get("keyword_filter") and not _HEALTH.search(rec["_text"]):
                    continue
                if dry_run:
                    print("  ", {k: v for k, v in rec.items() if k != "_text"})
                    continue
                if not rec.get("name"):
                    continue
                cid = db.get_or_create_company(con, rec["name"], source_url=cfg["url"])
                vals = {**cfg.get("defaults", {}), **rec}
                program = vals.pop("program", None)
                db.set_fields(con, cid, {k: v for k, v in vals.items() if k in COMPANY_FIELDS and k != "name"},
                              "lab", cfg["url"], "중")
                if program:
                    db.add_tag(con, cid, "program", program, cfg["url"])
                st["facts"] += 1
            con.commit()
