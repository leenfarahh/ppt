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
  rebuild/quarantine  a slide the master has no layout for is measured before
        |           and after, and where the restyle wrecked it the slide is
        |           kept exactly as it arrived, with a comment on it
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
  rules/  pass 1    each slide on its own: layouts, colours, fonts, sizes,
        |           logo, title/subtitle, presentation space
        v
  rules/  pass 2    re-reads the file, then the deck against itself: role
        |           sizes, title heights, the alignment grid -- and the
        |           orphan/widow check LAST, after everything else
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
| `color.text.off_palette` | in a placeholder, sets the colour the master gives it; elsewhere, the nearest palette entry |
| `color.shape.off_palette` | recolours the fill, the outline, or the SVG an icon draws from |
| `typography.terminal_punctuation` | takes the full stop off the end of a title |
| `font.family.arabic` | sets the Arabic runs in the brand's Arabic face, when it declares one |
| `typography.rtl_not_set` | marks the Arabic paragraphs right-to-left |
| `typography.rtl_alignment` | sets the Arabic paragraphs right aligned in the box they are in |
| `space.rtl_leading_edge` | moves a shape onto the mirrored column, so it leads from the side the deck reads from |

**Undo, one change at a time.** A designer applies forty corrections, looks at
the renders, and wants one of them back. `--undo ID` holds that finding back;
repeat it for each one, and everything else is applied exactly as before. In
the browser it is an Undo beside every change in the list and under every
render, with the held-back ones listed underneath and a Restore on each.

```powershell
# everything mechanical except one change that was not wanted
python -m formatting_tool apply --deck messy.pptx --report review.json `
    --out out/fixed.pptx --all --undo bb8e6162
```

UNDO IS A REPLAY, NOT A REVERSE. The deck is written again from the original
upload without the changes on the undo list, rather than the corrected file
being edited to drag one shape back. That is not a detail of the
implementation, it is the only version of this that is correct: a fix is not
an independent edit. The colour plan is decided across the whole deck at once
so that colours which are distinct stay distinct, a cohort move carries shapes
no finding named because a column moves whole or not at all, and the second
round exists only to clear up what the first round caused. Put one shape back
by hand and everything derived from it stays behind, which is a file in a
state no run of this tool would ever produce.

Because the input deck is never modified, the original is always still there
to replay from, and the result is not an approximation of the file the
designer would have had if they had never ticked that fix: it is that file.
Second-round fixes undo on the same terms as first-round ones, because the
list a designer reads does not distinguish them.

**What an undo costs, and what that paid for.** An undo is an apply, so it
takes as long as the apply did -- which on a 105-slide deck with 1086
mechanical fixes was long enough to be the first thing anyone said about the
feature. Two things came out of profiling it, and both make every apply faster,
not just the undos:

| | Before | After |
| --- | --- | --- |
| The fixes, on 105 slides / 1086 fixes | 47.4s | 20.9s |
| Preview render, three slides of that deck | 10.6s | 2.2s |

A rebuilt run costs more per undo than a plain one: measured on a real
31-slide, 31MB deck, an undo takes 50s and 30s of that is PowerPoint applying
the master again. The fixes themselves are 9s of it.

**An undone change stays undone.** Holding a fix back leaves the defect it was
correcting standing, the recheck finds that defect, and it can find it under a
rule the original report never fired on that shape -- which makes it, to the
second round, a finding this run introduced and therefore its business to clear
up. A row of six shapes went back to being spread evenly by
`space.series_uneven` after `space.series_crowded` had been taken back on it,
and the file came out as though nothing had been undone. So a shape a change
has been taken back on is one the run has finished with: the second round skips
it. One shape wide, and only in the second round -- the first round is the list
the designer ticked, and a second change they ticked on the same shape is still
theirs to have.

The fix loop was reading the same slide over and over through python-pptx,
where every geometry read is an XPath: `_cohort_move` rebuilt the list of
shapes not coming along once per member and re-read all their boxes each time,
and `_find_shape` walked the whole slide comparing ids for every finding. The
shapes of a slide are now indexed once per run and dropped only when a fix
removes one, and the cohort path works out what it is measuring against once.
The output is identical -- same 1086 fixes, same details, checked against a
fingerprint of the run.

The preview used to render the whole deck whatever the page was going to show.
It now asks PowerPoint for the slides it needs, falling back to the whole deck
when that is cheaper (above a fifth of it) or when per-slide export fails,
which it does on some paths. The rest of an undo is the recheck and the second
round, which measure the written deck the same way `validate` does.

**Placeholder text takes the master's own colour.** Snapping an off-palette
title to the closest brand colour is a guess made in front of an answer: the
master has already said what colour a title on that layout is. It is not one
colour per deck either -- off one real master a title is white on the cover,
the accent green on a section divider, and the dark text colour on a content
layout -- so the question is always about a particular layout.

The cost of guessing was not theoretical. On that master a red cover title
measured nearest to the theme's dark colour, so the fix was black text on a
dark navy slide: unreadable, and reported as putting the deck on brand.

The value is read where it lives, which is why nothing found it before: a
layout placeholder holds no text, so it carries no run with a colour on it.
What it carries is a default, `a:lstStyle/a:lvl1pPr/a:defRPr`, and where it
carries none it inherits from the master's placeholder and then the master's
`p:txStyles`, with scheme colours resolved through the theme. See
`extract.textstyle`.

It applies to any placeholder -- title, subtitle, body -- whenever the slide
sits on a layout the master has and that layout states a colour for it.
Otherwise the nearest-palette fallback stands, because a deck still on its
previous master names layouts this one has never heard of, and matching them
by name would be reading another brand's file. The deck-wide colour plan gets
no say here: it exists to keep colours that MEAN something distinct from each
other, and a title is not an encoding -- every title on a layout is the same
colour on purpose.

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

Only the hard constraints may do this: `space.safe_margin`,
`space.off_canvas`, `space.text_collision` and `space.rtl_leading_edge`. A deck
cannot ship with content outside the frame, with copy drawn across a shape, or
reading from the side it does not read from, so the column moves whole. A grid
snap is a preference, and dragging a neighbour to satisfy one is the failure
that got `space.alignment_grid` disabled once already. Every shape that moves is checked against its own neighbours, and the
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
- `typography.rtl_alignment` reports Arabic paragraphs explicitly set flush
  left. A different defect from the one above and not fixed by it: `algn="l"`
  names an EDGE of the box, not a leading edge, so a paragraph can read right
  to left and still sit against the left margin with its rag on the side the
  reader starts from. Only an explicit "left" is reported -- an unstated
  alignment inherits flush right, and centred and justified copy have no side.
  Table cells count, minus the header row, which `table.header_alignment`
  already reports.
- `space.rtl_leading_edge` reports a SHAPE that still starts at the English
  column: its left edge on a column the master declares and its right edge on
  none. Copy set flush right inside a box placed against a left-hand frame
  still reads from the wrong side of the page. Pictures are the case nothing
  else could see -- they hold no text, and the alignment grid reads copy --
  and the miss is far too large for that rule's near-miss window anyway. The
  fix moves the shape onto the MIRROR of the column it sits on, and only when
  the master declares that mirrored column too: an asymmetric master states no
  mirror, and there this says nothing rather than inventing a position. Footer,
  page-number and date placeholders are exempt, living where the master puts
  them.

  The columns are read off the master's layouts as the designer drew them,
  per slide, from the layout that slide is built on. Nothing is added to the
  master and nothing in it is changed: unlike `space.alignment_grid`, this
  does not wait for a PS mark, because an unannotated master is the ordinary
  case and a check that stayed silent on one would be no check at all.
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
| Loose shapes | Transplanted as they are, position included. They were never governed by a layout and are not now -- unless they form a run the layout has repeated regions for, in which case they fill them in reading order |
| Pictures | Transplanted with the image itself, so crops and effects survive |
| Charts, SmartArt, media, embedded objects | Left behind and reported by name. Their content lives in parts this cannot rebuild, and a silently broken chart is worse than a missing one. They are also never carried off a LAYOUT onto a slide: doing so wrote a file PowerPoint would not open at all |
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

**A picture that renders as white.** A full-bleed image down the right half of
a section divider came out of the rebuild as a blank panel, and every check
said the file was fine: the `p:pic` was there, on top, with its frame baked in
and its image resolving to a 2.4MB JPEG in the package. PowerPoint opened it
without complaint, listed the shape as visible at the right size, and exported
nothing. What the shape had lost was its GEOMETRY -- a picture placeholder
states no `a:prstGeom` because it takes one from the layout behind it, and
where the layout states none either, both are relying on the implicit rectangle
the schema gives a placeholder. Strip the `p:ph`, which is what freezing must
do, and the shape is an ordinary picture with a frame and no geometry, and a
shape with no geometry has nothing to fill. So the implicit rectangle is now
written out before the `p:ph` goes, and only where the shape states nothing of
its own.

Layouts are matched by what somebody looked at first -- the AI layer's reading
of the render, when there is one -- then by what the slide is for (see
`classify` above) and the content regions it carries: how many of each kind,
and **where they sit**.

**The model is now shown the layouts, not told about them.** It used to get a
picture of the slide and a written list of what the master offered, which is
half a comparison. On a real fourteen-layout master, seven of them --
`Title with Content 01` through `07` -- describe identically: one title, one
content region, six of the seven at the same inches. What separates them is
decoration no placeholder records -- a panel down the right of `03`, an image
band across the top of `04`, a half-canvas image on the left of `05`, a band
across the bottom of `06`. So the model answered `01` every time, which is the
best answer available to anyone shown seven identical descriptions, and the
structural matcher was no better off: its position score reads the same
placeholders.

`ai.layoutsheet` renders the master's layouts once per run and tiles them into
one numbered contact sheet, sent with every slide. Each region is labelled
where it falls -- a picture region, which cannot hold text, is covered with a
labelled block -- so a tile shows both halves of what a layout is: where its
content goes, and what is already on the page. One image rather than fourteen
parts, because fourteen would be fourteen uploads on every call.

Measured on the same 17-slide deck, against the same master:

| Slide | Told about the layouts | Shown them |
| --- | --- | --- |
| 5, a four-card row over a landscape photo | `Title with Content 01` at 0.40, below the floor, so `Project Card` -- a title block and a picture region | `Content with Image 02` at 0.90: "a wide landscape photograph across the bottom, matching this layout's bottom image region" |
| 8, two columns under a curved graphic | `Content with Image 01` at 0.60 -- an image layout for a slide with no image | `Title with Content 02` at 0.90: "the distinctive top-heavy curved background graphic found on this layout" |
| 15, three cards, unfinished | no pick survived; `Project Card` | `Title with Content 01` at 0.30, taken and flagged |

Confidence across the deck went from 0.30-0.60 to 0.90-1.00, and the model
stopped hedging about missing layouts it could not see.

**An image region is not a content region, in the prompt as well as the
matcher.** The written inventory folded `PICTURE` in with the copy regions, so
`Content with Image 01` was described as "offers 2 content, 1 title". Asked
where a two-column text slide belonged, the model answered it and said why:
"this layout offers a title and two content placeholders". The right deduction
from what it had been told. `rebuild.matcher` had already been taught the
difference; this had not.

**A hesitant pick still beats a structural guess.** The confidence floor threw
away every pick below it, which is only right while the thing underneath is
better. On that deck four slides came back between 0.30 and 0.40, each saying
the same true thing -- the slide runs three columns and the master has no
three-column layout -- and the floor replaced them with structural picks
scoring 0.03 to 0.08. That is not a weaker answer to the same question, it is
noise; and the noise landed on `Project Card`, so text slides arrived with a
half-page picture region nothing could fill. The floor still means something:
above it the pick is taken outright, before any structure is measured. Below it
the pick wins only where the structure could not reach its own floor either --
two admissions of uncertainty, and the one that looked at the slide is the
better of them. The match is still reported as not confident, because a slide
placed because nothing fit is exactly the slide a designer should be shown.

**What a slide SAYS it is outranks what it looks like.** Showing the model the
layouts invites matching on appearance, and appearance is the right signal for
"one column or two" and the wrong one for "what is this slide for". The deck's
agenda -- title reading "Agenda", six items in two columns -- was matched to
`Title with Content 02`, because that layout carries a decorative arc across
its top and so did the slide. True, and the wrong answer: the master has a
layout built to BE an agenda. So the four kinds a slide can state outright -- a
cover, an agenda, a section divider, a closing -- keep their purpose-built
layout where the master has one and the classifier read it off the page rather
than off the slide's position. Everything else stays with the render, which is
the whole point of asking.

**The reading of the render now reaches the rebuild a designer actually runs.**
`ai.layout` asks the model which layout each slide belongs on, from its
picture, and its own notes record it beating the structural matcher on four of
five slides of a real deck -- the question it answers is coarse and
categorical, which is what a render is good for. It was wired into
`validate --apply-master` and into nothing else: Apply with "rebuild onto the
master" called `rebuild()` with no picks at all, so every layout it chose came
from counting boxes. The picks are computed once per session and kept, because
an undo replays the whole run and paying a call a slide again on every
take-back would make the cheapest correction the most expensive thing on the
page. With the AI layer off, no renderer, or no key, the structural matcher
decides alone exactly as before.

**The name the slide carries is the weakest of the three**, and it used to be
the strongest. A slide whose layout name existed in the master was placed on it
outright, scored 1.0, called confident and never measured. That is right often
enough, and wrong in the cases that matter: a name survives everything. A deck
rebuilt onto one master and handed another carries the old names; a template
renamed around its slides carries names for what a layout used to be; an author
duplicating `Title with Content 03` to make something else keeps the name.
Evidence beats labels.

Three things keep that from throwing the name away entirely, because it is
still a designer's own statement:

- **It settles ties.** A master with seven near-identical content layouts is
  normal and the structure cannot separate them at all; the slide's own name
  for one of them beats the tie-break it replaced, which was whichever layout
  sat earlier in the master.
- **It decides when there is nothing to measure.** A slide carrying no content
  gives the structure no evidence, and "structure first" cannot mean preferring
  a silence to a statement.
- **It outranks a classification the classifier is unsure of.** `classify_slide`
  grades itself: a title saying "Agenda" is 0.9, a first slide with one region
  0.75, "too little text to be a content slide" 0.6. Below 0.75 the reading is
  a guess, and a guess does not move a slide off the layout its author named --
  before this, it called a rebuilt deck's cover a section and took it off
  `Title Slide`.

Where the name and the structure disagree and the structure wins, the report
says so: the disagreement is worth a designer's eye either way.

Four things decide it, and each was a wrong pick on a real deck before it was
there:

| | |
| --- | --- |
| Copy is counted in COLUMNS, not boxes | Fourteen loose text boxes a slide is not fourteen demands; a heading, its list and a caption are one column |
| Imagery has to be big enough to be imagery | Three 0.4in icons beside the copy made a slide with no photograph read as an image slide |
| Position is measured PER FAMILY | Title against title, copy against copy, photo against photo. Compared as one union, a layout wins by being big: an agenda layout with 25 placeholders overlapped everything |
| Crowding lowers the score | Twelve boxes and two boxes both read as one column. The layout is the right shape for either; it only FITS the second, and the difference is what the confidence flag is for |

Position matters because counting runs out. A messy slide made of fourteen
loose text boxes asks for fourteen regions, no designed layout offers more than
three, and every candidate scores the same; the match was then settled by which
layout came first in the master, so seven content slides on one real deck all
went to `Content with Image 01`, image layouts for slides with no image. Where
the content sits survives that: a photograph down the right half and a column
of copy on the left is the same shape of slide whether the copy is in one
placeholder or fourteen boxes. Counting still dominates -- a layout that cannot
hold the content is wrong wherever its regions sit -- and position breaks the
ties, with the imagery weighted heaviest, because `Content with Image 01` and
`02` differ by nothing else. A picture region is also no longer counted as
interchangeable with a body one: text in a picture region and a photograph in a
body region are both wrong, and a designer sees which straight away.
A slide that fits nothing well is still placed, on the best layout available,
and named in the output for a designer to look at.

**Copy lands in the slot it sat on, not the next one in the file.** A layout's
placeholders sit in the order the designer's XML happens to carry, which has
nothing to do with the order a reader sees. Claiming the first free one of the
same family scrambled an agenda of eleven numbered items: the rebuilt slide
read 02, 03 ... 11, 01, with item 01 alone at the foot of the second column and
a two-digit number wrapped to "0 / 3" in a slot sized for something else. The
item at the top left of the old slide now takes the placeholder at the top left
of the new one -- overlap decides, distance between centres breaks ties.

**A layout's repeated regions are filled from the slide's repeated content.**
This used to be the thing it deliberately would not do, and the reason was
sound: deciding which loose box belongs in which region is a judgement call,
and getting it wrong scrambles a deck. What it cost was the whole value of
choosing the layout. A real deck's agenda -- title reading "Agenda", five items
across the bottom -- landed correctly on the master's `Agenda` layout, which
then arrived with all twenty-four of its slots empty and the five items sitting
at their old inches on top of the layout's photograph.

**Order decides it, not position.** `builder._claim` puts a placeholder's copy
in the slot that sits where it used to sit, which is right for a slide already
on a layout of the same shape and no answer at all here: the slide's agenda is
five cards across the bottom, the layout's is a twelve-item list down the right
half, so of five items one overlaps no slot and the other four collide in
pairs. The two arrangements are not the same picture. What maps them is that
both sides are *runs*, and a designer fills run to run in reading order. The
master's own sample slide settles which order: it fills the left column `01` to
`06` and the right `07` to `12`, so a tall list reads down before it reads
across, and a wide row of cards reads across.

A run is a set of like-sized boxes filling a regular grid -- three or more of
them on the slide, where a run stops being a coincidence. Both sides have to be
one, and the layout's has to be long enough to take the whole of the slide's:
nothing is filled by halves, because half an agenda in the right place and half
in the wrong one is harder to fix than none of it. On a master offering one
content region per layout, nothing fires at all.

Two measurements it needs to get right, both of which were wrong first:

- **Which regions.** The agenda layout offers twelve 3.92in labels and twelve
  0.48in ordinal slots. Ranking them by the DIFFERENCE between aspect ratios
  made the ordinals look nearer to the slide's 2.15in cards by four hundredths,
  and five agenda titles went into five half-inch boxes. Compared as a ratio
  instead: twice as wide and half as wide are equally unlike, which a
  subtraction cannot say.
- **What goes with the run.** A run is not only its words. That agenda was five
  labels and also five icons above them, four rules between them and five empty
  panels below -- one old layout's way of drawing a list. Move the words and
  leave the drawing and the slide reads as a tidy agenda with five orphaned
  icons floating across it: worse than before, with the copy in the right
  place. So the drawing goes too, if it carries no copy, is no bigger than one
  item of the run, and sits in the run's own block. Every shape it takes is
  reported, because this deletes things.

### A slide the master has no layout for is handed back, not wrecked

A deck's framework page was four labelled layers with connector lines between
them. The master offered nothing of that shape -- its nearest layout is a
title, a subtitle, a circle drawn as artwork and eight identical body regions
stacked down one side -- so PowerPoint's placeholder matching poured four
layers of copy into eight slots that knew nothing about them, and every bullet
landed on top of the diagram it belonged to. The layout choice was the best
available and the result was unreadable. That is not a wrong pick; it is a
master with no right answer in it.

So the slide is left exactly as it arrived, on its own design, and a
PowerPoint comment on it says why. A slide nobody can lay out automatically is
a slide for a person, and the original is a working slide where the restyle is
not.

**Doubt is the gate, not the verdict.** The matcher already says when it is
guessing, and on a thin master it says so often -- quarantining every slide it
doubted would hand back half the deck, including the ones it doubted and got
right. So a doubted slide is *measured*: the walk already copies the slide,
restyles the copy and deletes one of the pair, which means that at the moment
of the decision the slide exists twice, as it arrived and as the master would
have it. Both are measured and the loser is deleted. Normally that is the
original, which is what the route always did; where the restyle wrecked the
slide it is the restyle. The slide count never changes either way, so the
plain 1..n walk stays correct.

**Worse means what a reader sees**, in square inches of the page: copy written
over other copy, text spilling out of the box holding it, shapes pushed off
the edge. Not a count of findings -- one bullet overlapping another by a hair
and a title written across a diagram are both one finding, and only the second
is a reason to hand a slide back. A slide that arrived broken and came out
marginally worse is not this feature's business, and neither is a tidy slide
that picked up a hairline collision: the restyle has to add a square inch of
new damage *and* leave at least two behind.

**Everything downstream has to leave it alone**, and each of those was a way
to break the promise quietly. `fill_runs` would claim its boxes and delete
them. `reset_layouts` would move its placeholders. The RTL mirror would turn
it round -- which is the exact mistake `rebuild.rtl` opens by warning about,
since the mirror exists because the *master* is drawn for the other reading
direction and this slide is not on the master. All three now skip it.

**The XML route cannot do this** and says so rather than differing silently.
It builds the output from the master, so there is no untouched slide sitting
in it to keep; the slides it would have considered are named in the report
with a note explaining that a Windows host with desktop PowerPoint is what
measures and keeps them.

**The PowerPoint route needs a second pass for it.** Assigning a CustomLayout
runs PowerPoint's own placeholder matching, which is why that route is
preferred -- but that matching only ever moves a placeholder into a
placeholder. It does instantiate the layout's empty regions onto the slide, so
`builder.fill_runs` reopens the written file and fills them. That route
otherwise avoids re-serialising the file at all; this re-serialisation is
narrow (text into regions already on the slide, and the consumed boxes removed)
and only happens on a deck where a run actually matched.

**A run's icons follow it; the run's drawing does not.** The first version of
this deleted both, and on the agenda that meant losing the five icons a
designer had chosen along with the four rules and five blank panels between
them. The distinction is what the shape is: an icon is content someone put
there, a rule is the old layout's way of separating two rows, and the new
layout draws its own rules. So pictures move and drawing goes.

Where a picture moves to is the layout's own answer. A designer who draws a
list of twelve labels usually draws twelve somethings beside them, and on this
master each agenda label has a 0.48in box at the same row for its ordinal --
which is exactly where that item's icon belongs. So a filled run looks for a
second run of regions pairing one-for-one with it, row by row, and fits each
item's icon into its partner: scaled down to fit and centred, never scaled up.
Exactly one icon per item or none of them move, because a partial mapping
scatters a deck's iconography across a list.

**A title the layout already writes is not carried over.** The master's agenda
layout has no title region at all; it draws the word AGENDA itself, as artwork.
So the deck's title placeholder found no home, was transplanted at its old
inches, and the rebuilt slide said "Agenda" twice -- once in the layout's
display type and once in the deck's, over the layout's photograph. A shape
whose WHOLE text matches something the layout already writes is now dropped and
reported. Whole text, with case, punctuation and spacing taken out: a heading
that repeats the layout's word and then says more is a heading, not an echo.

**Graphics that land on the new layout's artwork are lifted onto what they
label.** A two-track slide carried an icon above each column, sitting on the
edge of the arc its old layout drew. The master's arc is bigger, so both icons
came to rest well inside it: dark line art on a dark disk, still there and
impossible to see. The geometry was right and so was the layout.

Asking whether a shape sits on the artwork does not settle it -- that layout
puts its own title and body regions inside the same arc, in reversed type, on
purpose. What settles it is that these graphics belong to something: each is
centred over a column of copy, and the place it belongs is directly above that
copy. So one is lifted only when all three hold: it is small enough to be a
label rather than content, it is sitting on artwork big enough to swallow it,
and there is copy below that it is centred over. A ring and the glyph inside it
move together, because one icon is often several shapes.

Two measurements this needs, and the second is not obvious:

- **The artwork has to be big.** A hairline rule and a 0.67in logo are what a
  plain layout carries, and a graphic resting on one is not sitting on the
  layout.
- **The artwork has to be CLIPPED to the page.** The photograph inside that
  master's arc is a 13.33in-tall picture starting five inches above the top
  edge. Taken at its bounding box it covers everything, and every shape on
  every slide would read as sitting on artwork.

**A single box drawn on top of a region is copied into it.** Runs need three or
more boxes, and a subtitle is one. So a real deck's layout kept its subtitle
region empty and showing its own prompt text, with the deck's subtitle drawn
across the top of it in a loose box: the words on the slide twice over, once as
a prompt and once as content.

One box has no order to read, but it has geometry, and here the geometry is
unambiguous in a way it never was for the agenda. The region is 12.28in wide at
0.69in from the edge; the box is 12.28in wide at 0.67in. The same rectangle,
drawn twice.

**Width decides it and height is deliberately ignored.** Ranking instead by how
much of a region a box covers lets a small caption sitting inside a big empty
content region score perfectly, and a slide of fourteen small boxes over one
region would hand it to whichever happened to win. A box that IS a region's
copy has the region's WIDTH, because that is what it was typed into, and it may
be any height at all -- a one-line prompt strip on the layout holds three lines
of real copy. So: near enough the same width, sitting over it horizontally, and
covering the middle of it vertically. Runs get first refusal, because a run
says what a group of boxes IS where this says only where one of them sits, and
one region only ever takes one box.

One thing it still deliberately does not do:

- **It will not place a loose box that is neither part of a run nor drawn over
  a region.** A box sitting in open space is a judgement call with nothing to
  decide it on. Those placeholders are left empty and every one is listed, so
  the remaining work is visible.
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

**If uploads keep vanishing, move the working directory.** A session's uploaded
deck, its renders and the deck written from it all live in a directory under
the system temporary one. That is right until something else is managing it:
Windows Storage Sense sweeps it on a schedule, and so do the remote-management
agents an IT department installs -- neither asks whether a process is using the
files. On one machine every session directory went while the server was
running, and what reached the log was PowerPoint's

    could not render X.pptx: (-2147352567, 'Exception occurred.',
    (0, None, None, None, 0, -2147024893), None)

which is `0x80070003`, "the system cannot find the path specified", and reads
like a fault in the renderer. It now says which file is gone and why. To stop
it happening, point the sessions somewhere nobody sweeps:

```powershell
$env:FORMATTING_TOOL_WORKDIR = "C:/work/formatting-tool"
python -m formatting_tool ui
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
- **Undo** sits beside every change, in the detail list and under each render,
  and rejecting one is a click rather than a re-tick. It posts to `/api/undo`,
  which replays the same run without that change; the held-back ones are
  listed under the changes with a **Restore** on each, and they accumulate, so
  taking back a second change keeps the first one taken back. Pressing Apply
  again starts a fresh run: the tick list you just sent is what you want.
- **The two changes that will not show on a render say so.** `typography.whitespace`
  is marked *no visible change* and `typography.orphan_widow` *wrap only*: one
  non-breaking space, which shows only if it changes where the line breaks, and
  in a box too narrow for the pair to share a line it shows nowhere at all. Two
  identical pictures otherwise read as a tool that did nothing, and the next
  thing doubted is the rest of the list.

  It is a list of two rules rather than a measurement, and that is deliberate.
  The first attempt asked whether the shape had moved, which is wrong twice
  over: clearing a hardcoded typeface moves nothing and changes every glyph,
  and re-spacing a component's cells moves the other shapes rather than the one
  the finding names. Marking either invisible tells a designer to stop looking
  at the changes most worth looking at.
- **The change list is grouped by what kind of change it is** -- layout,
  titles, logo, typefaces, colour, position and spacing, tables, typography --
  each group folded on its own, and each fold remembered. The same grouping
  appears under each pair of renders, keyed per slide so one slide's Colour
  does not fold with another's. Groups start closed: forty-four changes is a
  wall of text however it is sorted, and the point of the groups is to read as
  six lines saying what kind of work was done. Skipped findings get a group of
  their own rather than a tail.
- **An undone change says what it did**, in the words it was described in when
  it was made: "slide 12 &middot; Picture 3 -- moved it +2.31in across". The
  server cannot supply that, because on the run that holds a change back the
  change is never made and has no outcome; the page keeps it from the run that
  did make it. The status line names the change as well, so an undo is never
  just something disappearing from a list.
- **Re-render** sits over each pair of renders and renders that slide again,
  ignoring what is cached. The cache is right nearly always -- the upload never
  changes, and a run that rewrites the corrected deck drops the after images
  itself -- but "nearly always" is not something a designer can check from the
  outside, and the renders are what they accept the result on.
- **An undo re-renders the slide it happened on**, when renders are already up.
  The file has just been written again, so every after image on the page is of
  a deck that no longer exists.

  **Each run's renders live in a directory of its own**, `after/<run>/`, and
  are served from URLs carrying the run number. The first version of this
  deleted the old images and rendered into the same place, and that cannot be
  relied on: deletion is best-effort, on Windows a file anything still holds
  open will not go, and a directory that did not empty reads as a cache that is
  already full. The page then showed the previous run's pictures beside the new
  deck -- the undo applied, the download correct, and the evidence on screen
  saying it had not happened. A number in the path cannot half-work: a render
  either exists for this run or is made, and nothing older is reachable, by the
  page or by anything caching on its behalf. Sweeping up the old directories is
  housekeeping, so a file that will not go costs disk space rather than
  correctness.

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

### The design check

The second page, at `/qa`, and a different question. The deck check measures a
deck against a master and a brand file and reports what is provably wrong with
it. The design check takes a deck on its own, renders every slide through
PowerPoint, and asks the model the thing no rule reaches: does this look right.

```powershell
python -m formatting_tool ui --reload
# then http://127.0.0.1:8000/qa
```

**What the model is shown is the real slide.** Each slide is exported through
PowerPoint COM at 1536x864 -- the largest 16:9 frame under the 1568px long edge
past which a picture is downsized before it is looked at -- and that PNG, the
render a client would open, is what goes up. The validation layer stays at
1280x720; see `render.DESIGN_QA_SIZE` for why the two differ.

**With it goes a shape map**: every shape on the slide, named, given a role,
and placed in percentages of the page -- `s3. 'Caption 4' (caption): left 8.2%,
top 79.6%, width 22.1%, height 10.0%, 9pt, 'Source: ...'`. The percentages are
the only frame of reference the picture and the file share, and they are what
lets a verdict be anchored to something nameable. Footers, slide numbers and
dates are left out: they are the master's furniture, and a verdict on one is a
verdict on the template.

**The map is a tree, and it goes inside the groups.** A shape in a group is
listed under it, indented, carrying its parent's ref: `s5` is the circle,
`s5.2` is the icon inside it. That is where the defect usually is -- a row of
components each holding an icon, and one icon sitting low inside its circle is
the commonest fault in a deck built from repeated parts. A list that named the
group and stopped had nowhere to put that verdict, so the model said nothing.
`deck_reader` composes a group's children into slide coordinates, so a child's
percentages mean the same thing as a top-level shape's and the two can be
compared directly; a child's own numbers are in a private coordinate system the
group declares, and listing those would put every icon somewhere it is not.
Percentages carry one decimal for the same reason: an icon 1.5% of the slide
out of place is an obvious wobble on the render and rounds to the same whole
number as a centred one.

It stops at two levels and twelve children to a group. An org chart of forty
boxes is an arrangement rather than forty decisions, and listing it would spend
the whole map on one drawing; the group is still named, so the model can still
say the drawing as a whole sits wrong.

**A ref comes back, not a name.** Shape names are not unique on a real slide --
sixteen shapes called `Pentagon 7` is a thing that happens -- so each listed
shape carries a ref, and the response schema will accept nothing else in
`shape`. The model cannot name a shape that was not on the list, because the
schema has no word for one.

**Two buckets come back, and both are kept.**

- **A verdict per shape**, as `{shape, status, issue, action, note, task}`.
  `action` is `shrink`, `grow`, `center`, `widen` or `none`; `issue` is
  `overlap`, `cut_off`, `off_center`, `too_small`, `too_big`, `crowded` or
  `unfilled`. Both are whitelisted in Python on the way in, so a word nobody
  defined reads as unlabelled rather than as a new behaviour. `task` is
  required whenever `status` is `issue`, whatever the action.

  Three of those are worth spelling out. **`cut_off`** covers a word broken
  across lines because its box is too narrow -- "Proportionalit / y" -- as well
  as text clipped by its box: both mean the box cannot show the words it was
  given, and the first is the one a designer sees and no rule measures.
  **`off_center`** is the icon-in-a-circle case above, and is given on the
  child rather than on the group. **`unfilled`** is a placeholder the deck
  never filled, and it is the one verdict the picture cannot support: PowerPoint
  does not export the "Click to add text" prompt, so an unfilled region renders
  as blank space rather than as a mistake. The map marks those `EMPTY` and the
  model is told to trust the mark over the picture for that one.
- **`slide_issues`**, for everything that vocabulary cannot express: a timeline
  with a stop nothing uses, a column left empty, a layout weighted to one side.
  Each is a `{note, task}` pair for the same reason -- the note is what is
  wrong, the task is what to do about it. Dropping these quietly is how a bad
  slide reaches a client.

**Every finding is a task.** Not a note, not an observation: a line that says
what to do, with the slide and the shape it is about. The model is required to
write one for every issue it reports, including the ones nothing here can
correct, because for those the instruction is the entire value of having
reported it. "The right half is empty" is an observation; "run the cards across
the full width, or move the callout into the empty half" is work.

A task is **fixable** when the applier has arithmetic for it. That is a
statement about the tool rather than about the finding, and the page says so
plainly: fixable tasks carry a tick box, the rest carry the instruction and go
into the deck.

**Two kinds of correction, and they are applied by different halves of the
tool.** A designer ticking a row does not need to know which.

*Measured verbs*, where the target is something this tool works out by asking
the renderer. They go through PowerPoint, because that is the only thing that
can say where a line broke or what size a placeholder is actually drawing.

*Proposals*, where the model names an op and the numbers for it. These are
`models.FIX_OPS`, the same vocabulary the rule layer's AI pass has used all
along, so they are validated by `ai/schema.py` and carried out by
`apply/fixers.py` with the guards those already have: a colour must be one the
deck vouches for, a size must sit in the range the deck uses for that role, a
move must land inside the safe margins and survive the overlap check. The
design check adds the eyes; it does not need a second applier.

Two ops are deliberately not offered. `set_font` swaps a typeface, which is a
brand decision rather than a design defect. `remove_note` deletes a shape, and
belongs to the layer that can tell a production note from a caption.

**The deck is its own authority.** `fix_ai_action` refuses every proposal when
there is no brand reference, which is the honest answer when a deck arrives
without a master, and this page never has one: it takes a single deck and asks
whether it holds together. So the reference is derived from the deck itself
with `derive_master_spec`, giving its own palette, its own typefaces and the
size range its own slides use for each role. A colour the deck uses on two or
more slides is added to that palette as well, because the theme is what the
file says and the body copy is what the file does; where the two disagree
about a colour that is on every slide, the file is doing it on purpose.

This is not a brand system and does not pretend to be one. A deck that is
wrong throughout will happily vouch for being wrong consistently, and that is
what the deck check next door is for.

**A cross-slide mismatch is fixable when the deck can be counted.** The model
says which slides disagree and on what; arithmetic says what the majority
does. `position` becomes a move to the median, `type_scale` becomes
`set_font_size` at the most common size for that role, and `color` becomes
`recolor_text` at the most common colour for it. A tie is not a majority: two
slides at 11pt and two at 12pt say the deck has not decided, and picking one
would be this tool deciding for it. `spacing`, `content` and the rest have no
number to count and stay tasks.

Measured on a three-slide deck whose third slide had 7pt body copy where the
others had 12pt, and a red title where the others were near-black:

```
[FIX:recolor_text ] deck:0   Change the title colour on slide 3 to match the others
[FIX:set_font_size] deck:1   Increase the body copy on slide 3 to 12pt to match
...
recolor_text   slide 3 Title 1                recoloured 1 run(s) to deck:17191C
set_font_size  slide 3 Content Placeholder 2  set 3 run(s) to 12pt
grow           slide 1 Content Placeholder 2  stepped the type up from 12pt to 14pt
```

**The measured verbs, each bounded and checked afterwards.**

| | what it does | what stops it |
| --- | --- | --- |
| `shrink` / `grow` | steps the type one notch, x0.85 or x1.15 | the 9pt floor and the 40pt cap; put back if the copy stops fitting |
| `center` | moves a shape to the middle of the thing holding it | refuses when it is not inside anything, or is already centred to within half a point |
| `widen` | widens a box until its words stop breaking in half | a neighbour, the slide edge, or 1.6x; put back if the break survives |
| `align` | moves a shape to where the rest of the deck puts it | fewer than three slides to measure, or a move over 2in |

A step that would pass a bound is not taken at all rather than clipped to it,
since a clipped grow is a shrink and nobody asked for one.

**`widen` re-reads the renderer rather than computing a width.** How wide a
word draws depends on the typeface, the size, the kerning and the language, and
every attempt to derive it from the characters is a guess that is wrong for
Arabic. So the box is widened a little and PowerPoint is asked again where it
broke the lines -- `breaks_mid_word` walks the copy alongside the lines the
renderer produced, and a line that ends where the original has no space is a
word broken in half.

**`align` is the one correction the model does not ask for.** It says which
slides disagree; arithmetic says where the shape belongs -- the median position
of the title across the deck, median rather than mean so the slide being
reported cannot drag the target towards itself. Titles only: a title is the one
shape a deck has on nearly every slide in a role this tool can identify, so
"where the rest of the deck puts it" means something. For a logo that drifts
there is no such set, and the finding stays a task.

**Everything else is written into the deck as a comment.** Ticking Apply does
two things: it makes the corrections, and it files every remaining task as a
PowerPoint comment anchored on the shape it is about. The designer opens the
Comments pane and works the list with the slide in front of them, which is
where the work happens -- not in a browser tab they have to keep in sync by
eye. Three kinds of task end up there, and they are the same thing to whoever
picks the deck up next: what nothing can correct, what was not ticked, and what
was ticked and refused. The third is the one a page loses.

**Shapes are addressed by their position in the tree, not by their id.** A
shape id is unique within a slide in a well-formed file, and this tool reads
the file with python-pptx and edits it through PowerPoint -- two readings that
agree only while the file is well formed. A deck carrying the same id on two
shapes, which is what pasting between decks produces, is renumbered silently by
PowerPoint when it opens it. Measured on such a deck: a group holding an icon
and a text box two shapes away both claimed id 910, and a correction addressed
by id centred the icon on the text box. Document order is the one thing both
readings agree on, so the path is the key and the shape's name is the check; a
lookup that finds a different name touches nothing and says why.

**It steps through PowerPoint, not python-pptx**, because a placeholder usually
states no size of its own: it inherits one from the layout, `run.font.size` is
`None`, and that is most of the shapes on a tidy deck. PowerPoint answers with
the size it is drawing and measures, on the same open presentation, whether the
copy still fits.

**Applying draws the pair.** The corrected slides come back as **Before and
after**: the original beside the rewritten one, each shape that was stepped
marked on both pictures with the number of its line in the list underneath.
"stepped the type down from 11pt to 9.5pt" is a claim; two pictures are the
evidence, and a designer can reject the result before it reaches a client. It
is the same reasoning as **Render before / after** on the deck check, and the
same furniture: a **Re-render** on each slide that ignores what is cached,
because the cache is right nearly always and "nearly always" is not something
anyone can check from the outside; a toggle for the marks; and the download
beside them.

Only slides that actually changed get a pair. A step that was refused -- the
copy would have spilled its box, the shape holds no text, the type is already
at the 9pt floor -- would produce two identical pictures, and two identical
pictures read as a tool that did nothing rather than as a tool that declined to
do something. The refusal is listed in words, with its reason, where it can say
which.

**Everything else is a note.** A shape in the wrong place, a colour fighting
its background, an image at the wrong crop: the model is told to answer `none`
and say so in `note`, and the page lists it beside the render with no tick box.
Ticking something that is a note rather than an action is refused with a
sentence.

**Each verdict is drawn on the slide it is about.** The boxes over the render
are the same rectangles the model was given, in the same percentages, numbered
to match the list underneath.

**And the slides are compared with each other, once.** The per-slide pass
cannot answer this by construction: a title 4% lower than on every other slide
looks perfectly placed on its own, and a deck of individually faultless slides
that do not match each other is exactly what reads as assembled rather than
designed. So the renders are tiled onto contact sheets -- three across at 512px,
twelve to a sheet, which keeps the sheet under the 1568px long edge -- and sent
with a table of what each slide measures: where its title sits, where its
content starts and ends, what type sizes are on it. One call, not the hundred
and thirty-six a pairwise comparison of seventeen slides would be.

What comes back is `{kind, slides, note, task}` per mismatch, `kind` being `position`,
`type_scale`, `spacing`, `color`, `size`, `alignment`, `content` or `other`, and
the page lists them under **Across the deck** with a link to each slide named.
They are notes, never steps: a mismatch has two sides, and which of them is
wrong is a designer's call. A mismatch naming a slide that was not in the
comparison is dropped -- nothing validates these later, so one pointing at
slide 40 of a deck compared up to 36 would send a designer to a slide the model
never saw. The model is told what not to report as well: a cover and a section
divider are not the content slides and are not meant to match them.

**The failure posture is the same as everywhere else here.** No renderer, no
key, a quota spent halfway down a deck: each comes back as a report that says
which, with the slides that did get looked at still on it. A slide the model
declined is called out as declined, because a slide nobody looked at and a
slide with nothing wrong with it look identical on a page.

## Tests

```powershell
pip install pytest
pytest
```

Nine smoke tests covering the rule layer, the AI payload and schema, the merge
step, and colour distance. None of them need python-pptx, an API key, or a
fixture deck.

**The design check's page is tested in a real browser.** `test_qa_page.py` is
the only test of any of the JavaScript here, and it exists because a typo in a
page does not fail anything: it blanks the results and leaves something that
loads, looks calm, and shows nothing. The script is lifted out of `qa.html`,
given a `fetch` that answers with the payloads `web/server.py` actually sends,
and driven through a check, an apply and a re-render in headless Edge or
Chrome; the assertions read the DOM that comes back. The canned payloads are
written out in the test rather than captured to a file because they ARE the
contract between the server and the page, and a change on one side that the
other does not follow should fail there rather than in a designer's browser.
Skipped where no browser is installed.

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
| `formatting_tool/web/` | The browser UI: a stdlib server and two HTML pages. |
| `formatting_tool/designqa.py` | The design check: render a deck, ask what it looks like. |
| `formatting_tool/ai/designqa.py` | The per-slide call, the shape map, the cross-slide pass, and the response contract. |
| `formatting_tool/apply/qafix.py` | The measured corrections, applied through PowerPoint. |
| `formatting_tool/apply/notes.py` | Writes the tasks nothing can fix into the deck as comments. |
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
