---
name: pdf-from-markdown
description: Convert markdown to PDF using pandoc. No script — pandoc on PATH plus the right `execute` invocation is the entire workflow.
---

# PDF from markdown

Renders a markdown file to a polished PDF using pandoc. Most agent
sessions that produce a written report want to hand the user a PDF
at the end; this skill is the canonical "how do I do that" rather
than re-deriving pandoc invocations from scratch each time.

## Requirements

`pandoc` on PATH. If you run a pandoc command via `execute` and see
``pandoc: command not found``, the user needs to install it:

- macOS: ``brew install pandoc``
- Debian / Ubuntu: ``apt install pandoc``
- Other Linux: distro package manager, or download from
  <https://pandoc.org/installing.html>

For the academic-styling invocation below, pandoc also needs a LaTeX
engine. ``texlive-xetex`` (Linux) / MacTeX (macOS) covers it.

## How to use this skill

There is no Python script. You invoke pandoc directly via `execute`
with a few well-tested flag sets. Pick the invocation that matches
the user's intent.

### Default — clean general-purpose layout

```
pandoc <input.md> -o <output.pdf>
```

Uses pandoc's bundled LaTeX engine. Decent typography out of the
box — titles, headings, code blocks, tables all render cleanly.
Right for memos, reports, summaries, anything that doesn't need a
specific look.

### Polished — system font, comfortable margins

```
pandoc <input.md> -o <output.pdf> \
    --pdf-engine=xelatex \
    -V geometry:margin=1in \
    -V mainfont="DejaVu Sans"
```

Use when the user wants something that looks more like a finished
deliverable. ``mainfont`` accepts any installed system font — pick
one the platform actually has (DejaVu Sans is a safe default on
Linux; ``Helvetica`` on macOS).

### Academic — LaTeX styling, two-column-friendly

```
pandoc <input.md> -o <output.pdf> --pdf-engine=pdflatex
```

Crisp serif typography, justified text, classic LaTeX article look.
Use for research writeups, papers, anything where the user expects
academic formatting.

## Where to save the output

Save the PDF into the current session's attachments directory so it
sticks with the conversation and gets cleaned up alongside other
session artifacts:

```
.pyagent/sessions/<session-id>/attachments/<your-name>.pdf
```

If you don't already know the session id, list the directory
``.pyagent/sessions/`` and pick the most recent. Or save into the
user's working directory and tell them the path — that's also fine
for one-off requests.

## Troubleshooting

- ``pandoc: command not found`` — install pandoc (see Requirements).
- ``! LaTeX Error`` from the polished or academic invocations — the
  LaTeX engine isn't installed. Either install it (``texlive-xetex``,
  MacTeX) or fall back to the default invocation, which uses
  pandoc's bundled engine.
- ``! Package fontspec Error: The font ... cannot be found`` — the
  ``mainfont`` you picked isn't installed. Drop the ``-V mainfont``
  flag, or pick one ``fc-list | head`` shows.

## When NOT to use this

If the user wants a structured PDF *generated from data* (e.g.,
"build me a report with these tables and charts"), this skill is
the wrong shape — produce the markdown first (or build directly
with reportlab / WeasyPrint in a one-off Python script), then run
the markdown through here. This skill is markdown-in, PDF-out only.
