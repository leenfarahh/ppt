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
        |  Issue(source=ai), plus context folded onto rule findings
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

`safe_margins` are read off the master's own layouts when no brand file states
them: the extent of a layout's *content* placeholders is where the designer
decided content may go, which is a far better frame than measuring where
content happens to sit on sample slides, and it works on a master with no
slides at all. Footer, date and page-number placeholders are excluded from the
frame and exempt from the check, because they live in the margin by design.

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

Nothing removes a finding. Every rule finding reaches the report, so the only
thing standing between what was checked and what is printed is the checks that
never ran:

```
Not checked (6 rule(s) did not run)
-----------------------------------
  color.text.off_palette: no brand guidelines file was supplied
  ...
  To enable: extract-guidelines --from BRANDBOOK.pdf (or the approved
             MASTER.pptx), then validate --guidelines that file
```

The AI layer has **no way to suppress a rule finding**. That is enforced in
the response schema, not in the prompt: `dismissed_refs` does not exist, so
the model cannot ask for something the contract does not allow, and a reworded
instruction cannot bring it back. When the AI thinks a finding is defensible
in context, it says so on the finding: a lowered `confidence` and an
explanation in `suggestion`. The judgement survives, and so does the finding,
and the designer decides.

This was not always true. An earlier build let the model dismiss with a
reason, and on a real deck it dropped 88 of 113 findings in one call,
including a 2.79 sq in text overlap it had called an error on the previous
run. A layer that can delete most of what the other layer proved is a filter,
not a review.

`stats.rules_skipped` sits alongside the severity counts, and
`report.skipped_rules` carries the same in JSON.

### Apply the fixes a designer ticked

`validate` says what is wrong and changes nothing. This is the other half.

```powershell
# what can be applied, and what needs a designer
python -m formatting_tool apply --deck messy.pptx --report review.json --list

# apply exactly the findings that were ticked
python -m formatting_tool apply --deck messy.pptx --report review.json `
    --out out/fixed.pptx --fix bb8e6162 --fix ace721b5

# everything mechanical, then rebuild onto the master's layouts, in one pass
python -m formatting_tool apply --deck messy.pptx --report review.json `
    --out out/fixed.pptx --all --master master.pptx
```

Every finding in the report carries a short `id`. That is what a designer
ticks and what `--fix` takes, so a selection made in the browser applies from
the CLI and means the same thing. The id is derived from what the finding is
about, not from its position in the list, so it survives other findings coming
and going. The input deck is never modified.

**What has a fixer.** Only findings that name a shape and a target, where
there is one way to reach the target:

| Rule | Fix |
| --- | --- |
| `space.off_canvas` | moves the shape back inside the canvas, the shortest distance |
| `space.alignment_grid` | snaps the left edge to the grid line the finding names |
| `typography.whitespace` | strips trailing spaces, collapses repeated ones |
| `typography.manual_line_break` | replaces soft returns with a space |
| `font.family.theme_drift` | clears the hardcoded typeface so the run inherits |
| `space.safe_margin` | moves the shape inside the frame, only on the edges the finding names |
| `space.repeat_out_of_line` | puts the shape back on the edge the rest of its set shares |
| `space.satellite_offset` | nudges one copy of a repeated pairing back onto the offset its cohort shares |
| `title.position_inconsistent` | moves a drifting title onto the position the rest of the deck's titles hold |
| `logo.geometry` | moves the logo onto the master's position |
| `color.text.off_palette` | recolours the runs carrying the off-palette colour to the nearest palette entry |
| `color.shape.off_palette` | recolours the fill, the outline, or the SVG an icon draws from |
| `typography.terminal_punctuation` | takes the full stop off the end of a title |

**A set is read as rows and columns, not as one shared edge.** Every space
rule modelled a set as shapes sharing ONE edge, which meant two columns of
five -- two lefts, five tops, no edge shared by all of them -- produced not a
single finding when one shape sat 0.08in low, and an eleven-node radial
diagram produced none either. `space.row_out_of_line` clusters a series into
rows and columns by centre and reports a member off the line its row shares;
`space.mirror_pair_offset` finds members that reflect across the
arrangement's axis and reports a pair that does not sit level.

The two split on what the geometry can settle. Three or more in a row is a
majority with an exception, so the exception is named and fixed. Exactly two
is a disagreement: nothing says which of them moved, and moving both to the
midpoint would level the pair while putting both off the arrangement they
belong to, so both are named and a designer decides. A shape sitting ON the
axis has no partner -- without that, six identical boxes in a column all share
a centre x, every one reflects onto every other, and a real deck produced
eight findings about a stack that is not mirrored at all.

**A set moves together, and is squared up before it does.** A column of shapes
all sitting outside the margin is not eight findings about eight shapes; it is
one column in the wrong place, and moving any one of them in breaks the
column. On a real deck that was 37 of 50 safe-margin fixes refused with
"moving it would break its alignment", every refusal correct. Now the shapes
sharing the edge travel the same distance, and any that were merely within
tolerance of the edge are put on it first -- moving a ragged set only
relocates the raggedness. That deck went from 1 applied to 10, with no
alignment refusals left.

Only the hard constraints may do this: `space.safe_margin` and
`space.off_canvas`. A deck cannot ship with content outside the frame, so the
column moves whole. A grid snap is a preference, and dragging a neighbour to
satisfy one is the failure that got `space.alignment_grid` disabled once
already. Every shape that moves is checked against its own neighbours, and the
whole set goes back if any of them lands on something.

**Arabic decks are measured as Arabic decks.** The scaffolding was there and
connected to nothing: `arabic_fonts` was merged into the Latin list before any
rule saw it, `rtl` was never read off the file, and the alignment grid
measured left edges only. So an Arabic deck came back clean -- not because it
was, but because the rules could not see it.

- `font.family.arabic` reports Arabic copy set in a face with no Arabic
  glyphs. Coverage, not direction: ONE Arabic word in an English run is enough,
  because a Latin face renders it as boxes however much Latin sits beside it.
  Fixed when the brand declares a single Arabic face; a design call otherwise.
- `typography.rtl_not_set` reports Arabic paragraphs with no direction set.
  The letters still shape and join, so the slide looks almost right -- what
  lands wrong is the punctuation, the numbers and any Latin inside the line.
  The fix is one attribute and changes no words.
- The alignment grid measures the LEADING edge, which is the right one in a
  deck that reads right to left. A column of Arabic shapes of different widths
  shares a right edge and nothing else, and the snap moves that edge rather
  than the left one -- moving the left edge of a right-aligned shape shifts it
  by its own width off the column.

Direction is read off the copy, not declared: a deck is right-to-left when
most of its text-bearing shapes are. Safe margins are unchanged, being
physical rather than directional.

**And the master is turned round rather than applied as drawn.** A master is
drawn for one reading direction: it puts its title 0.92in from the left
because that is where an English reader starts. Applied unchanged to an Arabic
deck it gets the type and the colour right and the layout backwards, which
reads as a deck somebody forgot to finish. So the rebuild mirrors the frame
about the slide's vertical centre when the deck reads right to left, and
`rebuild(mirror=...)` overrides the decision either way.

What turns is the FRAME, not the content -- layouts, the master, and any
placeholder stating its own position. A shape the author placed themselves
stays where they put it: an Arabic deck's copy is already flush right, and
turning that too moves it to the wrong side. Nothing inside a shape turns
either; a mirrored photograph is a different photograph and a mirrored chart
is a wrong chart.

The rule is "turn what states its own position, and let the rest follow",
which is what stops the frame turning twice -- a layout placeholder that
inherits from the master has already moved with it, and moving it again puts
it back. See `rebuild/rtl.py`.

**What the AI layer costs, and what it stopped costing.** A 105-slide deck
was 105 calls at concurrency three. Measured on the real payloads, one dense
slide sent an 8,418-character system prefix that never cached, 88 rule
findings of which 28 were the same rule on the same shape, and 158 shape
digests of which only 2,920 characters was text. Four changes, none of them
touching what the model is asked to do:

- Concurrency 3 -> 8. The layer is latency-bound, so this divides the wall
  clock; rate limits are waited out rather than raised, so setting it high
  costs a pause, not a slide.
- Slides packed into calls by size rather than one per call. On a real deck
  they ran 1,590 to 10,610 tokens, so a cover was costing a whole call's
  latency; now a call is filled to `--batch-tokens` and a dense slide still
  travels nearly alone, which is where one-per-call was actually protecting
  anything. The estimate is calibrated against the API, not assumed: a call
  the four-chars-a-token rule called 19,840 measured 44,732, and at that size
  the model returned nothing at all.
- Rule findings collapse to one entry per rule and shape, carrying a count.
  The surviving entry keeps the first ref, which is the one a restatement
  names and the merge absorbs into.
- A shape with no text, fill, outline, image or placeholder role is not sent.
  The model can say nothing about it that the geometry rules do not prove.
- The system prefix is put in an explicit cache once per run. It was
  byte-identical on every call and still reported `cache_read=0`, because
  implicit caching has a minimum it did not reach.

Measured on a five-slide deck: payload 258,524 -> 157,087 characters, and
`cache_read=2359` on every call instead of nothing.

**An overlap moves the shape on top.** Two boxes collide and the geometry
cannot say which is in the wrong place -- but z-order says which landed on the
other, so `space.overlap` reports the finding on the shape in front and that
is the one that moves. It is pushed the shortest way clear, but never along an
axis the two fully share: boxes side by side at one height share their whole
height, so "shortest" is downward, and moving down clears the collision by
taking the shape out of the row it belongs to.

**A collapsed row is spread, not nudged.** Five tabs each overlapping the next
is four overlaps and none is fixable one pair at a time -- push the second
clear of the first and it lands on the third. `space.series_crowded` reports
the set once and the fix distributes all of them across the space they occupy,
falling back to the master's content width when the row has outgrown its own
span. `space.overlap` stands down for those shapes.

**Applying is followed by a second pass**, and the second pass applies. The report a designer ticks
describes the deck as it arrived; once the fixes and the master have been
applied it is a different file, and nothing had measured that file. So the
deterministic rules run again on the output, and the result carries both what
they found and `introduced` -- the findings this run caused rather than the
ones it inherited. Those are then corrected on the written deck and it is
measured once more. ONLY what this run introduced: a finding the deck already
had and the designer did not tick is one they chose to leave, and picking it
up here would apply something nobody asked for. One round, never a loop. On a real deck that came back with eleven, among them a
title overlapping the subtitle by 4 square inches. Only the rule layer runs:
the AI layer costs money, and a second opinion on a file nobody has looked at
yet is not worth it.

**Which palette entry a colour becomes** is a different question from which is
nearest, and treating them as one recoloured a red to an orange 33 delta-E
away and a page of blue headings to a neutral. "Nearest" decides whether a
colour is off-palette and always has an answer. "Intended" is allowed to have
none: an entry is disqualified if it is a neutral and the colour is not (or
the reverse), if it is a different hue family, or if it is further away than
the distance at which the rule already stops recommending anything. No
qualifying entry means a designer picks, which is a better answer than one
chosen by arithmetic that had nothing suitable to choose from. Distance is
CIEDE2000, checked against the Sharma reference pairs; CIE76 overstated the
blues, which is what pushed them onto neutrals.

**Artwork a slide inherits from its layout is carried onto the slide** before
the rebuild swaps layouts. What a designed slide shows is often not on the
slide: a section divider carries a full-bleed photograph, the connector lines
joining its icons and the panels behind its copy, while its own `p:cSld` holds
nothing but a title. Point it at another layout and all of that is gone with
nothing to report, because no shape was lost -- there was never a shape.

Every non-placeholder shape travels, not only pictures; a placeholder does not,
because it is a slot and taking the new layout's position for those is the
point of a rebuild. What also does not travel is the old brand's furniture,
told apart by how much of the deck inherits it. Chrome reaches nearly every
slide because that is what chrome is for; a section's own artwork reaches the
handful of slides in that section. Anything on a slide master is the brand's
by construction, and so is anything on more than one layout.

Counting LAYOUTS instead of slides was the first reading and it is wrong for
the commonest deck there is -- one where every slide sits on the same layout.
On a real 12-slide deck its background, its logo and two footer boxes were
each on exactly one layout, read as content, and stamped onto all twelve
slides: 72 shapes. The master applied underneath them and the output looked
like the deck that came in. See `rebuild/pictures.py`.

**Icon colour is not shape colour.** An icon from PowerPoint's library is a
`p:pic`, and what the ribbon calls a *Graphics Fill* is not `a:solidFill` on
the shape -- the colour lives inside the SVG the picture draws from. Setting
`Shape.Fill` on one through automation writes byte-identical XML, verified
against desktop PowerPoint. So every icon in every deck was invisible to the
colour rules and unfixable however the finding was worded.

Microsoft's icons state their intent in the markup, which is what makes this
tractable: `class="MsftOfcThm_Accent1_Stroke_v2" stroke="#A32020"` says the
icon reads theme accent1, and the literal beside it is what that slot resolved
to. A theme-bound icon is not itself wrong -- the deck's theme is, and
`rebuild` corrects every icon reading it at once. An icon with a literal
colour and no class was recoloured by hand and gets a fixer, which rewrites
the rule and the literals together. See `formatting_tool/svgicon.py`.

**Orphans and widows are detected and bound.** Where a line breaks is not in
the file -- a .pptx stores a paragraph and a box, and the renderer decides --
so `typography.orphan_widow` had no way to fire and never did. PowerPoint now
supplies the real breaks (`linemetrics.PowerPointComMetrics`), one deck open
per run, about three seconds on a short deck; `--no-line-metrics` turns it off
for a fast pass. The fix is a non-breaking space between the last two words,
which is what a typesetter does: one character, no words changed, undone by
deleting it. Widening the box was the other candidate and is worse -- it
changes the composition and re-wraps the paragraph, so it can strand a
different word instead of no word.

**Production notes are taken off before the deck goes out.** A deck being
worked on collects messages addressed to whoever is making it -- "Design - can
you redo the map and make the colour contrast stronger", "TBC with legal", a
coloured comment box parked in a corner -- and they must not reach a client.
Only the AI layer can tell one from a caption, because the question is who the
text is talking to, so it reports them as `production_note` and proposes
`remove_note`.

This is the only op that takes something off a slide, and removal is the one
change a designer cannot check by looking at the result: everything else
leaves evidence, this leaves a gap. So it is the most guarded. A placeholder
is never removed -- it is the layout's structure, and a title box holding a
note is a title box with a note typed into it. Nothing is removed below 0.8
confidence, which is a higher bar than the 0.5 that separates a judgement call
from a defect, because leaving a note in costs a designer ten seconds and
taking a caption out of a client deck is a defect nobody sees until the client
does. What was removed is quoted verbatim and listed apart from every other
outcome.

**An AI finding is fixable when it carries one.** The model is asked for a
`fix`: an `op` from a closed set (recolour a fill, line or text; set an
approved typeface; set a size; turn off shrink-to-fit; drop trailing empty
paragraphs; move; resize) and a target. Most findings have none, which is
right -- the AI layer exists for the judgement the rules cannot make, and a
judgement has no op.

Every shape in the payload carries its OOXML `id`, and a proposal has to
return it. Names repeat within a slide -- sixteen shapes called "Pentagon 7"
is a real deck -- so a fix matched on a name lands on whichever came first.

`move` and `resize` were refused outright at first, on the grounds that the
model is told not to measure off a rendered image. That was the wrong line.
The payload carries every box in inches, the slide size and the safe margins,
all exact, so reasoning from those to a position is arithmetic on numbers it
was given -- which is what `basis: "geometry"` has always meant. A proposed
box is checked against the safe margins and refused if it falls outside, then
goes through the same overlap and alignment guards a measured fix gets: a
slide is a composition whoever proposed the move.

Every proposal is checked against the master before the file is touched. A
colour has to be a palette entry *exactly* -- "nearest" is how a colour the
model liked the look of gets written into a client deck wearing a brand label
-- a typeface has to be approved, a size has to sit in the role's range. With
no master to check against, none of them run. So the worst a bad proposal can
do is cost itself.

**Every move is checked against the shapes around it**, because a slide is a
composition and satisfying a rule by shoving a shape into its neighbour trades
a measurable finding for a visible one. Three guards, each of which exists
because it was missing once and a designer sent back a screenshot:

- A shape lying **wholly off the slide** is never dragged into view. It is
  invisible where it is, so nobody is looking at a defect; it is parked or
  left over, and either way that is a designer's call. A shape only
  *straddling* an edge is still brought back.
- A move that **covers more of a neighbour** than before is reverted. Area, not
  which neighbours are touched: a caption already overlapping the portrait
  above it gains no new neighbour by sliding further under it.
- `space.alignment_grid` **acts only on a grid the master declares.** A real
  slide carries several legitimate columns, and a line four shapes happen to
  share is not one of them; against an inferred grid the fix stands down and
  says so. Mark the master's presentation space to enable it.
- A **delta measured from a position that has since changed** is refused.
  `space.satellite_offset` says "0.15in right of where its copies sit", which
  is only true of where the shape was when the report was written. On a real
  deck the edge fix corrected that same 0.15in first, and this one subtracted
  it a second time. Absolute targets are idempotent and re-run freely; this is
  the one that cannot be.

On the deck those screenshots came from, the guards cut the moves from 141 to
4, took the report from 171 findings to 37, and left every other rule count
unchanged -- no finding anywhere got worse.

Fixes run in a fixed order, not report order: preferences first, hard
constraints last, so a grid snap cannot push a shape back off the canvas and
the safe margin has the last word over both.

Everything else is reported as needing a designer, with the reason:
`space.overlap` names two boxes and cannot know which should move,
`logo.missing` needs a logo file, `title.missing` needs copy that has to be
written, `layout.not_in_master` is what `rebuild` is for.

**Two things this got wrong first, and now tests for.** Shape names are not
unique -- a real deck carried sixteen shapes called "Pentagon 7" on one slide
-- so fixes are matched on the OOXML shape id, and an ambiguous name with no
id is skipped rather than guessed. And two fixes can touch one shape: snapping
a box to a grid line after clamping it onto the canvas pushed it straight back
off, so hard constraints now run after preferences.

On a real deck, `--all` took it from 165 findings to 29, and the 29 left are
exactly the ones above that need a person.

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
| Customer-data tags, empty `r:id` hyperlinks, hd/svg image alternates | Stripped, and the shape kept. None is content: an empty `r:id` is the idiom for "no hyperlink", a tag is metadata, and an `hdphoto` is a second copy of a picture the shape already carries |

**think-cell is the real casualty.** Its charts are OLE objects with their own
part graphs, so they cannot cross and are reported by name. On a consulting
deck that is usually the most important thing on the slide, so check the
dropped list before sending a rebuild anywhere.

**Rebasing onto a master repaints the deck.** Theme-bound colour and inherited
type re-resolve through the *master's* theme, which is the point of rebuilding
and also a very large visual change: on a real deck 35 theme-bound runs went
from a red brand to the master's yellow, and the master's own logo and
furniture appeared on every slide. Nothing is lost, but render before and
after before sending it on.
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
python -m formatting_tool ui --reload
```

**Use `--reload`.** Python imports a module once and never re-reads it, so a
server started before an edit serves the old code for as long as it runs,
without saying so. An afternoon of reports once came out of a stale process
and looked exactly like a tool that had not changed. With `--reload` a
supervisor restarts the server whenever a source file changes; without it the
banner says `Reload: OFF` so at least the state is visible.

The page is the whole loop: drop in a master and a deck, run the check, tick
the findings you want, apply them, and render before and after to see what
changed before downloading the corrected deck.

- Every fixable finding gets a checkbox. Findings that need a designer do not,
  and keep an empty column so the list still reads down the page.
- **Apply** sends the ticked ids to `/api/apply`, which runs exactly the same
  `apply_fixes` the CLI does, against the deck you uploaded. Optionally rebuild
  onto the master's layouts in the same pass.
- **Render before / after** draws the affected slides through PowerPoint, side
  by side. "moved 0.65in back onto the canvas" is a claim; two pictures are
  the evidence, and you can reject the result before it reaches a client.

The uploaded deck is kept for four hours so the ticking and the applying can be
minutes apart. Nothing leaves the machine except the AI call, if it is on.

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
| `formatting_tool/rules/repeats.py` | One of a set of identical shapes out of line with the rest. |
| `formatting_tool/classify.py` | What job a slide does, what job a layout is drawn for, and the fit. |
| `formatting_tool/rebuild/matcher.py` | Which master layout a messy slide belongs on. |
| `formatting_tool/rebuild/builder.py` | Recreates the deck on the master. |
| `formatting_tool/apply/fixers.py` | One fixer per rule, for the findings a machine can correct. |
| `formatting_tool/apply/applier.py` | Applies the ticked findings, then optionally the rebuild. |
| `formatting_tool/report/reader.py` | Reads a report back from JSON, so a selection round-trips. |
| `formatting_tool/ai/payload.py` | Builds the cached prefix and the per-batch payload. |
| `formatting_tool/ai/schema.py` | The JSON contract, enforced server-side. |
| `formatting_tool/ai/client.py` | The Gemini call. |
| `formatting_tool/report/` | Merge, dedupe, order, write. |
| `formatting_tool/web/` | The browser UI: a stdlib server and one HTML page. |
| `formatting_tool/render.py` | Renders slides through PowerPoint, for the AI layer and the previews. |
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
same finding with its `ref` and the slide around it, and can argue that the
context justifies it, by naming the ref in `confirms_refs` with a low
confidence and an explanation. `report/merge.py` folds that onto the rule
finding, which keeps its own wording and gains the argument. What it cannot do
is remove the finding: the response schema has no field for it.

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
- **`apply` does not re-check the deck afterwards.** The per-move guards stop
  a fix making something worse, but they compare bounding boxes, which is not
  the same as looking at the slide. Run `validate` on the output, or render
  before and after in the UI. The report itself tells you to.
- **Repeated elements on a curve are not checked.** `space.repeat_out_of_line`
  finds a series of identical shapes and holds it to the row or column the
  majority share. Eleven badges arranged around a hexagon have neither, and the
  one that looks wrong there looks wrong to an eye rather than to a ruler.
  Reporting a guess about it would be worse than silence, so that case belongs
  to `--render`. The rule also only looks at top-level shapes: inside a group,
  positions are relative to a drawing somebody composed on purpose.
- **A vision pass is opt-in and narrow.** `validate --render` draws each slide
  through PowerPoint and attaches it to the AI call, for the defects that only
  exist once rendered: clipped text, type that actually collides rather than
  boxes that merely overlap, contrast over an image, a shape hidden behind
  another. Every AI finding carries `basis`, `render` or `geometry`, and a
  claim that says it read something off a slide that was never rendered is
  demoted to `geometry` rather than trusted. Measurements are never taken from
  pixels: 0.06in off a grid line is real and invisible.
- **Rendering needs PowerPoint and pywin32, on Windows.** `pip install
  pywin32`. LibreOffice is the obvious portable fallback and is deliberately
  not written: it needs a PDF rasteriser as a second dependency and wraps text
  a few percent differently from PowerPoint, and the questions a render answers
  are exactly the borderline ones.
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
