"""Render sample cards to a local HTML file, so the design can be judged without Anki.

Substitutes the real card templates against real rows and shows both card types in both
themes side by side. Regenerate after editing anything under templates/.
"""

import argparse
import base64
import mimetypes
import pathlib
import re
import typing

from . import language_config
from . import anki

CONDITIONAL_PATTERN = re.compile(r'\{\{([#^])(\w+)\}\}(.*?)\{\{/\2\}\}', re.DOTALL)
# A section keeps its body when the field is filled; the other marker keeps it when empty.
FILLED_SECTION_MARKER = '#'
FIELD_PATTERN = re.compile(r'\{\{(?:(\w+):)?(\w+)\}\}')
# Anki turns a hint into a link plus the text it reveals; the revealed state is the one
# worth looking at, so the preview shows both at once.
HINT_TEMPLATE = '<a class="hint" href="#">Show Hint</a><div class="hint">{value}</div>'
FRONT_SIDE_PLACEHOLDER = 'FrontSide'
IMAGE_PATTERN = re.compile(r'<img src="([^"]+)">')
SOUND_PATTERN = re.compile(r'\[sound:([^\]]+)\]')
# Anki renders a sound tag as a replay button; stand in for it so the layout is honest.
SOUND_PLACEHOLDER = '<span class="replay">&#9654;</span>'

PAGE_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>Card preview</title>
<style>
{css}
body {{ background: #8a8a8a; margin: 0; padding: 2rem; }}
.grid {{ display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); margin: 0 auto; max-width: 1400px; }}
.pane {{ border-radius: 0.6rem; overflow: hidden; }}
.pane h2 {{ background: #2b2b2b; color: #fff; font: 600 0.75rem/1 sans-serif;
           letter-spacing: 0.08em; margin: 0; padding: 0.6rem 0.8rem; text-transform: uppercase; }}
.replay {{ align-items: center; background: var(--accent); border-radius: 999px; color: var(--bg);
          display: inline-flex; font-size: 1rem; height: 44px; justify-content: center; width: 44px; }}
</style>
<div class="grid">{panes}</div>
"""


class TemplateRenderer:
    """Anki's template syntax, reduced to what these cards actually use."""

    @classmethod
    def render(cls, *, template: str, fields: typing.Dict[str, str]) -> str:
        # One pass resolves only the outermost conditional: what it substitutes in is never
        # rescanned, so a conditional nested inside it survives and reaches the card as its own
        # literal markup. Repeating until nothing changes matches what Anki itself does. Each
        # pass removes at least one pair, so this terminates.
        rendered = template
        while True:
            resolved = CONDITIONAL_PATTERN.sub(
                lambda match: cls._section(match=match, fields=fields), rendered,
            )
            if resolved == rendered:
                break
            rendered = resolved
        return FIELD_PATTERN.sub(lambda match: cls._field(match=match, fields=fields), rendered)

    @staticmethod
    def _section(*, match: re.Match, fields: typing.Dict[str, str]) -> str:
        """A card that falls back to another field when one is empty needs both forms rendered.

        Leaving the negated form out does not drop the fallback, it prints the markup around it.
        """
        marker, name, body = match.group(1), match.group(2), match.group(3)
        filled = bool(fields.get(name))
        keep = filled if marker == FILLED_SECTION_MARKER else not filled
        return body if keep else ''

    @staticmethod
    def _field(*, match: re.Match, fields: typing.Dict[str, str]) -> str:
        name_filter, name = match.group(1), match.group(2)
        value = fields.get(name, '')
        if name_filter == 'hint':
            return HINT_TEMPLATE.format(value=value) if value else ''
        return value

    @staticmethod
    def inline_media(*, markup: str, directory: pathlib.Path) -> str:
        """Anki resolves media names against its own folder; a browser needs the bytes inline."""
        def embed(match: re.Match) -> str:
            path = directory / match.group(1)
            if not path.exists():
                return ''
            media_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            encoded = base64.b64encode(path.read_bytes()).decode('ascii')
            return f'<img src="data:{media_type};base64,{encoded}">'

        return SOUND_PATTERN.sub(SOUND_PLACEHOLDER, IMAGE_PATTERN.sub(embed, markup))

    @classmethod
    def render_card(cls, *, design: anki.CardDesign, name: str, fields: dict) -> typing.Tuple[str, str]:
        sides = design.templates[name]
        front = cls.render(template=sides['Front'], fields=fields)
        back = cls.render(
            template=sides['Back'].replace(f'{{{{{FRONT_SIDE_PLACEHOLDER}}}}}', front),
            fields=fields,
        )
        return front, back


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=pathlib.Path, default=None)
    parser.add_argument('--output', type=pathlib.Path, default=pathlib.Path('card_preview.html'))
    parser.add_argument('--word', action='append', help="Headword to preview; repeatable")
    language_config.add_language_arguments(parser)
    arguments = parser.parse_args()

    root = language_config.PROJECT_ROOT
    language = language_config.language_from(arguments, root=root)
    input_path = arguments.input or language.data_directory(root=root) / 'cards.tsv'
    design = anki.CardDesign(
        directory=root / anki.TEMPLATE_DIRECTORY,
        labels=language.labels,
        variant=language.target,
        card_templates=language.card_templates,
    )

    # A folded-away row is not a card any more, and previewing one shows a card the deck does not
    # have.
    rows = [
        row for row in language_config.TsvFile.read(input_path)
        if not row.get(anki.MERGED_COLUMN, '').strip()
    ]
    if arguments.word:
        wanted = {word.lower() for word in arguments.word}
        rows = [row for row in rows if row['word'].lower() in wanted]
    rows = rows[:3]

    media = anki.MediaLibrary.load(data_directory=language.data_directory(root=root))
    media_directory = language.data_directory(root=root) / anki.MEDIA_DIRECTORY
    pusher = anki.NotePusher(client=None, language=language, media=media)
    panes = []
    for row in rows:
        fields = pusher.build_fields(row=row)
        fields.update(media.fields_for(key=row['key']))
        for card_name in design.templates:
            front, back = TemplateRenderer.render_card(design=design, name=card_name, fields=fields)
            front = TemplateRenderer.inline_media(markup=front, directory=media_directory)
            back = TemplateRenderer.inline_media(markup=back, directory=media_directory)
            for theme, body_class in (("light", ''), ("dark", ' nightMode')):
                for side, markup in (("front", front), ("back", back)):
                    panes.append(
                        f'<div class="pane"><h2>{row["word"]} · {card_name} · {side} · {theme}</h2>'
                        f'<div class="card{body_class}">{markup}</div></div>'
                    )

    arguments.output.write_text(
        PAGE_TEMPLATE.format(css=design.css, panes=''.join(panes)),
        encoding='utf-8',
    )
    print(f"wrote {arguments.output} — {len(panes)} panes from {len(rows)} words")


if __name__ == '__main__':
    main()
