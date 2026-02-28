#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

try:
    from playwright.async_api import (
        BrowserContext,
        Page,
        async_playwright,
    )
except ImportError:
    sys.exit(
        "Playwright not found.\n"
        "Install with:  pip install playwright && playwright install chromium"
    )

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    sys.exit("BeautifulSoup not found.\nInstall with:  pip install beautifulsoup4 lxml")

try:
    from ebooklib import epub
except ImportError:
    sys.exit("EbookLib not found.\nInstall with:  pip install ebooklib")


# ===========================================================================
# Configuration
# ===========================================================================

DEFAULT_YEAR = 2026

# Cover image: place cover.jpg (or .jpeg / .png) next to this script.
# The generator will use it automatically if found.
_SCRIPT_DIR = Path(__file__).resolve().parent
COVER_CANDIDATES = [
    _SCRIPT_DIR / "cover.jpg",
    _SCRIPT_DIR / "cover.jpeg",
    _SCRIPT_DIR / "cover.png",
]

OFFICES: List[Tuple[str, str]] = [
    ("morning-prayer", "Morning Prayer"),
    ("noonday-prayer", "Noonday Prayer"),
    ("evening-prayer", "Evening Prayer"),
    ("compline",       "Compline"),
]

# URL structure: /pray/{lang}/{rite}/{calendar}/{year}/{month}/{day}/{office}
BASE_URL = "https://www.venite.app/pray/en/Rite-II/bcp1979"

# Liturgy-specific preferences (passed as JSON via ?prefs= URL parameter).
# Numeric values select an option by 0-based index for "option"-type elements.
# String values set a specific option value for preference-type elements.
LITURGY_PREFS: Dict = {
    # Translations
    "bibleVersion":    "NRSV",
    "psalterVersion":  "bcp1979",
    # Cycles
    "psalmCycle":      "daily-office-psalter",   # Daily Office Lectionary
    "canticleTable":   "Rite-II",                 # 1979 Rite-II table
    "lectionary":      "daily-office",
    # Option selections (0-based index)
    "lords_prayer":    0,   # 0=Traditional, 1=Contemporary
    "suffrages_mp":    0,   # 0=Suffrages A, 1=Suffrages B (Morning Prayer)
    "suffrages_ep":    0,   # 0=Suffrages A, 1=Suffrages B (Evening Prayer)
    "confession_intro": 1,  # 0=Long form, 1=Short form
    "absolution":      1,   # 0=Priest, 1=Lay/Deacon
    "venite":          0,   # 0=Venite (BCP), 1=Jubilate, 2=Pascha Nostrum
    # Include collects for minor feasts
    "includeMinorFeasts": True,
}

# Display preferences stored in localStorage to suppress rubrics, verse
# numbers, and other display elements before the page renders.
LOCALSTORAGE_PREFS: Dict[str, str] = {
    "preference-rubrics":     "false",  # hide red rubric text
    "preference-bibleVerses": "false",  # hide verse numbers in readings
    "preference-psalmVerses": "false",  # hide psalm verse numbers
    "preference-dropcaps":    "none",
    "preference-response":    "bold",
    "preference-bolded":      "none",
    "preference-darkmode":    "light",
}

CACHE_DIR = Path("bcp_cache")

# How long (ms) to wait for the liturgy section to appear after navigation.
CONTENT_LOAD_TIMEOUT = 60_000  # 60 s – some pages compile slowly

# Concurrent browser tabs used to fetch the 4 offices of one day in parallel.
MAX_CONCURRENT_OFFICES = 4

# Extra pause (s) between each day when pages are being freshly fetched.
POLITE_DELAY = 1.5


# ===========================================================================
# Helpers
# ===========================================================================

def day_ordinal(d: date) -> str:
    """Return e.g. 'January 1, 2026' without zero-padded day."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def build_url(d: date, office_slug: str) -> str:
    """Assemble the venite.app URL with preferences encoded in the query string."""
    prefs_json = json.dumps(LITURGY_PREFS, separators=(",", ":"))
    prefs_enc  = urllib.parse.quote(prefs_json)
    return (
        f"{BASE_URL}/{d.year}/{d.month}/{d.day}/{office_slug}"
        f"?prefs={prefs_enc}"
    )


def cache_path(d: date, office_slug: str) -> Path:
    return CACHE_DIR / f"{d.isoformat()}-{office_slug}.html"


# ===========================================================================
# Playwright fetching
# ===========================================================================

async def configure_localstorage(context: BrowserContext) -> None:
    """
    Navigate to the site once so localStorage can be seeded with the display
    preferences we want before any liturgy page loads.
    """
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.venite.app",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        js = "; ".join(
            f'localStorage.setItem("{k}", "{v}")'
            for k, v in LOCALSTORAGE_PREFS.items()
        )
        await page.evaluate(js)
    except Exception as exc:
        print(f"  Warning: could not seed localStorage – {exc}")
    finally:
        await page.close()


async def fetch_office_html(
    context: BrowserContext,
    d: date,
    office_slug: str,
    office_name: str,
) -> Optional[str]:
    """
    Return the fully-rendered HTML for one office on one day.
    Uses an on-disk cache; re-fetches only if not cached.
    """
    cp = cache_path(d, office_slug)
    if cp.exists():
        return cp.read_text(encoding="utf-8")

    url  = build_url(d, office_slug)
    page = await context.new_page()
    html: Optional[str] = None

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=CONTENT_LOAD_TIMEOUT)

        # Wait for the compiled liturgy to appear in the DOM.
        # venite.app renders an Angular app; after navigation the content
        # may still be compiling asynchronously.
        try:
            await page.wait_for_selector(
                "section.liturgy, .liturgy, article.doc",
                timeout=CONTENT_LOAD_TIMEOUT,
            )
        except Exception:
            # Fall back to a timed wait if the selector never appears.
            await page.wait_for_timeout(8_000)

        # Inject LDF document data as HTML data-attributes so the Python
        # processing pipeline can access psalm numbers, section labels, etc.
        # without needing to read Angular/Stencil JS component internals.
        try:
            await page.evaluate("""() => {
                const tags = [
                    'ldf-heading', 'ldf-psalm', 'ldf-bible-reading',
                    'ldf-text', 'ldf-responsive-prayer'
                ];
                for (const tag of tags) {
                    for (const el of document.querySelectorAll(tag)) {
                        try {
                            const d = el.doc;
                            if (!d || typeof d !== 'object') continue;
                            // Heading label (section title text)
                            if (d.value && typeof d.value === 'string')
                                el.dataset.ldfLabel = d.value;
                            else if (d.label && typeof d.label === 'string')
                                el.dataset.ldfLabel = d.label;
                            if (d.type)  el.dataset.ldfType  = d.type;
                            if (d.style) el.dataset.ldfStyle = d.style;
                            // Psalm / canticle number
                            if (d.metadata && d.metadata.number != null)
                                el.dataset.ldfNumber = d.metadata.number;
                            // Bible reading citation (book, chapter, verse)
                            if (d.citation && typeof d.citation === 'string')
                                el.dataset.ldfCitation = d.citation;
                        } catch (e) { /* silently ignore */ }
                    }
                }
            }""")
        except Exception:
            pass  # JS injection is best-effort; pipeline has text-pattern fallback

        html = await page.content()

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(html, encoding="utf-8")

    except Exception as exc:
        print(f"\n  ✗ {office_name} {day_ordinal(d)}: {exc}")
    finally:
        await page.close()

    return html


# ===========================================================================
# HTML processing pipeline
# ===========================================================================

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_BCP_PAGE_RE   = re.compile(
    r"\bBCP\s+p+\.?\s*\d+(?:[-–]\d+)?\b", re.IGNORECASE
)
_PAGE_ONLY_RE  = re.compile(r"\bp{1,2}\.?\s*\d{2,4}\b")
_PARENS_NUM_RE = re.compile(r"\(\s*(?:p|pp)\.?\s*\d+\s*\)")

_REMOVE_SECTION_HEADINGS = [
    "a period of silence",
    "prayers and thanksgivings",
    "prayers and thanksgivings following the prayer for mission",
]

# Option picker labels that are purely preference/variant selectors and
# should be silently discarded (no heading created).
_PREF_OPTION_LABELS: set = {
    "Rite II", "Rite I", "EOW",
    "Lay/Deacon", "Priest",
    "Short", "Long",                    # confession intro form
    "BCP",                              # Phos Hilaron / invitatory variant label
    "Change Canticle",                  # canticle swap button
    "Prayers and Thanksgivings",
    "Daily Office Lectionary", "30-day Cycle",
    "1979 Table of Suggested Canticles", "Te Deum/Benedictus",
}

# Option picker labels that should become section headings with transformed
# text.  The value is (heading_tag, heading_text).
_LABEL_TRANSFORMS: Dict[str, tuple] = {
    "Venite (BCP)":   ("h3", "Venite"),
    "Jubilate (BCP)": ("h3", "Jubilate"),
    "Ps. 95 (BCP)":   ("h3", "Psalm 95"),
    "Suffrages A":    ("h3", "Suffrages"),
    "Suffrages B":    ("h3", "Suffrages"),
    "Traditional":    ("h3", "The Lord's Prayer"),
    "Contemporary":   ("h3", "The Lord's Prayer"),
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical single-line Gloria Patri text to replace split verse/halfverse
GLORIA_FULL = (
    "Glory to the Father, and to the Son, and to the Holy Spirit: "
    "as it was in the beginning, is now, and will be for ever. Amen."
)

# Preces response texts that should never be indented (case-insensitive match
# after stripping trailing punctuation).
_PRECES_INLINE_TEXTS: frozenset = frozenset({
    # Morning/Evening Prayer opening versicles
    "and our mouth shall proclaim your praise",
    # O God make speed (opening preces)
    "o lord, make haste to help us",
    # Suffrages
    "and let our cry come to you",
    "and also with you",
    "the maker of heaven and earth",
    # Kyrie eleison
    "lord, have mercy",
    "christ, have mercy",
    # Closing versicle
    "thanks be to god",
    # Compline — Into your hands
    "for you have redeemed me, o lord, o god of truth",
    # Compline — Keep us O Lord
    "hide us under the shadow of your wings",
})

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _normalise_text(tag_or_str) -> str:
    if isinstance(tag_or_str, str):
        return " ".join(tag_or_str.split()).lower()
    return " ".join(tag_or_str.get_text().split()).lower()


# ---------------------------------------------------------------------------
# 1a. Fix antiphons
#
# venite.app renders psalms/canticles that have antiphons as follows:
#   Inside ldf-psalm → psalm-parent.no-numbers:
#     ldf-liturgical-document.antiphon  (antiphon BEFORE the psalm)
#     div.psalm-set ×N  (each ends with a div.repeat-antiphon between stanzas)
#     ldf-liturgical-document.antiphon  (trailing copy — in DOM it sits BEFORE
#                                         the sibling gloria refrain)
#   Outside ldf-psalm, as sibling ldf-refrain elements:
#     ldf-refrain containing div.gloria    (Gloria Patri)
#     ldf-refrain containing div.antiphon  ×N  (post-gloria repeats)
#
# Desired output: Antiphon → psalm text → Gloria → Antiphon  (once each)
# ---------------------------------------------------------------------------

def _fix_antiphons(soup: BeautifulSoup) -> None:
    """
    Step 1 — inside invitatory ldf-psalm elements:
      • Remove every div.repeat-antiphon from psalm-set stanzas.
      • Remove every div.antiphon that is nested inside a psalm-set.
      • Remove the trailing ldf-liturgical-document.antiphon (keep only first).

    Step 2 — ldf-refrain sequences anywhere in the document:
      After each gloria ldf-refrain keep only the FIRST antiphon ldf-refrain;
      decompose all subsequent antiphon ldf-refrains (stanza-boundary repeats).
    """
    # ── Step 1: clean inside invitatory / canticle ldf-psalm ─────────────
    for psalm_el in soup.find_all("ldf-psalm"):
        psalm_parent = psalm_el.find(class_="psalm-parent")
        if not psalm_parent:
            continue
        if "no-numbers" not in (psalm_parent.get("class") or []):
            continue  # only invitatory psalms and canticles need this

        # Remove every div.repeat-antiphon inside any psalm-set stanza
        for rv in psalm_el.find_all("div", class_="repeat-antiphon"):
            rv.decompose()

        # Remove any div.antiphon that is inside a psalm-set stanza
        for div in psalm_el.find_all("div", class_="antiphon"):
            if div.find_parent(class_="psalm-set"):
                div.decompose()

        # Determine whether an external gloria refrain already follows this psalm.
        psalm_art_s1 = psalm_el.find_parent("article") or psalm_el
        next_sib = psalm_art_s1.find_next_sibling()
        while next_sib and isinstance(next_sib, NavigableString):
            next_sib = next_sib.find_next_sibling()
        has_external_gloria = bool(next_sib and next_sib.find("div", class_="gloria"))

        # Handle ldf-liturgical-document.antiphon elements inside psalm-parent.
        #
        # Stanza-boundary antiphons (appearing BEFORE the internal Gloria)
        # are repeats already handled by the psalm renderer and must be removed.
        # Closing antiphons (appearing AFTER the internal Gloria) are legitimate
        # — e.g. the "Guide us waking" repeat after the Nunc Dimittis — and
        # must be preserved.
        #
        # If there is no internal Gloria (because an external refrain will serve,
        # e.g. Phos Hilaron), all trailing antiphons are duplicates → remove all.
        ant_wrappers = psalm_parent.find_all(
            "ldf-liturgical-document", class_="antiphon"
        )
        if len(ant_wrappers) > 1:
            internal_gloria_ref = psalm_parent.find("div", class_="gloria")
            if internal_gloria_ref:
                all_desc = list(psalm_parent.descendants)
                try:
                    gloria_i = all_desc.index(internal_gloria_ref)
                except ValueError:
                    gloria_i = -1
                for aw in ant_wrappers[1:]:
                    try:
                        aw_i = all_desc.index(aw)
                    except ValueError:
                        continue
                    if gloria_i == -1 or aw_i <= gloria_i:
                        aw.decompose()   # before/at Gloria → stanza repeat → remove
                    # after Gloria → closing antiphon → keep
            else:
                # No internal Gloria: all trailing antiphons are duplicates
                for aw in ant_wrappers[1:]:
                    aw.decompose()

        # If an external gloria refrain already follows this psalm,
        # remove the internal div.gloria to avoid duplication (e.g. Phos Hilaron).
        if has_external_gloria:
            for internal_g in psalm_el.find_all("div", class_="gloria"):
                internal_g.decompose()

        # Add a closing antiphon repeat after the internal Gloria when the psalm
        # has an opening antiphon but no explicit closing antiphon wrapper.
        # This covers the Nunc Dimittis in Compline: venite.app renders only
        # the opening "Guide us waking" antiphon inside the psalm-parent but
        # expects the repeat to follow the Gloria Patri.
        internal_g = psalm_parent.find("div", class_="gloria")
        if internal_g and ant_wrappers:
            all_desc = list(psalm_parent.descendants)
            try:
                gloria_i = all_desc.index(internal_g)
            except ValueError:
                gloria_i = -1
            has_closing = False
            if gloria_i >= 0:
                for aw in ant_wrappers:
                    try:
                        if all_desc.index(aw) > gloria_i:
                            has_closing = True
                            break
                    except ValueError:
                        pass
            if not has_closing and gloria_i >= 0:
                ant_source = ant_wrappers[0]
                ant_div = ant_source.find("div", class_="antiphon")
                ant_p   = ant_source.find("p")
                ant_text = (
                    ant_div.get_text(strip=True) if ant_div
                    else ant_p.get_text(strip=True) if ant_p
                    else ""
                )
                if ant_text:
                    repeat_wrapper = soup.new_tag(
                        "ldf-liturgical-document", attrs={"class": "antiphon"}
                    )
                    repeat_div = soup.new_tag("div", attrs={"class": "antiphon"})
                    repeat_div.string = ant_text
                    repeat_wrapper.append(repeat_div)
                    internal_g.insert_after(repeat_wrapper)

    # ── Step 2a: move pre-psalm gloria article to AFTER the psalm ────────
    # venite.app sometimes places the gloria ldf-refrain article BEFORE the
    # invitatory ldf-psalm article in document order.  Detect this pattern
    # and reorder so that the sequence becomes:
    #   Antiphon (inside psalm) → Psalm text → Gloria → Antiphon repeat
    for psalm_el in soup.find_all("ldf-psalm"):
        psalm_parent_el = psalm_el.find(class_="psalm-parent")
        if not psalm_parent_el:
            continue
        if "no-numbers" not in (psalm_parent_el.get("class") or []):
            continue  # only invitatory psalms need this

        psalm_art = psalm_el.find_parent("article")
        if not psalm_art:
            continue

        # Look for an opening-doxology gloria in the preceding sibling articles.
        # In Lent the gloria sits immediately before the psalm.  Outside Lent
        # venite.app inserts a standalone "Alleluia." ldf-text article between
        # the gloria and the psalm, so we scan back up to 2 siblings.
        def _prev_sib(el):
            s = el.find_previous_sibling()
            while s and isinstance(s, NavigableString):
                s = s.find_previous_sibling()
            return s

        prev_art = _prev_sib(psalm_art)
        if not (prev_art and prev_art.find("div", class_="gloria")):
            prev_art = _prev_sib(prev_art) if prev_art else None
        if not (prev_art and prev_art.find("div", class_="gloria")):
            continue  # no out-of-order gloria found within 2 preceding siblings

        # Capture antiphon text from the first antiphon wrapper inside the psalm
        ant_wrapper = psalm_parent_el.find("ldf-liturgical-document", class_="antiphon")
        ant_div = ant_wrapper.find("div", class_="antiphon") if ant_wrapper else None
        ant_text = ant_div.get_text(strip=True) if ant_div else ""

        # Leave the original gloria article in place between the preces and the
        # psalm — it serves as the opening doxology after "And our mouth shall
        # proclaim your praise."  Create a fresh ending gloria article to appear
        # AFTER the psalm as the Venite's own closing Gloria Patri.
        ending_gloria_art = soup.new_tag("article", attrs={"class": "sc-ldf-liturgy"})
        ending_gloria_div = soup.new_tag("div", attrs={"class": "gloria"})
        ending_gloria_art.append(ending_gloria_div)
        psalm_art.insert_after(ending_gloria_art)

        # Remove any internal div.gloria from the psalm (now superseded by the
        # new ending gloria article directly above).
        for internal_g in psalm_el.find_all("div", class_="gloria"):
            internal_g.decompose()

        # Insert a post-gloria antiphon repeat article
        if ant_text:
            repeat_art = soup.new_tag("article", attrs={"class": "sc-ldf-liturgy"})
            repeat_div = soup.new_tag("div", attrs={"class": "antiphon"})
            repeat_div.string = ant_text
            repeat_art.append(repeat_div)
            ending_gloria_art.insert_after(repeat_art)

    # ── Step 2b: deduplicate post-gloria antiphon ldf-refrain elements ────
    all_refrains = soup.find_all("ldf-refrain")
    i = 0
    while i < len(all_refrains):
        if all_refrains[i].find("div", class_="gloria"):
            # Collect consecutive antiphon refrains that follow this gloria
            j = i + 1
            antiphon_refs = []
            while j < len(all_refrains) and not all_refrains[j].find(
                "div", class_="gloria"
            ):
                antiphon_refs.append(all_refrains[j])
                j += 1
            # Keep only the first antiphon refrain; decompose the rest
            for ar in antiphon_refs[1:]:
                ar.decompose()
            i = j
        else:
            i += 1


# ---------------------------------------------------------------------------
# 1b. Fix Gloria Patri — collapse split verse/halfverse into one sentence
# ---------------------------------------------------------------------------

def _fix_gloria(soup: BeautifulSoup) -> None:
    """
    Replace every div.gloria (which contains split div.verse / div.halfverse
    children) with a single <p class="gloria-text"> containing the full
    canonical Gloria Patri text.
    """
    for div in soup.find_all("div", class_="gloria"):
        p = soup.new_tag("p", attrs={"class": "gloria-text"})
        p.string = GLORIA_FULL
        div.replace_with(p)


# ---------------------------------------------------------------------------
# 1c. Fix preces indentation — remove indent from specific V/R pairs
#     The user expects these short call-and-response lines to sit flush
#     left, unlike longer responsive prayers that keep the 2em indent.
# ---------------------------------------------------------------------------

# Responses that get extra bottom-margin to separate from the next element.
# "And also with you" precedes "Let us pray" in MP/EP.
# "And let our cry come to you" precedes "Let us pray" in Noonday/Compline
# and ends the Suffrages A block in MP/EP.
# "For you have redeemed me" precedes "Keep us, O Lord" in Compline.
_PRECES_SECTION_END_TEXTS: frozenset = frozenset({
    "for you have redeemed me, o lord, o god of truth",
    "and also with you",
    "and let our cry come to you",
})


def _fix_preces_layout(soup: BeautifulSoup) -> None:
    """
    Change p.preces-response → p.preces-response-inline for any response
    whose text matches a known no-indent pair (Kyrie, versicles, etc.).
    Responses in _PRECES_SECTION_END_TEXTS also get preces-section-end for
    extra bottom-margin to visually separate the next liturgical block.
    """
    for p in soup.find_all("p", class_="preces-response"):
        # Normalise: strip trailing punctuation and lowercase
        text = re.sub(r"[.!?,;:]+$", "", p.get_text(strip=True)).lower()
        if text in _PRECES_INLINE_TEXTS:
            classes = ["preces-response-inline"]
            if text in _PRECES_SECTION_END_TEXTS:
                classes.append("preces-section-end")
            p["class"] = classes


# ---------------------------------------------------------------------------
# 1. Remove option-picker tab bars
#    venite.app wraps every selectable liturgical variant in <ldf-option>.
#    The tab strip (div.select-control > ldf-label-bar > ion-segment) shows
#    "Rite II | EOW", "Suffrages A | B | EOW", etc.
#    Before removing, we capture the selected tab label for option blocks
#    that represent collects/day-headings so those titles survive.
# ---------------------------------------------------------------------------

def _remove_option_selectors(soup: BeautifulSoup) -> None:
    """
    Remove the ion-segment tab navigation inside every ldf-option.

    Selected label handling:
      • In _LABEL_TRANSFORMS  → emit an <h3 class="section-heading"> with
                                the mapped text (Venite, Suffrages, etc.)
      • In _PREF_OPTION_LABELS → discard silently (Short/Long, Rite II, …)
      • Otherwise             → emit a <p class="collect-title"> (feast names,
                                collect headings, etc.)
    """
    for ctrl in soup.find_all("div", class_="select-control"):
        selected = ctrl.find(
            "ion-segment-button", attrs={"aria-selected": "true"}
        )
        if selected:
            label_el = selected.find("ion-label")
            label = label_el.get_text(strip=True) if label_el else selected.get_text(strip=True)
            if label:
                if label in _LABEL_TRANSFORMS:
                    tag, text = _LABEL_TRANSFORMS[label]
                    h = soup.new_tag(tag, attrs={"class": "section-heading"})
                    h.string = text
                    ctrl.insert_before(h)
                elif label not in _PREF_OPTION_LABELS:
                    p = soup.new_tag("p", attrs={"class": "collect-title"})
                    p.string = label
                    ctrl.insert_before(p)
        ctrl.decompose()

    # Remove any orphaned ldf-label-bar elements
    for el in soup.find_all("ldf-label-bar"):
        el.decompose()


# ---------------------------------------------------------------------------
# 2. Remove rubrics (red liturgical instruction text)
# ---------------------------------------------------------------------------

def _remove_rubrics(soup: BeautifulSoup) -> None:
    """Remove rubric instruction elements and inline rubric phrases."""
    for el in soup.find_all(class_="rubric"):
        el.decompose()


# ---------------------------------------------------------------------------
# 3. Remove meditation timer (ldf-meditation web component)
# ---------------------------------------------------------------------------

def _remove_meditation(soup: BeautifulSoup) -> None:
    """Remove the meditation timer component and any prayer-list UI."""
    for el in soup.find_all("ldf-meditation"):
        el.decompose()
    for el in soup.find_all(class_=["meditation", "prayer-list", "prayers-and-thanksgivings"]):
        el.decompose()
    # Text-based fallback for sections with matching heading text
    for h_tag in list(soup.find_all(re.compile(r"^h[1-6]$"))):
        if _normalise_text(h_tag) in _REMOVE_SECTION_HEADINGS:
            level = int(h_tag.name[1])
            parent = h_tag.parent
            if parent is None:
                continue
            siblings = list(parent.children)
            try:
                idx = siblings.index(h_tag)
            except ValueError:
                continue
            to_remove = [h_tag]
            for sib in siblings[idx + 1:]:
                if isinstance(sib, Tag) and sib.name in [f"h{l}" for l in range(1, level + 1)]:
                    break
                to_remove.append(sib)
            wrapper = h_tag.find_parent("article")
            if wrapper and wrapper != parent:
                to_remove = [wrapper]
            for el in to_remove:
                try:
                    el.decompose()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 4. Convert preces tables to clean paragraph layout
#    <table class="preces">
#      <tr class="row">
#        <td class="cell">Officiant / V.</td>      ← remove (label)
#        <td class="cell [response]">prayer text</td>  ← keep
#      </tr>
#    Replaced by <div class="preces-block"><p class="preces-v/r">…</p></div>
# ---------------------------------------------------------------------------

def _convert_preces_tables(soup: BeautifulSoup) -> None:
    """Flatten preces tables: remove label column, emit clean paragraphs."""
    for table in soup.find_all("table", class_="preces"):
        container = soup.new_tag("div", attrs={"class": "preces-block"})
        for row in table.find_all("tr", class_="row"):
            cells = row.find_all("td", class_="cell")
            if not cells:
                continue
            # Identify the text cell: prefer one with "response" class, else last
            text_cell = next(
                (c for c in cells if "response" in (c.get("class") or [])),
                cells[-1],
            )
            is_response = "response" in (text_cell.get("class") or [])
            p_class = "preces-response" if is_response else "preces-versicle"
            p = soup.new_tag("p", attrs={"class": p_class})
            for child in list(text_cell.children):
                p.append(child.__copy__() if hasattr(child, "__copy__") else child)
            container.append(p)
        table.replace_with(container)

    # Remove any remaining <em class="label"> role labels
    for label in soup.find_all("em", class_="label"):
        label.decompose()


# ---------------------------------------------------------------------------
# 5. Move scripture citations below their header / reading intro
# ---------------------------------------------------------------------------

def _move_citations_under_headers(soup: BeautifulSoup) -> None:
    """
    Handle scripture citations in two ways:

    Case A — citation inside an h-tag (collect / feast headings):
        Move to a standalone <p class="citation-ref"> immediately below the
        heading (unchanged behaviour).

    Case B — citation inside a <p>:
        • "A Reading from…" lesson heading → append "(citation)" inline on
          the same line (no separate element created).
        • All other short-reading paragraphs → append an italic
          <span class="inline-citation"> at the end of the same paragraph,
          matching the Gloria Patri style.
    """
    # Case A: citation inside a heading element
    for article in soup.find_all(class_="heading"):
        h_tag = article.find(re.compile(r"^h[1-6]$"))
        if not h_tag:
            continue
        citation = h_tag.find(class_="citation") or article.find(class_="citation")
        if not citation:
            continue
        cite_text = citation.get_text(strip=True)
        citation.decompose()
        for node in list(h_tag.children):
            if isinstance(node, NavigableString):
                cleaned = node.strip(" :–-,")
                if cleaned != str(node):
                    node.replace_with(cleaned)
        cite_p = soup.new_tag("p", attrs={"class": "citation-ref"})
        cite_p.string = cite_text
        h_tag.insert_after(cite_p)

    # Case B: citation inside a <p> — always rendered inline
    for p in list(soup.find_all("p")):
        if p.parent and p.parent.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            continue
        citation = p.find(class_="citation")
        if not citation:
            continue
        cite_text = citation.get_text(strip=True)
        citation.decompose()

        p_norm = " ".join(p.get_text().split()).lower()
        if p_norm.startswith("a reading from"):
            # Lesson heading: append citation in parentheses, normal weight
            p.append(f" ({cite_text})")
        else:
            # Short reading: append citation as italic span (gloria style)
            p.append(" ")
            cite_span = soup.new_tag("span", attrs={"class": "inline-citation"})
            cite_span.string = cite_text
            p.append(cite_span)


# ---------------------------------------------------------------------------
# 6. Remove asterisks (psalm mediant markers)
# ---------------------------------------------------------------------------

def _remove_asterisks(soup: BeautifulSoup) -> None:
    """Remove the mediant asterisk (*) used in BCP psalm pointing."""
    for text_node in soup.find_all(string=True):
        txt = str(text_node)
        if "*" in txt:
            text_node.replace_with(txt.replace(" *", "").replace("*", ""))


# ---------------------------------------------------------------------------
# 7. Remove verse numbers from Bible readings and psalms
# ---------------------------------------------------------------------------

def _remove_verse_numbers(soup: BeautifulSoup) -> None:
    """Remove superscript verse numbers from lessons."""
    for sup in list(soup.find_all("sup")):
        txt = sup.get_text(strip=True)
        if re.match(r"^\d+[a-z]?$", txt):
            sup.decompose()


# ---------------------------------------------------------------------------
# 8. Handle psalm / canticle headings
#    venite.app renders these via <h5 slot="additional"> inside ldf-psalm:
#      • Regular psalms (have <sup> verse numbers) → remove Latin subtitle
#      • Canticles       (no <sup>, h5 has text)   → convert to <h3>
#      • Venite/antiphon (no <sup>, h5 is empty)   → remove empty element
#
#    IMPORTANT: must run BEFORE _remove_verse_numbers so <sup> presence
#    can still distinguish psalms from canticles.
# ---------------------------------------------------------------------------

def _handle_psalm_headings(soup: BeautifulSoup) -> None:
    """
    For each h5[slot="additional"] inside an ldf-psalm:
      - Psalm with verse numbers  → discard (Latin subtitle not needed)
      - Canticle (no verse nums)  → promote to <h3 class="canticle-heading">
      - Empty heading             → discard
    Also check JS-injected data-ldf-number attributes on ldf-psalm elements
    to insert English psalm-number headings.
    """
    # ── Canticle / psalm heading from slot="additional" ──────────────────
    for h5 in list(soup.find_all("h5", slot="additional")):
        text = h5.get_text(strip=True)
        psalm_parent = h5.find_parent(class_="psalm-parent")

        if not text:
            h5.decompose()
            continue

        has_verse_numbers = bool(
            psalm_parent.find("sup") if psalm_parent else False
        )
        if has_verse_numbers:
            # Regular psalm → remove Latin subtitle
            h5.decompose()
        else:
            # Canticle → convert to h3 before the psalm-parent block
            h3 = soup.new_tag("h3", attrs={"class": "canticle-heading"})
            h3.string = text
            if psalm_parent:
                psalm_parent.insert_before(h3)
            else:
                h5.replace_with(h3)
            h5.decompose()

    # ── Psalm numbers from JS injection (data-ldf-number on ldf-psalm) ───
    seen_numbers: set = set()
    for psalm_el in soup.find_all("ldf-psalm"):
        num = psalm_el.get("data-ldf-number")
        if not num or num in seen_numbers:
            continue
        # Skip the Venite/invitatory psalm (already has a Venite heading
        # injected by _remove_option_selectors)
        psalm_parent = psalm_el.find(class_="psalm-parent")
        if psalm_parent and "no-numbers" in (psalm_parent.get("class") or []):
            # no-numbers ← invitatory psalms and canticles; canticles get
            # their heading from the h5 branch above, Venite from option picker
            continue
        seen_numbers.add(num)
        art = psalm_el.find_parent("article")
        target = art if art else psalm_el
        # Only insert if no section heading immediately precedes it
        prev = target.find_previous_sibling()
        while prev and isinstance(prev, NavigableString):
            prev = prev.find_previous_sibling()
        if prev and getattr(prev, "name", None) in ("h1", "h2", "h3", "h4"):
            continue
        h4 = soup.new_tag("h3", attrs={"class": "psalm-heading"})
        h4.string = f"Psalm {num}"
        target.insert_before(h4)

    # ── Belt-and-suspenders: remove any remaining latin-name elements ─────
    for el in soup.find_all(class_="latin-name"):
        el.decompose()


# ---------------------------------------------------------------------------
# 9. Fix spaces around small-caps divine-name spans
#    venite.app renders "LORD" (YHWH) as <span class="sc sc-ldf-string">Lord</span>
#    with no whitespace between the span and surrounding text.
# ---------------------------------------------------------------------------

def _fix_sc_span_spaces(soup: BeautifulSoup) -> None:
    """
    Ensure a space exists on both sides of <span class="sc"> elements, then
    unwrap the span entirely so "LORD" (YHWH) renders with no special styling.
    venite.app marks the divine name with class="sc sc-ldf-string", which our
    CSS previously styled as small-caps.  Removing the element guarantees the
    word matches surrounding psalm text regardless of reader or CSS cascade.
    """
    for span in soup.find_all("span", class_="sc"):
        # Ensure space BEFORE the span
        prev = span.previous_sibling
        if isinstance(prev, NavigableString) and prev and not str(prev)[-1].isspace():
            prev.replace_with(str(prev) + " ")

        # Ensure space AFTER the span — but never before punctuation
        nxt = span.next_sibling
        if isinstance(nxt, NavigableString) and nxt:
            first_char = str(nxt)[0]
            if not first_char.isspace() and first_char not in ',;:.!?)]':
                nxt.replace_with(" " + str(nxt))

        # Remove the span wrapper; its text content stays in place
        span.unwrap()


# ---------------------------------------------------------------------------
# 10. Remove BCP page references (without disturbing surrounding whitespace)
# ---------------------------------------------------------------------------

def _remove_bcp_page_refs(soup: BeautifulSoup) -> None:
    """
    Strip BCP page number references from text nodes.
    IMPORTANT: only replace the node when a reference was actually found;
    never call .strip() on the result, which would destroy the leading/
    trailing whitespace that adjacent elements depend on for word spacing.
    """
    for text_node in soup.find_all(string=True):
        txt = str(text_node)
        new = _BCP_PAGE_RE.sub("", txt)
        new = _PAGE_ONLY_RE.sub("", new)
        new = _PARENS_NUM_RE.sub("", new)
        if new != txt:
            # Collapse internal double-spaces that may result from the removal
            new = re.sub(r"[ \t]{2,}", " ", new)
            text_node.replace_with(new)


# ---------------------------------------------------------------------------
# 11. Append LDF citation to lesson headings
#     The citation (e.g. "Ezek 18:1–4, 25–32") is stored only in the LDF
#     document's `citation` property.  It is never rendered to the HTML by
#     venite.app, so we capture it via JS injection (data-ldf-citation on
#     every ldf-bible-reading element) and append it here.
# ---------------------------------------------------------------------------

def _extract_citation_from_ldf_string_ids(br_el: Tag) -> str:
    """
    Fallback citation extractor: parse ldf-string id attributes.

    venite.app assigns ids of the form  {chapter}-{book_abbrev}-{verse}
    to every ldf-string verse element (e.g. "39-Gen.-1", "2-Mark-12",
    "10-1 Cor.-5").  From the first and last ids we construct:
      • single-chapter:  "ch:v1–v2"      e.g. "39:1–23"
      • multi-chapter:   "ch1:v1—ch2:v2" e.g. "9:23—10:5"
    Returns "" when no parseable ids are found.
    """
    ids = [el.get("id", "") for el in br_el.find_all("ldf-string") if el.get("id")]
    if not ids:
        return ""

    def _parse(id_str: str):
        parts = id_str.split("-")
        if len(parts) < 3:
            return None
        try:
            return int(parts[0]), int(parts[-1])   # (chapter, verse)
        except ValueError:
            return None

    first = _parse(ids[0])
    last  = _parse(ids[-1])
    if not first:
        return ""
    ch1, v1 = first
    if last and last != first:
        ch2, v2 = last
        if ch1 == ch2:
            return f"{ch1}:{v1}–{v2}"
        return f"{ch1}:{v1}—{ch2}:{v2}"
    return f"{ch1}:{v1}"


def _add_lesson_citations(soup: BeautifulSoup) -> None:
    """
    For each ldf-bible-reading, find the rendered "A Reading from…" heading
    and append " (chapter:verse)" to it inline.

    Primary source: data-ldf-citation attribute written by the JS injection.
    Fallback: parse chapter:verse from ldf-string id attributes.
    """
    for br in soup.find_all("ldf-bible-reading"):
        citation = br.get("data-ldf-citation", "").strip()
        if not citation:
            citation = _extract_citation_from_ldf_string_ids(br)
        if not citation:
            continue
        # The heading sits inside: ldf-liturgical-document.sc-ldf-bible-reading
        #   → div.container.sc-ldf-liturgical-document
        ldf_doc = br.find("ldf-liturgical-document")
        if not ldf_doc:
            continue
        container = ldf_doc.find("div", class_="container")
        if not container:
            continue
        # Find the specific child element that holds "A Reading from…" so the
        # citation is appended inline to that line rather than after all
        # child elements of the container (which would place it below the text).
        heading_el = None
        for child in container.children:
            if hasattr(child, "get_text"):
                child_text = " ".join(child.get_text().split()).lower()
                if child_text.startswith("a reading from"):
                    heading_el = child
                    break
        if heading_el is not None:
            heading_el.append(f" ({citation})")


# ---------------------------------------------------------------------------
# 12. Remove empty elements left after processing
# ---------------------------------------------------------------------------

def _clean_empty_elements(soup: BeautifulSoup) -> None:
    """Remove elements that are empty after all other processing."""
    changed = True
    while changed:
        changed = False
        for el in soup.find_all(True):
            if (
                el.name not in ("br", "hr", "img", "link", "meta")
                and not el.get_text(strip=True)
                and not el.find(["br", "hr", "img"])
            ):
                el.decompose()
                changed = True


# ---------------------------------------------------------------------------
# 12. Inject liturgical section headings
#     venite.app stores section titles only in its JS component state; they
#     are not rendered to HTML.  This step inserts them two ways:
#       A) via data-ldf-label attributes written by the JS injection in
#          fetch_office_html (most accurate)
#       B) via text-content pattern matching as a reliable fallback
# ---------------------------------------------------------------------------

def _add_section_headings(soup: BeautifulSoup, office_name: str = "") -> None:
    """
    Insert h3/h4 section headings that venite.app does not render as HTML.
    Runs after _remove_verse_numbers so <sup> elements are gone, but before
    _clean_empty_elements so article wrappers are still intact.
    """

    def _norm(el) -> str:
        return " ".join(el.get_text().split()).lower()

    def _prev_is_heading(el) -> bool:
        """True if an h1-h4 immediately precedes el's outermost article."""
        art = el.find_parent("article") or el
        prev = art.find_previous_sibling()
        while prev and isinstance(prev, NavigableString):
            prev = prev.find_previous_sibling()
        return bool(prev and getattr(prev, "name", "") in ("h1","h2","h3","h4"))

    def _insert_h3_before(el, text, css="section-heading"):
        art = el.find_parent("article") or el
        h = soup.new_tag("h3", attrs={"class": css})
        h.string = text
        art.insert_before(h)

    def _insert_h4_before(el, text, css="lesson-heading"):
        art = el.find_parent("article") or el
        h = soup.new_tag("h3", attrs={"class": css})
        h.string = text
        art.insert_before(h)

    # ── A. Section titles from JS-injected data-ldf-label on ldf-heading ──
    # Map normalised label text → display text (keep only section titles)
    _ldf_heading_map = {
        "confession of sin":          "A Confession of Sin",
        "a confession of sin":        "A Confession of Sin",
        "invitatory and psalter":     "Invitatory and Psalter",
        "the invitatory and psalter": "Invitatory and Psalter",
        "the lessons":                "The Lessons",
        "lessons":                    "The Lessons",
        "the apostles' creed":        "The Apostles' Creed",
        "apostles' creed":            "The Apostles' Creed",
        "the prayers":                "The Prayers",
        "prayers":                    "The Prayers",
        "the lord's prayer":          "The Lord's Prayer",
        "lord's prayer":              "The Lord's Prayer",
        "suffrages":                  "Suffrages",
    }
    injected_sections: set = set()
    for ldf_h in soup.find_all("ldf-heading"):
        label = ldf_h.get("data-ldf-label", "").strip()
        norm  = label.lower()
        mapped = _ldf_heading_map.get(norm)
        if mapped and mapped not in injected_sections:
            h = soup.new_tag("h3", attrs={"class": "section-heading"})
            h.string = mapped
            ldf_h.insert_before(h)
            injected_sections.add(mapped)

    # ── B. Text-pattern fallback ──────────────────────────────────────────
    # Only inject headings for sections not already added via A above.

    # B1. Confession of Sin
    # Patterns cover MP/EP ("Most merciful God…"), Compline invitation
    # ("Let us confess our sins to God"), and Compline confession body
    # ("Almighty God, our heavenly Father: we have sinned…").
    if "A Confession of Sin" not in injected_sections:
        for el in soup.find_all(["p", "div"]):
            t = _norm(el)
            if ("most merciful god" in t
                    or "almighty and most merciful father" in t
                    or "i confess to almighty god" in t
                    or "let us confess our sins to god" in t):
                if not _prev_is_heading(el):
                    _insert_h3_before(el, "A Confession of Sin")
                break

    # B2. Invitatory and Psalter
    #     Morning Prayer: "Lord, open our lips" versicle.
    #     Evening Prayer: "O God, make speed to save us" opening versicle.
    if "Invitatory and Psalter" not in injected_sections:
        for preces in soup.find_all("div", class_="preces-block"):
            t = _norm(preces)
            if "open our lips" in t or (
                "make speed to save us" in t and office_name == "Evening Prayer"
            ):
                if not _prev_is_heading(preces):
                    _insert_h3_before(preces, "Invitatory and Psalter")
                break

    # B3. The Lessons (section header) + The First / Second Lesson
    # For Noonday Prayer and Compline the single short lesson is headed
    # "A Short Reading" (an h3 section heading, not a subsection h4).
    # Short readings don't have "A Reading from…" text; find any bible-reading div.
    # Full lessons in MP/EP always start with "A Reading from [book]", so filter those.
    _is_noonday_or_compline = office_name in ("Noonday Prayer", "Compline")
    if _is_noonday_or_compline:
        lesson_divs = soup.find_all("div", class_="bible-reading")
    else:
        lesson_divs = [
            d for d in soup.find_all("div", class_="bible-reading")
            if "a reading from" in _norm(d) or "a reading" in _norm(d)
        ]
    if lesson_divs:
        if _is_noonday_or_compline:
            if not _prev_is_heading(lesson_divs[0]):
                _insert_h3_before(lesson_divs[0], "A Short Reading")
        else:
            if "The Lessons" not in injected_sections and len(lesson_divs) > 1:
                _insert_h3_before(lesson_divs[0], "The Lessons")
            names = {1: "The First Lesson", 2: "The Second Lesson", 3: "The Third Lesson"}
            for i, br_div in enumerate(lesson_divs, start=1):
                label = names.get(i, f"Lesson {i}") if len(lesson_divs) > 1 else "The Lesson"
                if not _prev_is_heading(br_div):
                    _insert_h4_before(br_div, label)

    # B4. The Apostles' Creed
    if "The Apostles' Creed" not in injected_sections:
        for el in soup.find_all(["p", "div"]):
            t = _norm(el)
            if "i believe in god" in t and len(t) > 40:
                if not _prev_is_heading(el):
                    _insert_h3_before(el, "The Apostles' Creed")
                break

    # B5. The Prayers
    #     MP/EP: opened by "The Lord be with you".
    #     Compline: opened by the "Into your hands" responsory.
    #     Noonday: opened by the Kyrie (if present).
    if "The Prayers" not in injected_sections:
        found_prayers = False
        for preces in soup.find_all("div", class_="preces-block"):
            t = _norm(preces)
            if "lord be with you" in t:
                if not _prev_is_heading(preces):
                    _insert_h3_before(preces, "The Prayers")
                found_prayers = True
                break
        if not found_prayers and office_name == "Compline":
            # Compline: "The Prayers" begins with the "Into your hands" responsory
            for preces in soup.find_all("div", class_="preces-block"):
                t = _norm(preces)
                if "into your hands" in t and "commend my spirit" in t:
                    if not _prev_is_heading(preces):
                        _insert_h3_before(preces, "The Prayers")
                    found_prayers = True
                    break
        if not found_prayers and office_name == "Noonday Prayer":
            # Noonday: the Kyrie may be ldf-responsive-prayer.responsive rather than
            # a preces-block, so search any element for the Kyrie text.
            for el in soup.find_all(["div", "ldf-responsive-prayer"]):
                t = _norm(el)
                if "lord, have mercy" in t and "christ, have mercy" in t:
                    if not _prev_is_heading(el):
                        _insert_h3_before(el, "Kyrie")
                    found_prayers = True
                    break

    # B6. The Kyrie — Noonday Prayer and Compline only.
    #     Inserted as an h4 subsection within The Prayers.
    if _is_noonday_or_compline:
        for preces in soup.find_all("div", class_="preces-block"):
            t = _norm(preces)
            if "lord, have mercy" in t and "christ, have mercy" in t:
                if not _prev_is_heading(preces):
                    _insert_h4_before(preces, "Kyrie", css="lesson-heading")
                break

    # B7. Closing Prayer — Noonday Prayer only.
    #     The "Lord, hear our prayer" versicle pair that precedes the collect.
    if office_name == "Noonday Prayer":
        for preces in soup.find_all("div", class_="preces-block"):
            t = _norm(preces)
            if "lord, hear our prayer" in t and "let our cry come to you" in t:
                if not _prev_is_heading(preces):
                    _insert_h3_before(preces, "Closing Prayer")
                break

    # B8. Nunc Dimittis — Compline only.
    #     Insert heading before the opening antiphon ("Guide us waking") that
    #     precedes the canticle.
    if office_name == "Compline":
        for el in soup.find_all(string=re.compile(r"guide us waking", re.I)):
            art = el.find_parent("article") or el.parent
            if not _prev_is_heading(art):
                _insert_h3_before(art, "Nunc Dimittis")
            break

    # B9. Closing Prayer — Compline only.
    #     Insert heading immediately before "Let us bless the Lord."
    if office_name == "Compline":
        for preces in soup.find_all("div", class_="preces-block"):
            t = _norm(preces)
            if "let us bless the lord" in t:
                if not _prev_is_heading(preces):
                    _insert_h3_before(preces, "Closing Prayer")
                break
        else:
            # Fallback: search any element text
            for el in soup.find_all(string=re.compile(r"let us bless the lord", re.I)):
                art = el.find_parent("article") or el.parent
                if not _prev_is_heading(art):
                    _insert_h3_before(art, "Closing Prayer")
                break


# ---------------------------------------------------------------------------
# 13. Add structural <hr> separators at major liturgical transitions
# ---------------------------------------------------------------------------

def _add_structural_separators(soup: BeautifulSoup) -> None:
    """
    Insert <hr class="office-break"> between major liturgical sections that
    otherwise run together visually, and annotate specific paragraphs with
    helper CSS classes for fine-grained spacing.

    Handled transitions (all offices where applicable):
      • After confession → before absolution
      • After absolution → before "O God, make speed" (Compline)
      • Before "Our help is in the name of the Lord" (Compline)
      • Before "Let us bless the Lord" (closing)
      • "Thanks be to God" short-reading response → reading-response class
      • "Let us pray" paragraph → preces-letuspray class (extra top-margin)
    """

    def _norm(el) -> str:
        return " ".join(el.get_text().split()).lower()

    def _hr():
        return soup.new_tag("hr", attrs={"class": "office-break"})

    def _after_art(el):
        art = el.find_parent("article") or el
        art.insert_after(_hr())

    def _before_art(el):
        art = el.find_parent("article") or el
        art.insert_before(_hr())

    # -- 1. After confession article --
    for el in soup.find_all(["p", "div"]):
        t = _norm(el)
        if (("most merciful god" in t and "sinned against you" in t)
                or ("almighty god, our heavenly father" in t and "sinned against you" in t)):
            _after_art(el)
            break

    # -- 2. After absolution article (before "O God make speed" in Compline) --
    for el in soup.find_all(["p", "div"]):
        t = _norm(el)
        if "may the almighty god grant" in t and "forgiveness" in t:
            _after_art(el)
            break

    # -- 3. Before "Our help is in the name of the Lord" (Compline opening) --
    #    This preces exchange follows the opening blessing; hr marks the section break.
    for preces in soup.find_all("div", class_="preces-block"):
        t = _norm(preces)
        if "our help is in the name of the lord" in t and "maker of heaven" in t:
            _before_art(preces)
            break

    # -- 4. Before "Let us bless the Lord" (closing of every office) --
    # Search preces-blocks first (MP/EP/Compline); fall back to any element
    # for offices (Noonday) where the closing versicle is in ldf-responsive-prayer.
    # Skip the <hr> if a section heading already immediately precedes the article
    # (e.g. Compline's "Closing Prayer" h3 already marks the transition).
    def _before_art_unless_headed(el):
        art = el.find_parent("article") or el
        prev = art.find_previous_sibling()
        while prev and isinstance(prev, NavigableString):
            prev = prev.find_previous_sibling()
        if prev and getattr(prev, "name", "") in ("h1", "h2", "h3", "h4"):
            return  # heading already provides the visual break
        art.insert_before(_hr())

    _bless_found = False
    for preces in soup.find_all("div", class_="preces-block"):
        t = _norm(preces)
        if "let us bless the lord" in t:
            _before_art_unless_headed(preces)
            _bless_found = True
            break
    if not _bless_found:
        for el in soup.find_all(string=re.compile(r"let us bless the lord", re.I)):
            _before_art_unless_headed(el.parent)
            break

    # -- 5. "Thanks be to God" response inside short readings → reading-response class --
    for reading_div in soup.find_all("div", class_="bible-reading"):
        for span in reading_div.find_all("span", class_="response"):
            if "thanks be to god" in _norm(span):
                p = span.find_parent("p")
                if p:
                    cls = list(p.get("class") or [])
                    if "reading-response" not in cls:
                        p["class"] = cls + ["reading-response"]

    # -- 6. "Let us pray" versicle → preces-letuspray class (extra top-margin) --
    for p in soup.find_all("p"):
        t = re.sub(r"[.,!?;:]+$", "", _norm(p)).strip()
        if t == "let us pray":
            cls = list(p.get("class") or [])
            if "preces-letuspray" not in cls:
                p["class"] = cls + ["preces-letuspray"]


# ---------------------------------------------------------------------------
# 14. Add collect headings and separators
# ---------------------------------------------------------------------------

# Distinctive substrings (lowercase) for fixed named collects.
# Using `in` containment rather than prefix matching so minor wording
# variations (punctuation, capitalisation) in venite.app don't break the match.
_FIXED_COLLECT_FINGERPRINTS: List[Tuple[str, str]] = [
    # Morning Prayer: "A Collect for Guidance"
    # venite.app Rite II: "Heavenly Father, in you we live and move…"
    ("heavenly father, in you we live and move and have our being",
     "A Collect for Guidance"),
    # Morning Prayer: "A Prayer for Mission" (standard form)
    ("almighty and everlasting god, by whose spirit the whole body of your faithful",
     "A Prayer for Mission"),
    # Morning Prayer: "A Prayer for Mission" (Lenten/alternate form)
    ("lord jesus christ, you stretched out your arms of love on the hard wood of the cross",
     "A Prayer for Mission"),
    # Evening Prayer: "A Collect for Peace"
    ("o god, you manifest in your servants the signs of your presence",
     "A Collect for Peace"),
    ("most holy god, the source of all good desires",
     "A Collect for Peace"),
    # Evening Prayer: "A Collect for Aid Against Perils" (multiple BCP variants)
    ("keep watch, dear lord, with those who work, or watch, or weep",
     "A Collect for Aid Against Perils"),
    ("lighten our darkness, we pray, o lord",
     "A Collect for Aid Against Perils"),
    ("o god, the life of all who live, the light of the faithful",
     "A Collect for Aid Against Perils"),
    # Evening Prayer: "A Prayer for Mission"
    ("o god and father of all, whom the whole heavens adore",
     "A Prayer for Mission"),
    # Compline collects (aid against perils — multiple BCP variants)
    ("be our light in the darkness, o lord, and in your great mercy",
     "A Collect for Aid Against Perils"),
    ("visit this place, o lord, and drive far from it all snares of the enemy",
     "A Collect for Aid Against Perils"),
    ("be present, o merciful god, and protect us through the hours of this night",
     "A Collect for Aid Against Perils"),
    ("look down, o lord, from your heavenly throne, and illumine this night",
     "A Collect for Aid Against Perils"),
    # Shared (MP + EP): "The General Thanksgiving"
    ("accept, o lord, our thanks and praise for all that you have done for us",
     "The General Thanksgiving"),
    # venite.app may have no space after comma: "Father of all mercies,we your unworthy servants"
    ("father of all mercies",
     "The General Thanksgiving"),
    # Belt-and-suspenders: "unworthy servants give you humble thanks" is unique
    ("unworthy servants give you humble thanks",
     "The General Thanksgiving"),
    # Shared: "A Prayer of St. Chrysostom"
    ("almighty god, you have given us grace at this time with one accord to make",
     "A Prayer of St. Chrysostom"),
]


def _add_collect_structure(soup: BeautifulSoup, office_name: str = "") -> None:
    """
    Add h4 collect headings and hr separators between named collects.

    Strategy:
      1. Label fixed collects (Guidance, Mission, Thanksgiving, Chrysostom …)
         by matching text fingerprints against ldf-text elements.
      2. Label day-specific collects using data-ldf-label from JS injection.
         "Collect of the Day" labels are normalised to "The Collect(s) of the Day".
      3. For Noonday / Compline, add an office-specific h4 heading before the
         first unlabelled ldf-text article that follows a "Let us pray" block.
         Compline receives a heading only on the first collect.
      4. Insert <hr class="collect-break"> between consecutive collect h4s.
    """

    def _norm(el) -> str:
        return " ".join(el.get_text().split()).lower()

    def _prev_heading(el) -> Optional[Tag]:
        """Return the immediately preceding h3/h4, skipping whitespace nodes."""
        art = el.find_parent("article") or el
        sib = art.find_previous_sibling()
        while sib and isinstance(sib, NavigableString):
            sib = sib.find_previous_sibling()
        return sib if (sib and getattr(sib, "name", "") in ("h3", "h4")) else None

    labeled: set = set()    # ids of already-labelled article elements

    def _label_art(art, heading_text: str) -> None:
        if id(art) in labeled:
            return
        labeled.add(id(art))
        if not _prev_heading(art.find("ldf-text") or art):
            h4 = soup.new_tag("h3", attrs={"class": "collect-heading"})
            h4.string = heading_text
            art.insert_before(h4)

    # ── 1. Fixed collects by text fingerprint ─────────────────────────────
    for fingerprint, label in _FIXED_COLLECT_FINGERPRINTS:
        for ldf in soup.find_all("ldf-text"):
            t = _norm(ldf)
            if fingerprint in t:
                art = ldf.find_parent("article") or ldf
                _label_art(art, label)
                break

    # ── 2. Day-specific collects via data-ldf-label (JS injection) ────────
    for ldf in soup.find_all("ldf-text"):
        raw_label = ldf.get("data-ldf-label", "").strip()
        if not raw_label:
            continue
        ll = raw_label.lower()
        if any(k in ll for k in ("collect", "prayer for", "thanksgiving")):
            art = ldf.find_parent("article") or ldf
            display_label = (
                "The Collect(s) of the Day" if "collect of the day" in ll
                else raw_label
            )
            _label_art(art, display_label)

    # ── 3. Noonday / Compline: office-specific heading after "Let us pray" ─
    # Walk forward from each "Let us pray" preces block and label unlabelled
    # ldf-text articles.  Compline gets a heading only on the first collect.
    if office_name == "Compline":
        collect_label = "The Collects for Compline"
    elif office_name == "Noonday Prayer":
        collect_label = "The Collect of Noonday Prayer"
    else:
        collect_label = "Collect"

    for preces_div in soup.find_all("div", class_="preces-block"):
        if "let us pray" not in _norm(preces_div):
            continue
        preces_art = preces_div.find_parent("article") or preces_div
        sib = preces_art
        for _ in range(3):          # label up to 3 following collect articles
            sib = sib.find_next_sibling("article")  # skip headings/hrs
            if not sib:
                break
            if not sib.find("ldf-text"):
                break               # hit a non-ldf-text element; stop
            if sib.find("ldf-psalm") or sib.find("div", class_="preces-block"):
                break               # hit psalms or another preces block; stop
            if "our father" in _norm(sib):
                break               # The Lord's Prayer, not a collect
            _label_art(sib, collect_label)
            if office_name == "Compline":
                break               # heading on first collect only

    # ── 4. hr separator between consecutive collect headings ──────────────
    for h4 in soup.find_all("h3", class_="collect-heading"):
        prev = h4.find_previous_sibling()
        while prev and isinstance(prev, NavigableString):
            prev = prev.find_previous_sibling()
        # If previous sibling is an article (a collect body), add separator
        if prev and getattr(prev, "name", "") == "article":
            hr = soup.new_tag("hr", attrs={"class": "collect-break"})
            h4.insert_before(hr)

    # ── 5. Compline: override any fingerprint-derived heading with generic label ──
    # Steps 1–2 may label individual Compline collects by name (e.g.
    # "A Collect for Aid Against Perils") before Step 3 can apply the generic
    # label.  Rename every collect heading in Compline to the office label.
    if office_name == "Compline":
        for h in soup.find_all("h3", class_="collect-heading"):
            h.string = collect_label


# ---------------------------------------------------------------------------
# 15. Filter Compline to a single collect
# ---------------------------------------------------------------------------

_COMPLINE_KEEP_COLLECT = "keep watch, dear lord, with those who work, or watch, or weep"

def _filter_compline_collect(soup: BeautifulSoup) -> None:
    """
    Compline provides several interchangeable collects; retain only
    "Keep watch, dear Lord…" and remove any others that venite.app rendered.

    Strategy: find every ldf-text article that follows a "Let us pray" preces
    block; keep the first one whose text contains the desired fingerprint,
    and decompose the rest.
    """
    def _norm(el) -> str:
        return " ".join(el.get_text().split()).lower()

    for preces_div in soup.find_all("div", class_="preces-block"):
        if "let us pray" not in _norm(preces_div):
            continue
        preces_art = preces_div.find_parent("article") or preces_div

        # Collect all consecutive ldf-text articles following "Let us pray"
        collect_arts = []
        sib = preces_art
        while True:
            sib = sib.find_next_sibling("article")
            if not sib or not sib.find("ldf-text"):
                break
            if sib.find("ldf-psalm") or sib.find("div", class_="preces-block"):
                break
            if "our father" in _norm(sib):
                break
            collect_arts.append(sib)

        if not collect_arts:
            continue

        # Identify which article contains the desired collect
        keep = None
        for art in collect_arts:
            if _COMPLINE_KEEP_COLLECT in _norm(art):
                keep = art
                break

        # If the desired collect is present, remove all others
        if keep is not None:
            for art in collect_arts:
                if art is not keep:
                    art.decompose()


# ---------------------------------------------------------------------------
# Main HTML processing pipeline
# ---------------------------------------------------------------------------

def process_office_html(
    raw_html: str,
    d: date,
    office_name: str,
    debug: bool = False,
) -> str:
    """
    Parse and format raw HTML from venite.app into clean XHTML for the EPUB.
    Returns an HTML fragment string (no <html>/<body> wrappers).
    """
    soup = BeautifulSoup(raw_html, "lxml")

    # Locate the compiled liturgy section
    liturgy: Optional[Tag] = (
        soup.find("section", class_="liturgy")
        or soup.find(class_=re.compile(r"\bliturgy\b"))
        or soup.find("ion-content")
        or soup.find("main")
    )

    if liturgy is None:
        if debug:
            (CACHE_DIR / f"debug_{d.isoformat()}_{office_name.replace(' ', '_')}.html"
             ).write_text(raw_html, encoding="utf-8")
        return (
            f'<p class="error"><em>Content unavailable for '
            f'{office_name} on {day_ordinal(d)}</em></p>'
        )

    working = BeautifulSoup(str(liturgy), "lxml")

    # ── Processing pipeline ──────────────────────────────────────────────
    _remove_option_selectors(working)    # tab bars → h3/p headings or removed
    _remove_rubrics(working)             # red instruction text
    _remove_meditation(working)          # meditation timer + P&T section
    _convert_preces_tables(working)      # label | text → clean paragraphs
    _fix_antiphons(working)              # antiphon only before psalm + after Gloria
    _fix_gloria(working)                 # collapse split Gloria Patri to one string
    _fix_preces_layout(working)          # remove indent from short V/R pairs
    _move_citations_under_headers(working)
    _remove_asterisks(working)           # psalm mediant *
    _handle_psalm_headings(working)      # canticle h5 → h3; psalm sub → removed
                                         # MUST run before _remove_verse_numbers
    _remove_verse_numbers(working)       # <sup> verse numbers in lessons
    _add_lesson_citations(working)       # append (Book chapter:verse) to lesson headings
    _add_section_headings(working, office_name)  # inject missing liturgical headings
    _fix_sc_span_spaces(working)         # spaces around "Lord"/"God" spans
    _remove_bcp_page_refs(working)       # BCP p. XX references
    _add_structural_separators(working)  # <hr> at major section transitions
    _add_collect_structure(working, office_name)  # h4 headings + separators for collects
    if office_name == "Compline":
        _filter_compline_collect(working)    # keep only "Keep watch, dear Lord…"
    _clean_empty_elements(working)

    body = working.find("body")
    if body:
        return "".join(str(c) for c in body.children)
    return str(working)


# ===========================================================================
# EPUB assembly
# ===========================================================================

EPUB_CSS = """\
@charset "UTF-8";

/* ═══════════════════════════════════════════════════════════════════════
   BCP Daily Office — EPUB stylesheet
   ═══════════════════════════════════════════════════════════════════════ */

/* ── Body ── */
body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1em;
    line-height: 1.7;
    margin: 0;
    padding: 0.8em 1em;
    color: #000;
}

/* ── Day heading (chapter title) ── */
h1.day-header {
    font-size: 1.5em;
    text-align: center;
    margin: 0.8em 0 0.6em;
    padding-bottom: 0.3em;
    border-bottom: 2px solid #333;
}

/* ── Office heading (Morning Prayer, Evening Prayer …) ──
   Page break before each office so offices open on a fresh screen.   */
h2.office-heading {
    font-size: 1.2em;
    font-variant: small-caps;
    letter-spacing: 0.05em;
    text-align: center;
    margin: 0;
    padding: 0.5em 0;
    border-top: 1px solid #555;
    border-bottom: 1px solid #555;
    page-break-before: always;
}
/* Keep the day heading on the same page as the first office heading */
h1.day-header + .office-section h2.office-heading {
    page-break-before: avoid;
}

/* ── All section headings — unified style ── */
h3 {
    font-size: 1em;
    font-variant: small-caps;
    letter-spacing: 0.04em;
    font-weight: bold;
    font-style: normal;
    margin: 2em 0 0.4em;
    padding-bottom: 0.15em;
    border-bottom: 1px dotted #aaa;
}

h5, h6 {
    font-size: 0.92em;
    font-style: italic;
    font-weight: normal;
    margin: 0.8em 0 0.2em;
}

/* ── Citation moved under header ── */
p.citation-ref {
    font-style: italic;
    font-size: 0.87em;
    margin: -0.2em 0 0.9em 0.4em;
    color: #444;
}

/* ── Inline scripture citation (short readings, same style as Gloria Patri) ── */
span.inline-citation {
    font-style: italic;
}

/* ── General text ── */
p {
    margin: 0.4em 0;
    text-indent: 0;
}

/* ── Collect / feast-day titles ── */
p.collect-title {
    font-weight: bold;
    font-size: 0.92em;
    margin: 1.5em 0 0.3em;
}

/* ── Article wrapper — breathing room between every liturgical unit ── */
article.sc-ldf-liturgy {
    margin-top: 1em;
}

/* ── Unison / prayer text ── */
.text-body {
    margin: 0.3em 0;
}

/* ── Responsive prayers (preces / suffrages) ── */
div.preces-block {
    margin: 0.5em 0 0.8em;
}
p.preces-versicle {
    margin: 0;
    padding-left: 0;
}
p.preces-response {
    margin: 0;
    padding-left: 0;
}
/* Short call-and-response pairs (Kyrie, versicles) — no indent */
p.preces-response-inline {
    margin: 0;
    padding-left: 0;
}
/* Extra spacing after "For you have redeemed me…" before "Keep us, O Lord…" */
p.preces-section-end {
    margin-bottom: 1.4em;
}

/* ── Response / Amen ── */
span.response {
    display: inline;
}

/* ── Psalm structure ── */
div.psalm-parent {
    margin-top: 0.6em;
    margin-bottom: 1.2em;
}
p.psalm {
    margin: 0;
    padding: 0;
}
div.text {
    margin: 0;
    padding: 0;
}
div.verse {
    display: block;
    margin: 0;
}
div.halfverse {
    display: block;
    padding-left: 2em;
    margin: 0;
}
br.sc-ldf-psalm {
    display: none;
}

/* ── Antiphon ── */
div.antiphon {
    font-style: italic;
    margin-bottom: 1em;
}
div.repeat-antiphon {
    font-style: italic;
    margin-top: 1em;
    margin-bottom: 1em;
}

/* ── Gloria Patri / refrain ── */
div.gloria,
p.gloria,
div.sc-ldf-refrain {
    margin-top: 1em;
    margin-bottom: 1.2em;
    font-style: italic;
}
p.gloria-text {
    margin-top: 1em;
    margin-bottom: 1.2em;
}

/* ── Bible reading ── */
div.bible-reading {
    margin: 0.4em 0 1em;
}
div.bible-reading p {
    margin: 0.4em 0;
}
sup {
    display: none;
}


/* ── Office section wrapper ── */
div.office-section {
    margin-bottom: 2em;
}

/* ── TOC page ── */
nav#toc ol {
    list-style: none;
    padding-left: 0;
}
nav#toc li {
    padding: 0.15em 0;
}
nav#toc li.month-section {
    font-weight: bold;
    margin-top: 0.8em;
}
nav#toc li.day-entry {
    padding-left: 1em;
}

/* ── Structural separator between major liturgical sections ── */
hr.office-break {
    border: none;
    margin: 1.2em 0;
}

/* ── Separator between consecutive named collects ── */
hr.collect-break {
    border: none;
    margin: 0.8em 0;
}


/* ── "Thanks be to God" response after a short reading ── */
p.reading-response {
    margin-top: 1em;
}

/* ── "Let us pray" versicle — extra top spacing before the collects ── */
p.preces-letuspray {
    margin-top: 1.4em;
}

/* ── Errors ── */
p.error { color: #800; }
"""


def _make_day_xhtml(d: date, offices: Dict[str, str]) -> str:
    """
    Build the complete XHTML source for one calendar day's chapter.

    `offices` maps office slug → processed HTML string.
    """
    title    = day_ordinal(d)
    sections = []

    for slug, name in OFFICES:
        content = offices.get(slug, "")
        section = (
            f'<div class="office-section">\n'
            f'<h2 class="office-heading">{name}</h2>\n'
        )
        if content:
            section += content
        else:
            section += '<p class="error"><em>Content unavailable</em></p>\n'
        section += "</div>\n"
        sections.append(section)

    body_content = "\n".join(sections)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
        "<head>\n"
        f'  <title>{title}</title>\n'
        '  <link rel="stylesheet" type="text/css" href="../style/main.css"/>\n'
        "</head>\n"
        "<body>\n"
        f'<h1 class="day-header" id="top">{title}</h1>\n'
        f"{body_content}"
        "</body>\n"
        "</html>\n"
    )


def _make_title_xhtml(title_label: str) -> str:
    """
    Build the title-page XHTML.

    title_label – e.g. '2026', 'March 2026', 'March 15, 2026'
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
        "<head>\n"
        f'  <title>Daily Office: {title_label}</title>\n'
        '  <link rel="stylesheet" type="text/css" href="../style/main.css"/>\n'
        "</head>\n"
        "<body>\n"
        '<div style="text-align:center; margin-top:3em;">\n'
        f'<h1>The Book of Common Prayer<br/>Daily Office</h1>\n'
        f'<h2>{title_label}</h2>\n'
        "<p><em>The Episcopal Church</em></p>\n"
        "<p><em>Rite II · NRSV · 1979 BCP Psalter</em></p>\n"
        "<hr/>\n"
        "<p><em>Morning Prayer · Noonday Prayer · Evening Prayer · Compline</em></p>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def build_epub(
    title_label: str,
    identifier_suffix: str,
    chapters: List[Tuple[date, epub.EpubHtml]],
    css_item: epub.EpubItem,
    cover_bytes: Optional[bytes] = None,
    cover_media_type: str = "image/jpeg",
) -> epub.EpubBook:
    """
    Assemble the final EPUB book object.

    title_label      – human-readable scope string, e.g. '2026', 'March 2026',
                       or 'March 15, 2026'
    identifier_suffix – used in the book's unique ID, e.g. '2026',
                       '2026-03', '2026-03-15'
    cover_bytes      – raw image bytes for the cover, or None to omit
    cover_media_type – MIME type of the cover image ('image/jpeg' or 'image/png')
    """
    full_title = f"Daily Office: {title_label}"

    book = epub.EpubBook()
    book.set_identifier(f"bcp-daily-office-{identifier_suffix}")
    book.set_title(full_title)
    book.set_language("en")
    book.add_author("The Episcopal Church")
    book.add_metadata(
        "DC",
        "description",
        (
            f"Pre-formatted Daily Offices (Morning Prayer, Noonday Prayer, "
            f"Evening Prayer, and Compline) from the 1979 Book of Common Prayer "
            f"for {title_label}.  Rite II, NRSV, 1979 BCP Psalter."
        ),
    )
    book.add_metadata("DC", "subject", "Religion; Prayer; Anglican; Episcopal")

    book.add_item(css_item)

    # ── Cover image and cover page ───────────────────────────────────────
    # Use ebooklib's dedicated EpubCover / EpubCoverHtml types so that EPUB
    # readers correctly identify the cover in the OPF manifest and guide.
    cover_page_item: Optional[epub.EpubHtml] = None
    if cover_bytes:
        ext = "png" if cover_media_type == "image/png" else "jpg"
        cover_img_filename = f"images/cover.{ext}"

        # EpubCover sets properties="cover-image" in the OPF manifest
        cover_img = epub.EpubCover(
            uid="cover-image",
            file_name=cover_img_filename,
        )
        cover_img.media_type = cover_media_type
        cover_img.content = cover_bytes
        book.add_item(cover_img)

        # <meta name="cover"> for EPUB 2 / older readers
        book.add_metadata(
            None, "meta", "", {"name": "cover", "content": "cover-image"}
        )

        # Full-bleed cover page using EpubCoverHtml
        cover_page_item = epub.EpubCoverHtml(
            title="Cover",
            file_name="cover.xhtml",
            image_name=cover_img_filename,
        )
        book.add_item(cover_page_item)

    # ── Title page ───────────────────────────────────────────────────────
    title_page = epub.EpubHtml(
        title=full_title,
        file_name="title.xhtml",
        lang="en",
    )
    title_page.content = _make_title_xhtml(title_label).encode("utf-8")
    title_page.add_item(css_item)
    book.add_item(title_page)

    # Add all day chapters
    for _, chapter in chapters:
        book.add_item(chapter)

    # ── TOC grouped by month ─────────────────────────────────────────────
    monthly_groups: Dict[str, List[epub.EpubHtml]] = {}
    for d, chapter in chapters:
        month_key = d.strftime("%B %Y")
        monthly_groups.setdefault(month_key, []).append(chapter)

    toc: list = []
    for month_name, month_chapters in monthly_groups.items():
        # Use epub.Link with "#top" fragment so every reader navigates to the
        # h1.day-header (id="top") rather than a remembered scroll position or
        # the first substantive content element (which would be Morning Prayer).
        links = tuple(
            epub.Link(
                href=ch.file_name + "#top",
                title=ch.title,
                uid=ch.id or ch.file_name,
            )
            for ch in month_chapters
        )
        toc.append((epub.Section(month_name), links))

    book.toc = toc

    # Navigation documents
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Spine: cover (if present) → nav → title → day chapters
    spine_front = []
    if cover_page_item:
        spine_front.append(cover_page_item)
    book.spine = spine_front + ["nav", title_page] + [ch for _, ch in chapters]

    return book


# ===========================================================================
# Main
# ===========================================================================

async def run(
    days: List[date],
    title_label: str,
    identifier_suffix: str,
    output_file: str,
    debug: bool,
) -> None:
    """
    Core generator.

    days             – ordered list of calendar dates to include
    title_label      – human-readable scope label for title page / metadata
    identifier_suffix – slug used in EPUB identifier and output filename
    output_file      – path of the .epub file to write
    debug            – if True, dump raw HTML for pages with missing selectors
    """
    total_pages = len(days) * len(OFFICES)

    cover_found = next((c for c in COVER_CANDIDATES if c.exists()), None)

    print(f"\nBCP Daily Office eBook Generator")
    print(f"══════════════════════════════════════════")
    print(f"Scope:    {title_label}")
    print(f"Days:     {len(days)}")
    print(f"Offices:  {len(OFFICES)} per day  ({', '.join(n for _, n in OFFICES)})")
    print(f"Pages:    {total_pages} total")
    print(f"Cover:    {cover_found.name if cover_found else '(none — place cover.jpg next to script)'}")
    print(f"Cache:    {CACHE_DIR.resolve()}")
    print(f"Output:   {output_file}")
    print()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Count already-cached pages
    cached_count = sum(
        1 for d in days for slug, _ in OFFICES if cache_path(d, slug).exists()
    )
    to_fetch = total_pages - cached_count
    print(f"Cached:   {cached_count}/{total_pages} pages")
    if to_fetch > 0:
        print(
            f"Fetching: {to_fetch} pages  "
            f"(~{to_fetch * 15 // 60} min estimated, may vary)"
        )
    print()

    # ------------------------------------------------------------------
    # Prepare EPUB skeleton
    # ------------------------------------------------------------------
    css_item = epub.EpubItem(
        uid="style-main",
        file_name="style/main.css",
        media_type="text/css",
        content=EPUB_CSS.encode("utf-8"),
    )

    chapters: List[Tuple[date, epub.EpubHtml]] = []

    # ------------------------------------------------------------------
    # Playwright browser session
    # ------------------------------------------------------------------
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        # Seed localStorage display preferences
        print("Configuring browser preferences…")
        await configure_localstorage(context)
        print("Done.\n")

        for i, d in enumerate(days):
            label = day_ordinal(d)
            already_cached = all(
                cache_path(d, slug).exists() for slug, _ in OFFICES
            )

            print(
                f"[{i + 1:3d}/{len(days)}] {label}  ",
                end="",
                flush=True,
            )

            # ---- Fetch all 4 offices concurrently ----
            fetch_tasks = [
                fetch_office_html(context, d, slug, name)
                for slug, name in OFFICES
            ]
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # ---- Process HTML ----
            office_content: Dict[str, str] = {}
            for (slug, name), result in zip(OFFICES, results):
                if isinstance(result, Exception):
                    print(f"\n  Error {name}: {result}")
                    office_content[slug] = ""
                elif result:
                    office_content[slug] = process_office_html(
                        result, d, name, debug=debug
                    )
                else:
                    office_content[slug] = ""

            # ---- Build chapter ----
            xhtml    = _make_day_xhtml(d, office_content)
            filename = f"day_{d.strftime('%Y_%m_%d')}.xhtml"
            chapter  = epub.EpubHtml(
                title    = label,
                file_name= filename,
                lang     = "en",
            )
            chapter.content = xhtml.encode("utf-8")
            chapter.add_item(css_item)
            chapters.append((d, chapter))

            print("✓")

            # Polite delay only when actually fetching (not from cache)
            if not already_cached:
                await asyncio.sleep(POLITE_DELAY)

        await browser.close()

    # ------------------------------------------------------------------
    # Compile and write EPUB
    # ------------------------------------------------------------------
    # Look for a cover image placed next to this script
    cover_bytes: Optional[bytes] = None
    cover_media_type = "image/jpeg"
    for candidate in COVER_CANDIDATES:
        if candidate.exists():
            cover_bytes = candidate.read_bytes()
            cover_media_type = (
                "image/png" if candidate.suffix.lower() == ".png" else "image/jpeg"
            )
            print(f"\nCover:    {candidate.name}")
            break

    print(f"\nAssembling EPUB ({len(chapters)} chapters)…")
    book = build_epub(
        title_label, identifier_suffix, chapters, css_item,
        cover_bytes, cover_media_type,
    )
    epub.write_epub(output_file, book)
    size_mb = os.path.getsize(output_file) / 1_048_576
    print(f"Saved: {output_file}  ({size_mb:.1f} MB)")

    # ------------------------------------------------------------------
    # Clean up cache files for the days that were generated
    # ------------------------------------------------------------------
    removed = 0
    for d in days:
        for slug, _ in OFFICES:
            cp = cache_path(d, slug)
            if cp.exists():
                cp.unlink()
                removed += 1
    if removed:
        print(f"Cache: removed {removed} file(s) from {CACHE_DIR}")
        # Remove the cache directory itself if it is now empty
        try:
            CACHE_DIR.rmdir()
        except OSError:
            pass  # not empty (files from other runs remain)

    print("\nDone!")


# ---------------------------------------------------------------------------
# Date-range helpers used by main()
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "january": 1,  "jan": 1,
    "february": 2, "feb": 2,
    "march": 3,    "mar": 3,
    "april": 4,    "apr": 4,
    "may": 5,
    "june": 6,     "jun": 6,
    "july": 7,     "jul": 7,
    "august": 8,   "aug": 8,
    "september": 9,"sep": 9,  "sept": 9,
    "october": 10, "oct": 10,
    "november": 11,"nov": 11,
    "december": 12,"dec": 12,
}


def _parse_month(value: str) -> int:
    """Accept a month number (1-12) or full/abbreviated month name."""
    v = value.strip().lower()
    if v.isdigit():
        m = int(v)
        if 1 <= m <= 12:
            return m
        raise argparse.ArgumentTypeError(f"Month number must be 1-12, got {value!r}")
    if v in _MONTH_NAMES:
        return _MONTH_NAMES[v]
    raise argparse.ArgumentTypeError(
        f"Unrecognised month {value!r}. "
        "Use a number (1-12) or a name such as 'March' or 'Mar'."
    )


def _parse_date(value: str) -> date:
    """Accept ISO format YYYY-MM-DD."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}. Use ISO format YYYY-MM-DD."
        )


def _days_for_year(year: int) -> List[date]:
    days, cur = [], date(year, 1, 1)
    while cur.year == year:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _days_for_month(year: int, month: int) -> List[date]:
    days, cur = [], date(year, month, 1)
    while cur.month == month:
        days.append(cur)
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Liturgical-season helpers
# ---------------------------------------------------------------------------

def _easter(year: int) -> date:
    """Return Easter Sunday for the given year (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _advent_sunday(year: int) -> date:
    """Return Advent Sunday for the given year.

    Advent Sunday is always the Sunday that falls between November 27 and
    December 3 — i.e., the first Sunday on or after November 27.
    """
    d = date(year, 11, 27)
    # weekday(): Mon=0 … Sun=6; advance to next Sunday if not already Sunday
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d


# Canonical season names keyed by their accepted lower-case aliases.
_SEASON_NAMES: Dict[str, str] = {
    "advent":        "Advent",
    "christmas":     "Christmas",
    "xmas":          "Christmas",
    "epiphany":      "Epiphany",
    "lent":          "Lent",
    "holy-week":     "Holy Week",
    "holyweek":      "Holy Week",
    "easter":        "Easter",
    "eastertide":    "Easter",
    "pentecost":     "Pentecost",
    "ordinary-time": "Pentecost",
    "ordinarytime":  "Pentecost",
}


def _parse_season(value: str) -> str:
    """Accept a liturgical season name (case-insensitive) and return its canonical form."""
    v = value.strip().lower().replace(" ", "-")
    if v in _SEASON_NAMES:
        return _SEASON_NAMES[v]
    valid = ", ".join(sorted(set(_SEASON_NAMES.values())))
    raise argparse.ArgumentTypeError(
        f"Unrecognised season {value!r}. Valid seasons: {valid}"
    )


def _days_for_season(year: int, season: str) -> List[date]:
    """Return a list of every date in the given liturgical season.

    'year' is the calendar year in which the season starts (or primarily falls).

    Season boundaries (BCP 1979 / Episcopal Church):
      Advent    : Advent Sunday (Nov 27–Dec 3) … Christmas Eve (Dec 24)
      Christmas : Dec 25 … Jan 5 of the following year  (12 days)
      Epiphany  : Jan 6 … day before Ash Wednesday
      Lent      : Ash Wednesday (Easter − 46) … Holy Saturday (Easter − 1)
      Holy Week : Palm Sunday (Easter − 7)   … Holy Saturday (Easter − 1)
      Easter    : Easter Sunday … day before Pentecost (Easter + 48)
      Pentecost : Pentecost Sunday (Easter + 49) … day before Advent Sunday
    """
    easter        = _easter(year)
    ash_wednesday = easter - timedelta(days=46)

    if season == "Advent":
        start = _advent_sunday(year)
        end   = date(year, 12, 24)
    elif season == "Christmas":
        start = date(year, 12, 25)
        end   = date(year + 1, 1, 5)
    elif season == "Epiphany":
        start = date(year, 1, 6)
        end   = ash_wednesday - timedelta(days=1)
    elif season == "Lent":
        start = ash_wednesday
        end   = easter - timedelta(days=1)        # Holy Saturday
    elif season == "Holy Week":
        start = easter - timedelta(days=7)        # Palm Sunday
        end   = easter - timedelta(days=1)        # Holy Saturday
    elif season == "Easter":
        start = easter
        end   = easter + timedelta(days=48)       # day before Pentecost
    elif season == "Pentecost":
        start = easter + timedelta(days=49)       # Pentecost Sunday
        end   = _advent_sunday(year) - timedelta(days=1)
    else:
        raise ValueError(f"Unknown season: {season!r}")

    days, cur = [], start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a BCP Daily Office EPUB from venite.app",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                          # full year 2026\n"
            "  %(prog)s --year 2027              # full year 2027\n"
            "  %(prog)s --month 3                # March 2026\n"
            "  %(prog)s --year 2027 --month March\n"
            "  %(prog)s --date 2026-03-15        # single day\n"
            "  %(prog)s --season lent            # Lent 2026\n"
            "  %(prog)s --year 2027 --season advent\n"
            "\n"
            "Liturgical seasons: Advent, Christmas, Epiphany, Lent,\n"
            "  Holy Week, Easter, Pentecost  (case-insensitive)\n"
            "  --year refers to the calendar year in which the season starts.\n"
        ),
    )
    parser.add_argument(
        "--year",
        type=int,
        default=DEFAULT_YEAR,
        help=f"Calendar year (default: {DEFAULT_YEAR})",
    )

    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--month",
        type=_parse_month,
        metavar="MONTH",
        help="Generate a single month (number 1-12 or name, e.g. 'March')",
    )
    scope.add_argument(
        "--date",
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="Generate a single calendar day",
    )
    scope.add_argument(
        "--season",
        type=_parse_season,
        metavar="SEASON",
        help=(
            "Generate a full liturgical season: Advent, Christmas, Epiphany, "
            "Lent, Holy Week, Easter, or Pentecost"
        ),
    )

    parser.add_argument(
        "--debug-html",
        action="store_true",
        help="Save raw HTML to cache dir when the liturgy section cannot be found",
    )
    args = parser.parse_args()

    # ---- Resolve the date list and descriptive labels --------------------
    if args.date:
        d            = args.date
        days         = [d]
        title_label  = day_ordinal(d)
        id_suffix    = d.strftime("%Y-%m-%d")
        output_file  = f"bcp_daily_office_{d.strftime('%Y_%m_%d')}.epub"

    elif args.month:
        month        = args.month
        year         = args.year
        days         = _days_for_month(year, month)
        month_name   = date(year, month, 1).strftime("%B")
        title_label  = f"{month_name} {year}"
        id_suffix    = f"{year}-{month:02d}"
        output_file  = f"bcp_daily_office_{year}_{month_name}.epub"

    elif args.season:
        season       = args.season
        year         = args.year
        days         = _days_for_season(year, season)
        season_slug  = season.replace(" ", "_")
        title_label  = f"{season} {year}"
        id_suffix    = f"{year}-{season_slug}"
        output_file  = f"bcp_daily_office_{year}_{season_slug}.epub"

    else:
        year         = args.year
        days         = _days_for_year(year)
        title_label  = str(year)
        id_suffix    = str(year)
        output_file  = f"bcp_daily_office_{year}.epub"

    asyncio.run(
        run(
            days             = days,
            title_label      = title_label,
            identifier_suffix= id_suffix,
            output_file      = output_file,
            debug            = args.debug_html,
        )
    )


if __name__ == "__main__":
    main()
