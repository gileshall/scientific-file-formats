#!/usr/bin/env python3
"""
build_site.py — render scientific_file_formats.yaml to a single-file HTML reference.

Usage:
    python build_site.py [INPUT.yaml] [-o OUTPUT.html]

Output is a single self-contained HTML file (embedded CSS+JS, no external deps).
Three views:
  • Catalog    — entries grouped by field, with search + field filter
  • Timeline   — SVG scatter of formats by year × field
  • Patterns   — the cross_domain_observations section

Cross-references (`see:` keys) resolve to anchor links when the target exists.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

import yaml


# ───────────────────────────── data model ──────────────────────────────


@dataclasses.dataclass
class Entry:
    """One concrete file-format entry, located within the YAML tree."""

    name: str
    field: str           # top-level key, e.g. 'life_sciences'
    subfield: str        # nested key, e.g. 'genomics' (may be '')
    field_label: str     # display label for field
    subfield_label: str  # display label for subfield
    slug: str            # stable anchor id
    data: dict[str, Any]


SKIP_TOP_LEVEL = {"schema_notes", "cross_domain_observations", "omitted"}


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s.-]", "", s.lower())
    s = re.sub(r"[\s_/]+", "-", s)
    return s.strip("-.") or "x"


def humanize(key: str) -> str:
    """Convert snake_case keys to display labels."""
    overrides = {
        "ml_and_arrays": "ML & arrays",
        "cross_cutting_containers": "cross-cutting containers",
        "high_energy": "high-energy physics",
        "climate_and_atmospheric": "climate & atmospheric",
        "geophysics_well_logs": "geophysics / well logs",
        "social_sciences_and_stats": "social sciences & stats",
        "computing_and_ml": "computing & ML",
        "cad": "CAD",
        "cryo_em": "cryo-EM",
        "genomics_clinical": "clinical genomics",
        "single_cell_and_omics_containers": "single-cell & omics containers",
        "microscopy_and_bioimaging": "microscopy & bioimaging",
        "proteomics_and_mass_spec": "proteomics & mass spec",
        "pathways_and_systems_biology": "pathways & systems biology",
        "sequence_database_flatfiles": "sequence database flatfiles",
        "structural_biology": "structural biology",
    }
    if key in overrides:
        return overrides[key]
    return key.replace("_", " ")


def collect_entries(data: dict) -> list[Entry]:
    """Walk the YAML tree, return a flat list of Entry objects in source order."""
    entries: list[Entry] = []
    for top_key, top_val in data.items():
        if top_key in SKIP_TOP_LEVEL:
            continue
        field_label = humanize(top_key)
        if isinstance(top_val, list):
            for raw in top_val:
                if isinstance(raw, dict) and "name" in raw:
                    entries.append(
                        Entry(
                            name=raw["name"],
                            field=top_key,
                            subfield="",
                            field_label=field_label,
                            subfield_label="",
                            slug=slugify(f"{top_key}-{raw['name']}"),
                            data=raw,
                        )
                    )
        elif isinstance(top_val, dict):
            for sub_key, sub_val in top_val.items():
                if not isinstance(sub_val, list):
                    continue
                subfield_label = humanize(sub_key)
                for raw in sub_val:
                    if isinstance(raw, dict) and "name" in raw:
                        entries.append(
                            Entry(
                                name=raw["name"],
                                field=top_key,
                                subfield=sub_key,
                                field_label=field_label,
                                subfield_label=subfield_label,
                                slug=slugify(f"{top_key}-{sub_key}-{raw['name']}"),
                                data=raw,
                            )
                        )
    return entries


def build_slug_index(entries: list[Entry]) -> dict[str, str]:
    """Map 'top.sub' and 'top' paths to the slug of their first entry,
    so `see: life_sciences.genomics` resolves to that subfield's first entry."""
    idx: dict[str, str] = {}
    for e in entries:
        for key in (f"{e.field}.{e.subfield}", e.field):
            if key and key not in idx:
                idx[key] = e.slug
        # also index by name for entries that cross-ref by name
        idx.setdefault(e.name.lower(), e.slug)
    return idx


# ─────────────────────────────── rendering ─────────────────────────────


def esc(x: Any) -> str:
    if x is None:
        return ""
    return html.escape(str(x), quote=True)


def is_url(s: Any) -> bool:
    return isinstance(s, str) and s.startswith(("http://", "https://"))


def render_link(url: str, label: str | None = None) -> str:
    """Render an external link with a small arrow glyph."""
    label = label or url
    return (
        f'<a href="{esc(url)}" target="_blank" rel="noopener" class="ext">'
        f"{esc(label)}<span class=\"arr\" aria-hidden=\"true\">↗</span></a>"
    )


def render_efficiency(n: Any) -> str:
    """1–5 efficiency rating as filled/empty squares."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return esc(n)
    if not 1 <= n <= 5:
        return esc(n)
    filled = "■" * n
    empty = "□" * (5 - n)
    return (
        f'<span class="rating" title="{n}/5">'
        f'<span class="rating-filled">{filled}</span>'
        f'<span class="rating-empty">{empty}</span></span>'
    )


def render_list_inline(items: list, sep: str = " · ") -> str:
    return sep.join(esc(i) for i in items if i is not None)


def render_kv_row(label: str, value: str) -> str:
    return f'<div class="kv-row"><dt>{esc(label)}</dt><dd>{value}</dd></div>'


# Fields that go in the compact key-value grid at the top of each card,
# in display order.
KV_FIELDS: list[tuple[str, str]] = [
    ("purpose", "purpose"),
    ("dimensionality", "dimensionality"),
    ("encoding", "encoding"),
    ("efficiency", "efficiency"),
    ("compression", "compression"),
    ("indexable", "indexable"),
    ("streaming", "streaming"),
    ("self_describing", "self-describing"),
    ("governing_body", "governance"),
    ("license", "license"),
    ("cloud_native", "cloud-native"),
    ("flagship_corpus", "flagship corpus"),
    ("successor", "successor"),
]

# Long-prose fields, rendered as separate sections under the kv grid.
PROSE_FIELDS: list[tuple[str, str]] = [
    ("story", "story"),
    ("quirks", "quirks"),
]


def render_entry(e: Entry, slug_index: dict[str, str]) -> str:
    d = e.data
    year = d.get("age")
    badges: list[str] = []
    if year is not None:
        badges.append(f'<span class="badge badge-year">{esc(year)}</span>')

    # Header row
    header = (
        f'<header class="card-head">'
        f'<h3 id="{esc(e.slug)}" class="card-name">{esc(e.name)}</h3>'
        f'<div class="card-badges">{"".join(badges)}</div>'
        f"</header>"
    )

    # Handle `see:` cross-refs (an entry that points to another)
    if "see" in d:
        target = str(d["see"])
        target_slug = slug_index.get(target.lower()) or slug_index.get(target)
        if target_slug:
            ref = f'<a class="seeref" href="#{esc(target_slug)}">see {esc(target)} →</a>'
        else:
            ref = f'<span class="seeref muted">see {esc(target)}</span>'
        return f'<article class="card card-stub" data-slug="{esc(e.slug)}">{header}<p class="see">{ref}</p></article>'

    # KV grid
    kv_html: list[str] = []
    for key, label in KV_FIELDS:
        if key not in d:
            continue
        v = d[key]
        if v is None or v == "":
            continue
        if key == "efficiency":
            kv_html.append(render_kv_row(label, render_efficiency(v)))
        elif key == "purpose":
            kv_html.append(
                f'<div class="kv-row kv-purpose">'
                f'<dt>{esc(label)}</dt><dd>{esc(v)}</dd></div>'
            )
        elif isinstance(v, list):
            kv_html.append(render_kv_row(label, render_list_inline(v)))
        elif isinstance(v, bool):
            kv_html.append(render_kv_row(label, "yes" if v else "no"))
        else:
            kv_html.append(render_kv_row(label, esc(v)))

    # Spec link gets its own row, prominent
    spec = d.get("spec")
    if spec:
        spec_label = re.sub(r"^https?://(www\.)?", "", str(spec)).split("/")[0]
        kv_html.append(render_kv_row("spec", render_link(spec, spec_label)))

    # estimated_count_and_size (structured)
    eccs = d.get("estimated_count_and_size")
    if isinstance(eccs, dict):
        order = eccs.get("order_of_magnitude", "")
        basis = eccs.get("basis", "")
        block = f'<div class="ecs">'
        if order:
            block += f'<div class="ecs-order">{esc(order)}</div>'
        if basis:
            block += f'<div class="ecs-basis"><em>basis:</em> {esc(basis)}</div>'
        block += "</div>"
        kv_html.append(render_kv_row("est. count / size", block))

    # Lists rendered as chip rows
    chip_fields = [
        ("public_repositories", "repositories"),
        ("open_source_tooling", "tooling"),
        ("shared_by", "shared by"),
    ]
    for key, label in chip_fields:
        v = d.get(key)
        if isinstance(v, list) and v:
            chips = "".join(f'<span class="chip">{esc(i)}</span>' for i in v)
            kv_html.append(render_kv_row(label, f'<div class="chips">{chips}</div>'))

    # Prose sections
    prose_html: list[str] = []
    for key, label in PROSE_FIELDS:
        v = d.get(key)
        if v:
            prose_html.append(
                f'<section class="prose-block prose-{esc(key)}">'
                f'<h4>{esc(label)}</h4><p>{esc(v).replace(chr(10), "<br>")}</p>'
                f"</section>"
            )

    kv_block = (
        f'<dl class="kv">{"".join(kv_html)}</dl>' if kv_html else ""
    )
    prose_block = "".join(prose_html)

    return (
        f'<article class="card" id="card-{esc(e.slug)}" '
        f'data-slug="{esc(e.slug)}" '
        f'data-field="{esc(e.field)}" '
        f'data-subfield="{esc(e.subfield)}" '
        f'data-year="{esc(year) if year is not None else ""}" '
        f'data-name="{esc(e.name.lower())}">'
        f"{header}{kv_block}{prose_block}"
        f"</article>"
    )


def render_field_section(field: str, entries: list[Entry], slug_index: dict[str, str]) -> str:
    """All entries within one top-level field, grouped by subfield."""
    field_label = entries[0].field_label
    by_sub: dict[str, list[Entry]] = {}
    for e in entries:
        by_sub.setdefault(e.subfield, []).append(e)

    sections_html: list[str] = []
    for sub, sub_entries in by_sub.items():
        sub_label = sub_entries[0].subfield_label
        sub_slug = slugify(f"{field}-{sub}") if sub else slugify(field)
        if sub:
            heading = (
                f'<h2 class="subfield-head" id="sub-{esc(sub_slug)}">'
                f'<span class="subfield-tick">§</span>{esc(sub_label)}'
                f' <span class="subfield-count">{len(sub_entries)}</span></h2>'
            )
        else:
            heading = ""
        cards = "".join(render_entry(e, slug_index) for e in sub_entries)
        sections_html.append(
            f'<section class="subfield" data-subfield="{esc(sub)}">'
            f"{heading}{cards}</section>"
        )

    return (
        f'<section class="field" id="field-{esc(slugify(field))}" data-field="{esc(field)}">'
        f'<header class="field-head">'
        f'<div class="field-eyebrow">field</div>'
        f'<h1 class="field-title">{esc(field_label)}</h1>'
        f'<div class="field-count">{len(entries)} formats</div>'
        f"</header>"
        f"{''.join(sections_html)}"
        f"</section>"
    )


def render_sidebar(entries: list[Entry]) -> str:
    by_field: dict[str, list[Entry]] = {}
    for e in entries:
        by_field.setdefault(e.field, []).append(e)

    items: list[str] = []
    for field, field_entries in by_field.items():
        field_slug = slugify(field)
        field_label = field_entries[0].field_label
        sub_links: list[str] = []
        seen_subs: set[str] = set()
        for e in field_entries:
            if e.subfield and e.subfield not in seen_subs:
                seen_subs.add(e.subfield)
                sub_slug = slugify(f"{field}-{e.subfield}")
                sub_links.append(
                    f'<li><a href="#sub-{esc(sub_slug)}">{esc(e.subfield_label)}</a></li>'
                )
        items.append(
            f'<li class="nav-field">'
            f'<a href="#field-{esc(field_slug)}" class="nav-field-link">'
            f'<span>{esc(field_label)}</span>'
            f'<span class="nav-count">{len(field_entries)}</span></a>'
            f'{"<ul>" + "".join(sub_links) + "</ul>" if sub_links else ""}'
            f"</li>"
        )

    return f'<nav class="sidebar"><ul class="nav-list">{"".join(items)}</ul></nav>'


def render_patterns(raw: dict) -> str:
    """Render cross_domain_observations as the Patterns view."""
    if not isinstance(raw, dict):
        return ""
    blocks: list[str] = []
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        label = humanize(key)
        examples = val.get("examples", [])
        note = val.get("note", "")
        ex_html = "".join(f'<li>{esc(x)}</li>' for x in examples)
        blocks.append(
            f'<section class="pattern">'
            f'<h2 class="pattern-title">{esc(label)}</h2>'
            f'<ul class="pattern-examples">{ex_html}</ul>'
            f'<p class="pattern-note">{esc(note).replace(chr(10), "<br>")}</p>'
            f"</section>"
        )
    return f'<div class="patterns">{"".join(blocks)}</div>'


def build_timeline_data(entries: list[Entry]) -> list[dict]:
    """Extract (name, year, field) tuples for the timeline view."""
    out = []
    for e in entries:
        year = e.data.get("age")
        if year is None:
            continue
        try:
            y = int(year)
        except (TypeError, ValueError):
            # ages stored as "1986 (TIFF) / 2005 (OME-TIFF)" — pick the first int
            m = re.search(r"\b(19|20)\d{2}\b", str(year))
            if not m:
                continue
            y = int(m.group(0))
        out.append(
            {
                "name": e.name,
                "year": y,
                "field": e.field,
                "field_label": e.field_label,
                "slug": e.slug,
                "purpose": e.data.get("purpose", ""),
            }
        )
    return out


# ─────────────────────────────── CSS & JS ──────────────────────────────


CSS = r"""
:root {
  --paper: #fbfaf5;
  --ink: #1a1d27;
  --ink-soft: #4a4d57;
  --ink-faint: #8d8e95;
  --rule: #d8d4c8;
  --rule-soft: #e8e4d8;
  --accent: #b8501e;
  --accent-soft: #f3d9c6;
  --highlight: #fff6c8;
  --serif: 'Iowan Old Style', 'Palatino Linotype', Palatino, 'Hoefler Text',
           Cambria, Georgia, serif;
  --mono: ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas,
          'Liberation Mono', monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper: #16171c;
    --ink: #ebe7dc;
    --ink-soft: #b0acA0;
    --ink-faint: #7a7770;
    --rule: #2c2d33;
    --rule-soft: #232429;
    --accent: #e88a52;
    --accent-soft: #3a2418;
    --highlight: #3a3520;
  }
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
  font-feature-settings: "kern", "liga", "onum";
}

a {
  color: var(--accent);
  text-decoration: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
}
a:hover { text-decoration-thickness: 2px; }

.ext .arr {
  display: inline-block;
  margin-left: 0.2em;
  font-size: 0.85em;
  opacity: 0.7;
}

/* ─── Masthead ─── */
.masthead {
  border-bottom: 1px solid var(--rule);
  padding: 32px 40px 24px;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 32px;
  flex-wrap: wrap;
}
.masthead-title {
  font-family: var(--mono);
  font-size: 13px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  margin: 0;
  color: var(--ink-soft);
  font-weight: normal;
}
.masthead h1 {
  font-family: var(--serif);
  font-size: 42px;
  line-height: 1.05;
  font-weight: normal;
  font-style: italic;
  margin: 4px 0 8px;
  letter-spacing: -0.01em;
}
.masthead-meta {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink-faint);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  text-align: right;
}
.masthead-meta strong {
  display: block;
  color: var(--ink);
  font-weight: normal;
  font-size: 22px;
  font-family: var(--serif);
  font-style: italic;
  text-transform: none;
  letter-spacing: 0;
}

/* ─── View tabs ─── */
.tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--rule);
  padding: 0 40px;
  position: sticky;
  top: 0;
  background: var(--paper);
  z-index: 10;
}
.tab {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  padding: 14px 20px;
  cursor: pointer;
  color: var(--ink-soft);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  text-decoration: none;
}
.tab:hover { color: var(--ink); }
.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.tabs-spacer { flex: 1; }

/* ─── Controls (search + filter) ─── */
.controls {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 16px 40px;
  border-bottom: 1px solid var(--rule);
  flex-wrap: wrap;
}
.search {
  flex: 1;
  min-width: 240px;
  font-family: var(--mono);
  font-size: 14px;
  padding: 8px 12px;
  background: transparent;
  border: 1px solid var(--rule);
  color: var(--ink);
  outline: none;
}
.search:focus { border-color: var(--accent); }
.search::placeholder { color: var(--ink-faint); }
.filter-pill {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 6px 10px;
  border: 1px solid var(--rule);
  background: transparent;
  color: var(--ink-soft);
  cursor: pointer;
}
.filter-pill:hover { color: var(--ink); border-color: var(--ink-soft); }
.filter-pill.active {
  color: var(--paper);
  background: var(--ink);
  border-color: var(--ink);
}

/* ─── Layout ─── */
.layout {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 0;
}
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { display: none; }
  .masthead, .tabs, .controls, .main { padding-left: 20px; padding-right: 20px; }
}

.sidebar {
  border-right: 1px solid var(--rule);
  padding: 28px 0 80px;
  position: sticky;
  top: 48px;
  align-self: start;
  height: calc(100vh - 48px);
  overflow-y: auto;
  font-size: 14px;
}
.sidebar::-webkit-scrollbar { width: 6px; }
.sidebar::-webkit-scrollbar-thumb { background: var(--rule); }

.nav-list { list-style: none; margin: 0; padding: 0 20px; }
.nav-list ul {
  list-style: none;
  padding-left: 12px;
  margin: 2px 0 8px;
  border-left: 1px solid var(--rule-soft);
}
.nav-list li a {
  display: flex;
  justify-content: space-between;
  text-decoration: none;
  color: var(--ink-soft);
  padding: 3px 6px;
  font-size: 13px;
  font-family: var(--serif);
}
.nav-list li a:hover {
  color: var(--accent);
  background: var(--rule-soft);
}
.nav-field > .nav-field-link {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink);
  margin-top: 16px;
  padding-top: 8px;
  border-top: 1px solid var(--rule-soft);
}
.nav-field:first-child > .nav-field-link {
  margin-top: 0;
  border-top: none;
}
.nav-count {
  font-family: var(--mono);
  color: var(--ink-faint);
  font-size: 10px;
}

/* ─── Main column ─── */
.main {
  padding: 40px;
  min-width: 0;
}

.view { display: none; }
.view.active { display: block; }

/* ─── Field sections ─── */
.field { margin-bottom: 80px; }
.field-head {
  padding-bottom: 16px;
  border-bottom: 2px solid var(--ink);
  margin-bottom: 32px;
  display: flex;
  align-items: baseline;
  gap: 16px;
}
.field-eyebrow {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--accent);
}
.field-title {
  flex: 1;
  font-family: var(--serif);
  font-size: 32px;
  font-style: italic;
  font-weight: normal;
  margin: 0;
}
.field-count {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink-faint);
}

.subfield-head {
  font-family: var(--mono);
  font-size: 13px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--ink-soft);
  margin: 40px 0 16px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--rule);
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.subfield-tick {
  color: var(--accent);
  margin-right: 6px;
}
.subfield-count {
  margin-left: auto;
  color: var(--ink-faint);
  font-size: 11px;
}

/* ─── Cards ─── */
.card {
  padding: 24px 0;
  border-bottom: 1px solid var(--rule-soft);
  scroll-margin-top: 80px;
}
.card:last-child { border-bottom: none; }
.card.hidden { display: none; }

.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
  gap: 16px;
}
.card-name {
  font-family: var(--serif);
  font-size: 26px;
  font-weight: normal;
  margin: 0;
  letter-spacing: -0.01em;
}
.card-badges { display: flex; gap: 6px; }
.badge {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.05em;
  padding: 3px 8px;
  border: 1px solid var(--rule);
  color: var(--ink-soft);
}
.badge-year {
  color: var(--accent);
  border-color: var(--accent);
  font-feature-settings: "tnum";
}

.card-stub .card-name { font-size: 18px; color: var(--ink-soft); font-style: italic; }
.card-stub { padding: 12px 0; }
.see { margin: 0; font-family: var(--mono); font-size: 13px; }
.seeref { color: var(--accent); }
.seeref.muted { color: var(--ink-faint); }

.kv {
  margin: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 0;
}
.kv-row {
  display: grid;
  grid-template-columns: 140px minmax(0, 1fr);
  gap: 16px;
  padding: 6px 0;
  border-top: 1px solid var(--rule-soft);
  align-items: baseline;
}
.kv-row:last-child { border-bottom: 1px solid var(--rule-soft); }
.kv-row dt {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin: 0;
}
.kv-row dd {
  margin: 0;
  font-size: 15px;
  line-height: 1.5;
  color: var(--ink);
}
.kv-purpose dd { font-style: italic; }

.rating { font-family: var(--mono); letter-spacing: 1px; }
.rating-filled { color: var(--accent); }
.rating-empty { color: var(--rule); }

.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip {
  font-family: var(--mono);
  font-size: 11px;
  padding: 2px 8px;
  background: var(--rule-soft);
  color: var(--ink-soft);
  border: 1px solid transparent;
}

.ecs-order { font-size: 14px; }
.ecs-basis {
  font-size: 13px;
  color: var(--ink-faint);
  margin-top: 2px;
}
.ecs-basis em { font-style: italic; color: var(--ink-soft); }

.prose-block {
  margin-top: 16px;
  padding-left: 16px;
  border-left: 2px solid var(--accent-soft);
}
.prose-block h4 {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 6px;
  font-weight: normal;
}
.prose-block p {
  margin: 0;
  font-size: 15px;
  line-height: 1.55;
  color: var(--ink-soft);
}

mark.search-hit {
  background: var(--highlight);
  color: var(--ink);
  padding: 0 2px;
}

/* ─── Timeline view ─── */
#timeline-view { padding: 24px 40px; }
.timeline-wrap {
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--rule);
  background: var(--paper);
}
.timeline-svg {
  display: block;
  min-width: 1000px;
  font-family: var(--mono);
  font-size: 10px;
}
.tl-axis { stroke: var(--rule); stroke-width: 1; }
.tl-grid { stroke: var(--rule-soft); stroke-width: 1; }
.tl-tick-label { fill: var(--ink-faint); }
.tl-field-label { fill: var(--ink-soft); font-size: 11px; }
.tl-dot {
  cursor: pointer;
  transition: r 0.15s ease;
}
.tl-dot:hover { stroke: var(--ink); stroke-width: 2; }
.tl-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
  margin-top: 16px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-soft);
}
.tl-legend-swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 6px;
  vertical-align: middle;
}
#tl-tooltip {
  position: fixed;
  background: var(--ink);
  color: var(--paper);
  padding: 8px 12px;
  font-family: var(--mono);
  font-size: 12px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s;
  max-width: 280px;
  z-index: 100;
}
#tl-tooltip strong { font-family: var(--serif); font-size: 14px; }
#tl-tooltip .tl-tt-meta { color: var(--accent); font-size: 11px; margin-top: 2px; }
#tl-tooltip .tl-tt-purpose { color: var(--ink-faint); font-size: 11px; margin-top: 4px; font-family: var(--serif); }

/* ─── Patterns view ─── */
#patterns-view { padding: 40px; max-width: 760px; margin: 0 auto; }
.pattern { margin-bottom: 48px; }
.pattern-title {
  font-family: var(--serif);
  font-size: 26px;
  font-style: italic;
  font-weight: normal;
  margin: 0 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--rule);
}
.pattern-examples {
  list-style: none;
  padding: 0;
  margin: 0 0 16px;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--ink-soft);
}
.pattern-examples li { padding: 3px 0; border-bottom: 1px dotted var(--rule-soft); }
.pattern-examples li:last-child { border-bottom: none; }
.pattern-note {
  font-size: 16px;
  line-height: 1.6;
  color: var(--ink);
  font-style: italic;
}

/* ─── No-results state ─── */
.no-results {
  padding: 60px 20px;
  text-align: center;
  font-family: var(--mono);
  font-size: 13px;
  color: var(--ink-faint);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

footer.colophon {
  border-top: 1px solid var(--rule);
  padding: 32px 40px;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--ink-faint);
  line-height: 1.6;
}
"""


JS = r"""
(function () {
  'use strict';

  // ─── State ───
  const state = {
    view: 'catalog',
    search: '',
    activeFields: new Set(),
  };

  // ─── View switching ───
  function setView(name) {
    state.view = name;
    document.querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('active', t.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach((v) => {
      v.classList.toggle('active', v.id === name + '-view');
    });
    // hash for shareability
    if (history.replaceState) {
      history.replaceState(null, '', '#view=' + name);
    }
    if (name === 'timeline') renderTimeline();
  }

  // ─── Search / filter ───
  function escapeReg(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function applyFilters() {
    const q = state.search.trim().toLowerCase();
    const fields = state.activeFields;
    const cards = document.querySelectorAll('#catalog-view .card');
    let visible = 0;
    cards.forEach((card) => {
      const matchesField = fields.size === 0 || fields.has(card.dataset.field);
      let matchesQuery = true;
      if (q) {
        const haystack = card.innerText.toLowerCase();
        matchesQuery = haystack.indexOf(q) !== -1;
      }
      const show = matchesField && matchesQuery;
      card.classList.toggle('hidden', !show);
      if (show) visible++;
    });
    // hide empty subfield/field containers
    document.querySelectorAll('#catalog-view .subfield').forEach((sub) => {
      const any = sub.querySelectorAll('.card:not(.hidden)').length > 0;
      sub.style.display = any ? '' : 'none';
    });
    document.querySelectorAll('#catalog-view .field').forEach((f) => {
      const any = f.querySelectorAll('.card:not(.hidden)').length > 0;
      f.style.display = any ? '' : 'none';
    });
    const noRes = document.getElementById('no-results');
    if (noRes) noRes.style.display = visible === 0 ? '' : 'none';
    highlightSearch(q);
  }

  function highlightSearch(q) {
    // strip prior marks
    document.querySelectorAll('mark.search-hit').forEach((m) => {
      const parent = m.parentNode;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      parent.normalize();
    });
    if (!q || q.length < 2) return;
    const re = new RegExp('(' + escapeReg(q) + ')', 'gi');
    document.querySelectorAll('#catalog-view .card:not(.hidden)').forEach((card) => {
      // only highlight in card-name and kv-purpose dd
      const targets = card.querySelectorAll('.card-name, .kv-purpose dd');
      targets.forEach((el) => {
        const t = el.textContent;
        if (re.test(t)) {
          el.innerHTML = el.textContent.replace(re, '<mark class="search-hit">$1</mark>');
        }
      });
    });
  }

  // ─── Timeline ───
  let timelineRendered = false;

  // 12 stable, muted colors for the up-to-12 field categories
  const PALETTE = [
    '#b8501e', '#3e6c70', '#7a5a3c', '#5a6b8c', '#8c6b9e', '#6f8a47',
    '#b87a3c', '#4a6b4a', '#8a4848', '#3e7a8c', '#7a4a7a', '#5a5a3c',
  ];

  function renderTimeline() {
    if (timelineRendered) return;
    timelineRendered = true;
    const data = window.__TIMELINE__;
    if (!data || data.length === 0) return;

    const fields = [...new Set(data.map((d) => d.field))];
    const fieldLabels = {};
    data.forEach((d) => { fieldLabels[d.field] = d.field_label; });
    const colorMap = {};
    fields.forEach((f, i) => { colorMap[f] = PALETTE[i % PALETTE.length]; });

    const years = data.map((d) => d.year);
    const yMin = Math.min(...years);
    const yMax = Math.max(...years);
    const minYear = Math.floor(yMin / 10) * 10;
    const maxYear = Math.ceil((yMax + 2) / 10) * 10;

    const W = 1400, marginL = 180, marginR = 40, marginT = 30, marginB = 50;
    const rowH = 32;
    const H = marginT + marginB + fields.length * rowH;
    const plotW = W - marginL - marginR;

    const xScale = (y) => marginL + ((y - minYear) / (maxYear - minYear)) * plotW;
    const yScale = (f) => marginT + fields.indexOf(f) * rowH + rowH / 2;

    let svg = `<svg class="timeline-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;

    // decade gridlines + labels
    for (let yr = minYear; yr <= maxYear; yr += 10) {
      const x = xScale(yr);
      svg += `<line class="tl-grid" x1="${x}" y1="${marginT}" x2="${x}" y2="${H - marginB}"/>`;
      svg += `<text class="tl-tick-label" x="${x}" y="${H - marginB + 16}" text-anchor="middle">${yr}</text>`;
    }
    // axis
    svg += `<line class="tl-axis" x1="${marginL}" y1="${H - marginB}" x2="${W - marginR}" y2="${H - marginB}"/>`;

    // field labels + row separators
    fields.forEach((f, i) => {
      const y = yScale(f);
      svg += `<text class="tl-field-label" x="${marginL - 12}" y="${y + 4}" text-anchor="end">${fieldLabels[f] || f}</text>`;
      if (i > 0) {
        const yLine = marginT + i * rowH;
        svg += `<line class="tl-grid" x1="${marginL}" y1="${yLine}" x2="${W - marginR}" y2="${yLine}" stroke-dasharray="2 4"/>`;
      }
    });

    // dots — jitter within row to reduce overlap
    // group by (field, year) to detect collisions
    const groups = {};
    data.forEach((d) => {
      const k = d.field + ':' + d.year;
      (groups[k] = groups[k] || []).push(d);
    });
    Object.values(groups).forEach((items) => {
      items.forEach((d, idx) => {
        const baseY = yScale(d.field);
        const offset = (idx - (items.length - 1) / 2) * 8;
        const cx = xScale(d.year);
        const cy = baseY + offset;
        const fill = colorMap[d.field];
        svg += `<circle class="tl-dot" cx="${cx}" cy="${cy}" r="5" fill="${fill}" fill-opacity="0.75" stroke="${fill}" stroke-width="1" data-slug="${d.slug}" data-name="${escapeAttr(d.name)}" data-year="${d.year}" data-field-label="${escapeAttr(d.field_label)}" data-purpose="${escapeAttr(d.purpose || '')}"/>`;
      });
    });

    svg += '</svg>';
    document.getElementById('timeline-svg-wrap').innerHTML = svg;

    // legend
    const legend = fields.map((f) =>
      `<span><span class="tl-legend-swatch" style="background:${colorMap[f]}"></span>${escapeText(fieldLabels[f] || f)}</span>`
    ).join('');
    document.getElementById('tl-legend').innerHTML = legend;

    // hover tooltip
    const tt = document.getElementById('tl-tooltip');
    document.querySelectorAll('.tl-dot').forEach((dot) => {
      dot.addEventListener('mouseenter', (ev) => {
        const t = ev.target;
        tt.innerHTML =
          '<strong>' + t.getAttribute('data-name') + '</strong>'
          + '<div class="tl-tt-meta">' + t.getAttribute('data-year')
          + ' · ' + t.getAttribute('data-field-label') + '</div>'
          + '<div class="tl-tt-purpose">' + t.getAttribute('data-purpose') + '</div>';
        tt.style.opacity = '1';
      });
      dot.addEventListener('mousemove', (ev) => {
        tt.style.left = (ev.clientX + 14) + 'px';
        tt.style.top = (ev.clientY + 14) + 'px';
      });
      dot.addEventListener('mouseleave', () => { tt.style.opacity = '0'; });
      dot.addEventListener('click', (ev) => {
        const slug = ev.target.getAttribute('data-slug');
        setView('catalog');
        setTimeout(() => {
          const el = document.getElementById(slug);
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            el.parentElement.style.background = 'var(--highlight)';
            setTimeout(() => { el.parentElement.style.background = ''; }, 1800);
          }
        }, 60);
      });
    });
  }

  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  }
  function escapeText(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  }

  // ─── Wiring ───
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.addEventListener('click', (ev) => {
        ev.preventDefault();
        setView(tab.dataset.view);
      });
    });
    const search = document.getElementById('search');
    if (search) {
      search.addEventListener('input', (ev) => {
        state.search = ev.target.value;
        applyFilters();
      });
    }
    document.querySelectorAll('.filter-pill').forEach((pill) => {
      pill.addEventListener('click', () => {
        const f = pill.dataset.field;
        if (state.activeFields.has(f)) {
          state.activeFields.delete(f);
          pill.classList.remove('active');
        } else {
          state.activeFields.add(f);
          pill.classList.add('active');
        }
        applyFilters();
      });
    });

    // honor hash on load
    const m = location.hash.match(/view=(\w+)/);
    if (m && ['catalog', 'timeline', 'patterns'].includes(m[1])) {
      setView(m[1]);
    }
  });
})();
"""


# ─────────────────────────────── page assembly ─────────────────────────


def render_page(yaml_data: dict, entries: list[Entry], slug_index: dict[str, str]) -> str:
    # Group by field, preserving source order
    by_field: dict[str, list[Entry]] = {}
    for e in entries:
        by_field.setdefault(e.field, []).append(e)

    field_sections = "".join(
        render_field_section(f, es, slug_index) for f, es in by_field.items()
    )

    # filter pills (one per top-level field)
    filter_pills = "".join(
        f'<button class="filter-pill" data-field="{esc(f)}">{esc(es[0].field_label)}</button>'
        for f, es in by_field.items()
    )

    sidebar = render_sidebar(entries)
    patterns = render_patterns(yaml_data.get("cross_domain_observations", {}))
    timeline_data = build_timeline_data(entries)

    total_entries = len([e for e in entries if "see" not in e.data])
    field_count = len(by_field)

    head = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A Field Guide to Scientific File Formats</title>
<style>{CSS}</style>
</head>
<body>
<header class="masthead">
  <div>
    <p class="masthead-title">A field guide to</p>
    <h1>Scientific File Formats</h1>
  </div>
  <div class="masthead-meta">
    <strong>{total_entries}</strong>
    formats indexed
    <br>across <strong>{field_count}</strong>
    fields of science
  </div>
</header>

<nav class="tabs">
  <a class="tab active" data-view="catalog" href="#view=catalog">Catalog</a>
  <a class="tab" data-view="timeline" href="#view=timeline">Timeline</a>
  <a class="tab" data-view="patterns" href="#view=patterns">Patterns</a>
</nav>

<div class="controls">
  <input id="search" class="search" type="search" placeholder="search formats, purposes, stories…" autocomplete="off">
  {filter_pills}
</div>

<div class="view active" id="catalog-view">
  <div class="layout">
    {sidebar}
    <main class="main">
      {field_sections}
      <div id="no-results" class="no-results" style="display:none">no matches</div>
    </main>
  </div>
</div>

<div class="view" id="timeline-view">
  <div class="timeline-wrap" id="timeline-svg-wrap"></div>
  <div class="tl-legend" id="tl-legend"></div>
  <div id="tl-tooltip"></div>
</div>

<div class="view" id="patterns-view">
  {patterns}
</div>

<footer class="colophon">
  Generated from scientific_file_formats.yaml ·
  click a timeline dot to jump to its catalog entry ·
  search matches anywhere in the card text
</footer>

<script>window.__TIMELINE__ = {json.dumps(timeline_data)};</script>
<script>{JS}</script>
</body>
</html>
"""
    return head


# ─────────────────────────────── CLI ───────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "input",
        nargs="?",
        default="scientific_file_formats.yaml",
        help="Input YAML (default: scientific_file_formats.yaml)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output HTML path (default: <input>.html alongside input)",
    )
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")

    with in_path.open() as f:
        data = yaml.safe_load(f)

    entries = collect_entries(data)
    slug_index = build_slug_index(entries)
    html_doc = render_page(data, entries, slug_index)

    out_path.write_text(html_doc, encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(
        f"wrote {out_path} "
        f"({len(entries)} entries, {size_kb:.1f} KB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
