# Formatting tool

Checks a messy deck against an approved master deck and a brand guidelines
file, then reports the inconsistencies. Two layers: a deterministic pass that
proves what it finds from the file, and a Gemini pass that judges what the
first pass cannot.

It also rebuilds a deck onto the master's layouts, which is the fix for the
largest single cause of those inconsistencies: a deck built on somebody else's
master.

CLI only. No UI yet.

## The workflow

```
brand-book.pdf  OR  approved.pptx          [extract-guidelines, run once]
        |
        v
  brandbook/        PDF: one Gemini call, whole document as pages; every value
        |           must carry a verbatim quote or it is discarded
        |           PPTX: no model call. The theme gives the palette and the
        |           typefaces, the slides give sizes, logo and margins
        v
  config/brand.yaml   annotated, reviewed and corrected by a designer
        |
        |  ................................................
        v
master.pptx + messy.pptx                        [rebuild, when needed]
        |
        v
  rebuild/          open the master as the base file and recreate every slide
        |           on the layout it belongs to
        v
  messy.rebuilt.pptx  + a list of what needs a designer by hand
        |
        |  ................................................
        v
master.pptx + messy.pptx + config/brand.yaml   [validate, every run]
        |
        v
  extract/          read both decks into DeckProfile, derive MasterSpec
        |
        v
  rules/            deterministic layer: layouts, colours, fonts, sizes, logo,
        |           title/subtitle, orphans/widows, presentation space
        |
        |  Issue(source=rule), each tagged with a ref (R1, R2, ...)
        v
  ai/payload.py     brand guidelines + rule findings + slide digests
        |
        v
  ai/client.py      Gemini, structured JSON out
        |
        |  Issue(source=ai) + dismissals of false positives
        v
  report/           merge, dedupe, order -> list of inconsistencies
```

`validate` reports; `rebuild` writes a new file and never touches the input.
Run `rebuild` first when the deck was built on the wrong master, then
`validate` the result: the rebuild fixes what a layout governs, and the report
then shows only what is genuinely left.

## Where the rules come from

Nothing is hardcoded as a brand rule. Three tiers, and the report keeps them
apart because they carry different weight:

| Tier | Source | Provenance |
| --- | --- | --- |
| Brand values: palette, typefaces, type scale, logo, margins | `extract-guidelines` reads them out of the client's brand book PDF | `authored`, with the quote and page |
| Gaps the brand book leaves | Inferred from what the master deck consistently does, when `--master` is given | `inferred`, with the observation behind it |
| Everything, when the only reference is an approved deck | `extract-guidelines --from approved.pptx`: the theme colour scheme and typefaces, plus whatever the slides do consistently | `inferred`, always |
| Rule thresholds: what counts as bleed, as a grid line, as a collision | The `tuning` block. Judgement calls about the checks, not statements about the brand | not brand values, so no provenance |

A deck never produces an `authored` value, whatever it is called and however
approved it is. A brand book states rules; a deck only demonstrates them, and
a master drifts. Confirm a value and mark it `authored` in the provenance
block yourself, and reports stop hedging it.

### The master is the only authority

A messy deck arrives carrying the theme, the layouts and the slide masters of
whatever file it was built from. None of that is a standard. Everything
downstream reads the uploaded master instead:

| | Read from |
| --- | --- |
| Palette, typefaces, type scale, logo, margins | the brand file, then the master |
| Theme colours and typefaces | `MasterSpec.theme_fonts` / `theme_colors`, never `DeckProfile.theme_*` |
| Layouts | the master's, and `rebuild` writes only those into the output |

`font.family.theme_drift` measures against the master's theme, so a deck set
in a foreign brand's face is no longer judged to be inheriting correctly. The
AI payload sends `master_theme_*` as the reference and attaches the deck's own
theme only when the two disagree, labelled as the foreign one. A theme
mismatch is logged at the start of the run, because it explains why so much of
the deck is being reported.

A value the brand book does not state and the master deck cannot settle stays
`missing`, and the checks that need it stay silent rather than testing an
invented number. Silent is not invisible: they are listed under **Not
checked**. `safe_margins` is the clearest case: an unspecified edge is
`null` and is not checked, because a default frame would report every deck
against a margin nobody set.

Findings that rest on an inferred value are the weaker claims in any report, so
both the text and markdown outputs name them in the header.

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .          # optional, gives you the `formatting-tool` command
```

The AI layer needs credentials. Copy the template and fill in your key
(get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)):

```powershell
Copy-Item .env.example .env
```

```ini
# .env
GEMINI_API_KEY=...
```

`.env` is gitignored and is read at startup from the working directory (or any
parent). Anything already exported in the shell wins over the file:

```powershell
$env:GEMINI_API_KEY = "..."
```

The SDK also answers to `GOOGLE_API_KEY`, and to the Vertex AI variables if you
would rather route through a Google Cloud project, so an unset `GEMINI_API_KEY`
does not mean there are no credentials. `--no-ai` needs none of this.

## Use

### Extract the brand rules, once per brand

```powershell
# from a brand book, with a master deck to fill the gaps it leaves
python -m formatting_tool extract-guidelines --from brand-book.pdf `
    --master master.pptx --out config/client.yaml -v

# from an approved deck alone, when there is no brand book. No API key needed:
# nothing is read that is not already structured in the file
python -m formatting_tool extract-guidelines --from approved.pptx `
    --out config/client.yaml -v
```

The deck is its own master, so `--master` is refused alongside a `.pptx`
source: two sets of observations with no rule for which wins is worse than
one.

Every value in the output carries a comment saying where it came from:

```yaml
palette:    # authored: 'Deep Navy #1F2A44 is our primary colour.'
  "Deep Navy": "#1F2A44"    # authored p4: 'Deep Navy #1F2A44 is our primary colour.'

logo:
  min_width_in: 0.9843    # authored p12: 'The logo is never smaller than 25mm wide.'
  required_on_every_slide: true    # inferred: every one of the master's 12 slides carries a logo

safe_margins:
  top_in: null    # MISSING - not specified; dependent checks stay silent
```

Read it, correct what is wrong, commit it. Values marked `inferred` are the
ones to check first. Readings that were discarded, and why, are listed at the
bottom of the file. Extraction is lossy, which is why this is a separate
command with a human in the middle rather than something recomputed on every
validate run.

### Validate decks against it

```powershell
# both layers
python -m formatting_tool validate --master master.pptx --deck messy.pptx `
    --guidelines config/my_brand.yaml -v

# deterministic layer only, no API call, no key needed
python -m formatting_tool validate --master master.pptx --deck messy.pptx --no-ai

# see exactly what would be sent to the model, and what it would cost
python -m formatting_tool validate --master master.pptx --deck messy.pptx `
    --ai-dry-run --payload-dir out/payloads -v

# several decks against one master, as a markdown review
python -m formatting_tool validate --master master.pptx `
    --deck a.pptx --deck b.pptx --format markdown --out review.md

# what the reader saw (first stop when a rule misfires)
python -m formatting_tool profile --deck messy.pptx --out out/profile.json

# the rule catalogue
python -m formatting_tool rules
```

Exit codes: `0` clean, `1` findings at or above `--fail-on`, `2` could not run.
`--fail-on error` makes this usable as a gate.

### What the report does not show

A short report can mean a clean deck, or it can mean most of the checks were
turned off and the rest were argued away. Every report accounts for the
difference, in two sections and two stat counters:

```
Dismissed by the AI layer (360 rule finding(s))
-----------------------------------------------
  Proved from the file, then judged a false positive in context.
  Re-run with --no-ai to see them all.

   255  font.family.theme_drift
         reason: the hardcoded typeface is the brand face
    65  space.overlap
         reason: overlap is normal inside a dense diagram

Not checked (6 rule(s) did not run)
-----------------------------------
  color.text.off_palette: no brand guidelines file was supplied
  ...
  To enable: extract-guidelines --from BRANDBOOK.pdf (or the approved
             MASTER.pptx), then validate --guidelines that file
```

A dismissal deletes something the deterministic layer proved from the file, so
it is a claim in its own right and is recorded with its reason rather than
dropped. `--no-ai` reproduces the unfiltered list. In JSON both sections are
first-class: `report.dismissals` and `report.skipped_rules`.

The counters `stats.dismissed` and `stats.rules_skipped` sit alongside the
severity counts so the numbers reconcile.

### Classify the slides and see which layouts fit

```powershell
python -m formatting_tool classify --master master.pptx --deck messy.pptx
```

Reads only, writes nothing. Every slide is classified by the job it does, and
named against the master layout that serves it:

```
The master offers:
  content   13_Content slide _ VCS_to use, 14_Content slide _ VCS_to use

  #  is a      conf  fits      layout
  1  cover     0.85  none      13_Content slide _ VCS_to use  <-- no such layout
     because an image covers the canvas behind 115 characters
  3  agenda    0.90  none      13_Content slide _ VCS_to use  <-- no such layout
     because the title says 'Table of contents'
  5  content   0.40  loose     13_Content slide _ VCS_to use  <-- check
     because 41 content region(s), 2920 characters

The master has no layout for: agenda, cover.
```

Kinds: `cover`, `agenda`, `section`, `content`, `columns`, `diagram`,
`closing`, `unknown`. Fits:

| | Means |
| --- | --- |
| `good` | the master has a layout for this slide and the content fits it |
| `loose` | the layout is the right kind, but the slide carries more or fewer blocks than it offers |
| `none` | the master has no layout for this kind of slide. Not a fit to adjust, a layout that does not exist |

**How a slide is classified.** Wording first: a title reading "Table of
contents" or "المحتويات" settles it, and is only trusted on a slide light
enough to be what it says. Then the shape of a cover, which is a full-bleed
image or a title-and-subtitle with almost no copy. Then sparseness, then
parallel bands, then content regions.

**Why not structure alone.** A photo cover is an image across the canvas with
three floating text boxes, which counts as three content regions and matches a
three-region content layout perfectly. So purpose decides for `cover`,
`agenda`, `section` and `closing`, where structure is not comparable to
anything, and structure decides for the rest, where it is measurable and a
guess about purpose is not.

**Missing layouts** are only reported for those four kinds. A content layout
will hold a columns slide awkwardly; nothing will hold a cover, so that is a
real blocker and the rest is a preference. `--strict` exits 1 when the master
cannot serve a kind the deck uses. `--format json` gives the whole thing as
data.

`rebuild` uses the same classification, so the layout `classify` names is the
layout the slide is rebuilt onto.

### Rebuild a deck onto the master

```powershell
python -m formatting_tool rebuild --master master.pptx --deck messy.pptx `
    --out out/messy.rebuilt.pptx -v
```

The input deck is never modified. What the rebuild does, and why it is a
rebuild rather than a layout swap:

Re-pointing a slide at a different layout in place changes nothing you can
see. A layout supplies only the properties a slide has not already set for
itself, and a messy deck has set all of them: an explicit `<a:xfrm>` on every
shape, an explicit size and typeface on every run. Recreating the slide from
the layout is what PowerPoint's **Reset Slide** does, and it is the only way
the master's geometry and type actually take effect.

| What | Treatment |
| --- | --- |
| Placeholder copy | Moved into the matching placeholder on the new layout. Paragraph level and bold/italic/underline survive; typeface, size and colour are dropped so the layout supplies them |
| Loose shapes | Transplanted as they are, position included. They were never governed by a layout and are not now |
| Pictures | Transplanted with the image itself, so crops and effects survive |
| Charts, SmartArt, media, embedded objects | Left behind and reported by name. Their content lives in parts this cannot rebuild, and a silently broken chart is worse than a missing one |
| Speaker notes | Carried across as plain text |
| The master's own sample slides | Dropped. The master is a template here, not content |

Layouts are matched by name first. Failing that, by what the slide is for
(see `classify` above), and failing that by content regions and their counts.
A slide that fits nothing well is still placed, on the best layout available,
and named in the output for a designer to look at.

Two things it deliberately does not do:

- **It will not decide which loose text box belongs in which layout region.**
  That is a judgement call, and getting it wrong scrambles a deck. The
  placeholders are left empty and every one is listed, so the remaining work
  is visible.
- **It will not add footer placeholders to the rebuilt slides.** PowerPoint
  keeps footer, slide-number and date placeholders latent: they live on the
  layout and appear on a slide only when someone ticks them in the Header and
  Footer dialog. Do that once on the rebuilt file.

`--strict` exits `1` when a slide matched no layout or a shape was left
behind, which makes it usable as a gate.

### In the browser

```powershell
python -m formatting_tool ui
```

Serves a page on `http://127.0.0.1:8000` and opens it. Drop in a master and one
or more decks, pick a guidelines file from `config/`, run. It is the same
`pipeline.run` the CLI calls, in-process, so the findings are identical; the
page adds filtering by severity, a text filter, and a JSON download.

Two things the CLI only reports through the log, the page shows as banners: a
deck built at the wrong slide size, and an AI layer that failed and left a
rule-only report behind.

`--port` picks a different port (it walks forward if one is busy), `--no-browser`
skips opening a window, `--root` points the guidelines dropdown at another
project. Stdlib only, no server dependency. Loopback, single user, no auth: it
is a local tool, not something to expose.

## Tests

```powershell
pip install pytest
pytest
```

Nine smoke tests covering the rule layer, the AI payload and schema, the merge
step, and colour distance. None of them need python-pptx, an API key, or a
fixture deck.

## Layout

| Path | Role |
| --- | --- |
| `formatting_tool/models.py` | Every dataclass. The contract between stages. |
| `formatting_tool/brandbook/` | Brand book PDF or approved deck to reviewable guidelines file. |
| `formatting_tool/brandbook/mapping.py` | The gate: discards anything unquoted, unitless or malformed. |
| `formatting_tool/brandbook/inference.py` | Fills gaps from the master deck, marks them inferred. |
| `formatting_tool/brandbook/writer.py` | The annotated YAML a designer reviews. |
| `formatting_tool/ai/gemini.py` | Shared Gemini plumbing for both callers. |
| `formatting_tool/guidelines.py` | Loads the brand file. Rejects unknown keys. |
| `formatting_tool/extract/deck_reader.py` | The only module that imports python-pptx. |
| `formatting_tool/extract/master_spec.py` | Merges guidelines with observed master values. |
| `formatting_tool/rules/` | One module per category, one class per check. |
| `formatting_tool/rules/layouts.py` | Slide-to-layout binding, and whether the master's layouts are complete. |
| `formatting_tool/classify.py` | What job a slide does, what job a layout is drawn for, and the fit. |
| `formatting_tool/rebuild/matcher.py` | Which master layout a messy slide belongs on. |
| `formatting_tool/rebuild/builder.py` | Recreates the deck on the master. The only module that writes a `.pptx`. |
| `formatting_tool/ai/payload.py` | Builds the cached prefix and the per-batch payload. |
| `formatting_tool/ai/schema.py` | The JSON contract, enforced server-side. |
| `formatting_tool/ai/client.py` | The Gemini call. |
| `formatting_tool/report/` | Merge, dedupe, order, write. |
| `formatting_tool/web/` | The browser UI: a stdlib server and one HTML page. |
| `formatting_tool/colorutil.py` | Perceptual colour distance. |
| `formatting_tool/linemetrics.py` | Where rendered line breaks would come from. |
| `config/brand_guidelines.example.yaml` | Annotated brand file to copy. |

## Adding a rule

```python
class MyRule(Rule):
    id = "category.thing"
    category = Category.COLOR
    description = "One line, shown by `formatting-tool rules`."
    default_severity = Severity.WARNING
    requires_guidelines = True      # skipped when no brand file is supplied

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape, paragraph, run in ctx.runs():
            if ...:
                yield self.issue("What is wrong.", slide=slide, shape=shape,
                                 expected="...", found="...")
```

Register it in `rules/__init__.py:build_default_rules()`. A rule that raises is
logged and skipped, so one broken check cannot cost the report the others.

A rule whose subject is the **master** rather than the deck under review goes
in `build_master_rules()` instead. Those read `ctx.spec` alone and the pipeline
runs them once, not once per deck, so an incomplete layout is reported a single
time however many decks are being checked.

Rules never open a file or call an API, which is why `tests/test_smoke.py`
builds decks in memory instead of carrying fixture `.pptx` files. Add a case
there.

## Design notes

**Extraction is gated on evidence.** A model asked to fill in a form will fill
it in, so every value the extractor returns must carry a verbatim quote from
the document; `mapping.py` discards the ones that do not, and lists what it
threw away. The prompt draws the same line the mapping enforces: a type size
measured off a sample slide is not a stated rule. Units travel with every
measurement and are converted once, so a margin given in mm is not silently
read as inches, and px is refused outright for having no physical size without
a stated DPI.

**The master deck is evidence, not truth.** Masters drift too. Authored
guidelines win on conflict; the master fills blanks (theme colours, theme
fonts, observed sizes, logo placement) and gives the AI layer facts to reason
from.

**Two layers, not one.** The deterministic layer is precise and literal: it
will report a full-bleed image crossing the safe margin. The AI layer sees the
same finding with its `ref` and the slide around it, and can dismiss it with a
reason. Dismissals are applied in `report/merge.py`, so the final report keeps
the rule layer's precision without its false positives.

**Prompt caching drives the request shape.** Caching is a prefix match in
`tools -> system -> messages` order, so the stable content has to come first:
reviewer instructions in `system[0]`, the brand reference block in `system[1]`
(1h TTL), the deck in `messages`. The reference block is built once per run and
reused verbatim; rebuilding it per call would change the bytes and lose every
hit. Watch `cache_read` in `-v` output. A thin brand file can sit under the
model's minimum cacheable prefix (512-4096 tokens) and silently not cache.

**Slides are batched, never truncated.** A long deck splits into several calls
sharing one cached prefix, so every slide reaches the model. Tune with
`--batch-size`.

**Structured output, not prose.** `output_config.format` pins the response to
`ai/schema.py`, so the JSON is guaranteed to parse and to carry every field. No
regex over a chat reply, no retry loop for malformed JSON.

## Known gaps

- **Orphans and widows need a renderer.** A `.pptx` stores a paragraph and a
  box, not the lines they render into, so nothing in the deterministic layer
  can see a wrap. `linemetrics.py` is the seam, with three implementations
  sketched; the recommended one drives PowerPoint over COM and reads
  `TextRange.Lines()`, which is exact because it is the same engine that
  renders the deck. Until one is wired in, `OrphanWidowRule` reports nothing.
  Silent is the right failure mode: invented wrap positions are worse than
  none. `ManualLineBreakRule` covers the workaround people reach for instead,
  and catches most of these in practice.
- **Text overflow** has the same dependency (`space.TextOverflowRule`), though
  the two cheap proxies noted in its docstring are worth wiring first.
- **Theme-bound colours are not resolved.** A run bound to `accent2` is
  currently trusted rather than compared against the palette. Resolve it
  through `MasterSpec.theme_colors`, not the deck's own, in
  `deck_reader._font_color`.
- **The colour and type-scale rules need a guidelines file that the master
  could supply on its own.** `RuleContext.has_guidelines` tests the authored
  brand file, so a run with a master but no `--guidelines` skips six checks
  even though the master handed over a palette and a typeface set. The report
  now says so under **Not checked** rather than looking clean, but the honest
  fix is for `has_guidelines` to consider `ctx.spec`. It is left alone because
  turning it on silently multiplies findings: on a real deck here it took the
  deterministic layer from 418 to 1025. Run `extract-guidelines --from
  MASTER.pptx` and pass the result when you want those checks.
- **The AI layer can dismiss in bulk.** It is free to drop every finding of a
  class in one judgement, and on a dense deck it does: 360 of 418 in one
  observed run. Nothing caps this. The dismissals are all on the report with
  their reasons, so it is visible and arguable, but reading that section is
  currently the only control. A per-rule cap, or a floor below which a
  dismissal needs its own evidence, would be the next step.
- **`delta_e` is CIE76, not CIEDE2000.** It overstates distance in the blues.
  The default `color_delta_e: 3.0` is calibrated for CIEDE2000, so re-check the
  tolerance either way.
- **Arabic runs are recorded but not checked.** `RunProfile.language` is
  populated and `arabic_fonts` is loaded, but no rule yet asserts that an
  Arabic run uses an Arabic face. This matters for bilingual decks, where the
  usual defect is Arabic text falling back to a Latin font.
- **Extraction quality is unmeasured.** There is no eval set. Run it against a
  brand book whose rules you already know and check the output before trusting
  it on an unfamiliar one. The evidence quotes are what make that check quick.
- **DOCX and Keynote brand books must be exported to PDF.** `.pdf` and `.pptx`
  are the two accepted reference forms. That is deliberate: what the model
  reads is then what a designer sees on the page.
- **A template with no slides yields only a palette and typefaces.**
  `extract-guidelines --from approved.pptx` reads sizes, logo placement and
  margins off slides, so a master holding nothing but layouts leaves them
  unspecified. The command warns when it sees one. Add an example slide per
  layout to get the rest.
- **The rebuild will not place loose content into layout regions.** Deciding
  that a given text box is the left column and not the right is a judgement
  call, so the placeholders are left empty and listed instead. This is the
  single biggest remaining piece of manual work after a rebuild, and the
  obvious next step: the AI layer already sees shape names, roles, boxes and
  text, which is what the decision needs.
- **The rebuild leaves footers latent.** Footer, slide-number and date
  placeholders live on the layout and reach a slide only through PowerPoint's
  Header and Footer dialog, which has to be applied once by hand on the
  rebuilt file. `layout.header_footer_missing` checks that the layouts can
  carry them; nothing yet ticks the box.
- **`layout.band_missing` is a heuristic.** It reads a header or footer band as
  a non-placeholder shape sitting in the top or bottom strip of the layout,
  with a name check as a fallback. A brand whose furniture sits elsewhere, a
  side tab for instance, will be reported wrongly. Tune
  `tuning.header_band_fraction` and `tuning.footer_band_fraction`, or read the
  finding as a prompt to look rather than a proof.
- **Provenance is per brand value, not per finding.** The report names the
  inferred values in its header, but an individual finding does not say it
  rests on one. Threading a basis path through `Rule.issue()` would fix it.
- **Rotation is ignored** in overlap and off-canvas geometry; a rotated shape
  is compared by its unrotated box, which over-reports.
- **Refusal fallbacks are not enabled.** The server-side `fallbacks` parameter
  needs the beta messages surface; this call uses the stable one for the
  structured-output guarantee. A policy refusal on a formatting review is
  close to impossible, and `AIValidationError` covers it if one happens.

## Next

1. Point it at a real master and a real messy deck, then read
   `profile --deck` output before trusting any geometry finding.
2. Tune `tolerances` in the brand file until the deterministic layer is quiet
   on a known-good deck. Noise here poisons the AI layer, which sees every
   finding.
3. Wire up `linemetrics.PowerPointComMetrics` to unlock orphans, widows and
   overflow.
4. Fill in the `TODO`s in `rules/` -- each names the specific approach.
