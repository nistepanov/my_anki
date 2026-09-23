"""Every stage that turns a word list into an Anki deck.

Listed alphabetically rather than in running order: the stages no longer form one line. Some
run on every build, some answer a question a reviewer asked, and some import one source once.
`build.py` states the order a build actually takes.
"""

from . import language_config
from . import anki
from . import book_import
from . import context_cards
from . import dictionaries
from . import duplicates
from . import examples
from . import frequency
from . import goethe
from . import graded_lexicon
from . import images
from . import inflections
from . import llm
from . import mastered
from . import media
from . import model
from . import ordering
from . import picture_dictionary
from . import preview
from . import primary_sense
from . import relations
from . import reword
from . import reword_media
from . import sense_choice
from . import sense_pruning
from . import senses
from . import tasks
from . import transcription
from . import translations
from . import wordlist

__all__ = [
    'anki',
    'book_import',
    'context_cards',
    'dictionaries',
    'duplicates',
    'examples',
    'frequency',
    'goethe',
    'graded_lexicon',
    'images',
    'inflections',
    'language_config',
    'llm',
    'mastered',
    'media',
    'model',
    'ordering',
    'picture_dictionary',
    'preview',
    'primary_sense',
    'relations',
    'reword',
    'reword_media',
    'sense_choice',
    'sense_pruning',
    'senses',
    'tasks',
    'transcription',
    'translations',
    'wordlist',
]
