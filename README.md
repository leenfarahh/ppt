# Formatting tool

Checks a messy deck against an approved master deck and a brand guidelines
file, then reports the inconsistencies. Two layers: a deterministic pass that
proves what it finds from the file, and a Gemini pass that judges what the
first pass cannot.

CLI only. No UI yet.

## The workflow

```
brand-book.pdf                          [extract-guidelines, run once]
        |
        v
  brandbook/        one Gemini call, whole PDF as pages
        |           every value must carry a verbatim quote or it is discarded
        v
  config/brand.yaml   annotated, reviewed and corrected by a designer
        |
        |  ................................................
        v
master.pptx + messy.pptx + config/brand.yaml   [validate, every run]
        |
        v
  extract/          read both decks into DeckProfile, derive MasterSpec
        |
        v
  rules/            deterministic layer: colours, fonts, sizes, logo,
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

## Where the rules come from

Nothing is hardcoded as a brand rule. Three tiers, and the report keeps them
apart because they carry different weight:

| Tier | Source | Provenance |
| --- | --- | --- |
| Brand values: palette, typefaces, type scale, logo, margins | `extract-guidelines` reads them out of the client's brand book PDF | `authored`, with the quote and page |
| Gaps the brand book leaves | Inferred from what the master deck consistently does, when `--master` is given | `inferred`, with the observation behind it |
| Rule thresholds: what counts as bleed, as a grid line, as a collision | The `tuning` block. Judgement calls about the checks, not statements about the brand | not brand values, so no provenance |

A value the brand book does not state and the master deck cannot settle stays
`missing`, and the checks that need it stay silent rather than testing an
invented number. `safe_margins` is the clearest case: an unspecified edge is
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
python -m formatting_tool extract-guidelines --from brand-book.pdf `
    --master master.pptx --out config/client.yaml -v
```

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
| `formatting_tool/brandbook/` | Brand book PDF to reviewable guidelines file. |
| `formatting_tool/brandbook/mapping.py` | The gate: discards anything unquoted, unitless or malformed. |
| `formatting_tool/brandbook/inference.py` | Fills gaps from the master deck, marks them inferred. |
| `formatting_tool/brandbook/writer.py` | The annotated YAML a designer reviews. |
| `formatting_tool/ai/gemini.py` | Shared Gemini plumbing for both callers. |
| `formatting_tool/guidelines.py` | Loads the brand file. Rejects unknown keys. |
| `formatting_tool/extract/deck_reader.py` | The only module that imports python-pptx. |
| `formatting_tool/extract/master_spec.py` | Merges guidelines with observed master values. |
| `formatting_tool/rules/` | One module per category, one class per check. |
| `formatting_tool/ai/payload.py` | Builds the cached prefix and the per-batch payload. |
| `formatting_tool/ai/schema.py` | The JSON contract, enforced server-side. |
| `formatting_tool/ai/client.py` | The Gemini call. |
| `formatting_tool/report/` | Merge, dedupe, order, write. |
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
  currently trusted rather than compared against the palette. Resolving it
  through `DeckProfile.theme_colors` is a small change in
  `deck_reader._font_color`.
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
- **Only PDF reference files.** A PPTX or DOCX brand document has to be
  exported to PDF first. That is deliberate for now: what the model reads is
  then what a designer sees on the page.
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
