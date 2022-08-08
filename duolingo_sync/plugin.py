import requests.exceptions
from typing import Optional, List
import time
from collections import defaultdict

from aqt import mw
from aqt.utils import showInfo, askUser, showWarning
from aqt.qt import *
import aqt
from anki.collection import Collection
from aqt.operations import QueryOp, CollectionOp
from aqt.utils import showInfo
from aqt import mw
# import the main window object (mw) from aqt
from aqt import mw
# import the "show info" tool from utils.py
from aqt.utils import showInfo, qconnect
# import all of the Qt GUI library
from aqt.qt import *
from dataclasses import dataclass, field

from anki.utils import splitFields, ids2str

from .duolingo_display_login_dialog import duolingo_display_login_dialog
from .duolingo import Duolingo, LoginFailedException
from .duolingo_model import get_duolingo_model
from .duolingo_thread import DuolingoThread

WORD_CHUNK_SIZE = 50
ADD_STATUS_TEMPLATE = "Importing from Duolingo: {} of {} complete."


@dataclass
class VocabRetrieveResult:
    success: bool = False
    words_to_add: list = field(default_factory=list)
    language_string: Optional[str] = None
    lingo: Optional[Duolingo] = None


@dataclass
class AddVocabResult:
    notes_added: int = 0
    problem_vocabs: List[str] = field(default_factory=list)


def init(mw):
    model = get_duolingo_model(mw)

    if not model:
        showWarning("Could not find or create Duolingo Sync note type.")
        return

    note_ids = mw.col.findNotes('tag:duolingo_sync')
    notes = mw.col.db.list("select flds from notes where id in {}".format(ids2str(note_ids)))
    gids_to_notes = {splitFields(note)[0]: note for note in notes}

    return gids_to_notes


def login_and_retrieve_vocab(username, password) -> VocabRetrieveResult:
    result = VocabRetrieveResult(success=False, words_to_add=[])

    model = get_duolingo_model(mw)

    note_ids = mw.col.findNotes('tag:duolingo_sync')
    notes = mw.col.db.list("select flds from notes where id in {}".format(ids2str(note_ids)))
    gids_to_notes = {splitFields(note)[0]: note for note in notes}

    try:
        aqt.mw.taskman.run_on_main(
            lambda: aqt.mw.progress.update(
                label=f"Logging in...",
            )
        )

        lingo = Duolingo(username, password)

        aqt.mw.taskman.run_on_main(
            lambda: aqt.mw.progress.update(
                label=f"Retrieving vocabulary...",
            )
        )
        vocabulary_response = lingo.get_vocabulary()

    except LoginFailedException:
        aqt.mw.taskman.run_on_main(
            lambda: showWarning(
                """
                <p>Logging in to Duolingo failed. Please check your Duolingo credentials.</p>

                <p>Having trouble logging in? You must use your <i>Duolingo</i> username and password.
                You <i>can't</i> use your Google or Facebook credentials, even if that's what you use to
                sign in to Duolingo.</p>

                <p>You can find your Duolingo username at
                <a href="https://www.duolingo.com/settings">https://www.duolingo.com/settings</a> and you
                can create or set your Duolingo password at
                <a href="https://www.duolingo.com/settings/password">https://www.duolingo.com/settings/password</a>.</p>
                """
            )
        )
        return result
    except requests.exceptions.ConnectionError:
        aqt.mw.taskman.run_on_main(
            lambda: showWarning("Could not connect to Duolingo. Please check your internet connection.")
        )
        return result

    language_string = vocabulary_response['language_string']
    vocabs = vocabulary_response['vocab_overview']

    did = mw.col.decks.id("Default")
    mw.col.decks.select(did)

    deck = mw.col.decks.get(did)
    deck['mid'] = model['id']
    mw.col.decks.save(deck)

    words_to_add = [vocab for vocab in vocabs if vocab['id'] not in gids_to_notes]
    result.success = True
    result.words_to_add = words_to_add
    result.language_string = language_string
    result.lingo = lingo

    return result


def on_success(*args, **kwargs) -> None:
    showInfo(f"my_background_op() returned.")


def on_add_success(add_result: AddVocabResult) -> None:
    message = "{} notes added.".format(add_result.notes_added)

    if add_result.problem_vocabs:
        message += " Failed to add: " + ", ".join(add_result.problem_vocabs)

    showInfo(message)
    mw.moveToState("deckBrowser")


def add_vocab(retrieve_result: VocabRetrieveResult) -> AddVocabResult:
    result = AddVocabResult()

    total_word_count = len(retrieve_result.words_to_add)
    word_chunks = [retrieve_result.words_to_add[x:x + WORD_CHUNK_SIZE] for x in range(0, total_word_count, WORD_CHUNK_SIZE)]
    lingo = retrieve_result.lingo

    aqt.mw.taskman.run_on_main(
        lambda: mw.progress.update(label=ADD_STATUS_TEMPLATE.format(0, total_word_count), value=0, max=total_word_count)
    )

    words_processed = 0
    for word_chunk in word_chunks:
        lexeme_ids = {vocab['word_string']: vocab['id'] for vocab in word_chunk}
        translations = lingo.get_translations([vocab['word_string'] for vocab in word_chunk])

        # The `get_translations` endpoint might not always return a translation. In this case, try
        # a couple of fallback methods
        for word_string, translation in translations.items():
            if not translation:
                fallback_translation = "Translation not found for '{}'. Edit this card to add it.".format(word_string)
                try:
                    new_translation = lingo.get_word_definition_by_id(lexeme_ids[word_string])['translations']
                except Exception:
                    new_translation = fallback_translation

                translations[word_string] = [new_translation if new_translation else fallback_translation]

        for vocab in word_chunk:
            n = mw.col.newNote()

            # Update the underlying dictionary to accept more arguments for more customisable cards
            n._fmap = defaultdict(str, n._fmap)

            n['Gid'] = vocab['id']
            n['Gender'] = vocab['gender'] if vocab['gender'] else ''
            n['Source'] = '; '.join(translations[vocab['word_string']])
            n['Target'] = vocab['word_string']
            n['Pronunciation'] = vocab['normalized_string'].strip()
            n['Target Language'] = retrieve_result.language_string
            n.addTag(retrieve_result.language_string)
            n.addTag('duolingo_sync')

            if vocab['pos']:
                n.addTag(vocab['pos'])

            if vocab['skill']:
                n.addTag(vocab['skill'].replace(" ", "-"))

            num_cards = mw.col.addNote(n)

            if num_cards:
                result.notes_added += 1
            else:
                result.problem_vocabs.append(vocab['word_string'])
            words_processed += 1

            aqt.mw.taskman.run_on_main(
                lambda: mw.progress.update(label=ADD_STATUS_TEMPLATE.format(result.notes_added, total_word_count), value=words_processed, max=total_word_count)
            )

    aqt.mw.taskman.run_on_main(
        lambda: mw.progress.finish()
    )

    return result


def on_retrieve_success(retrieve_result: VocabRetrieveResult):
    if not retrieve_result.success:
        return

    if not retrieve_result.words_to_add:
        showInfo(f"Successfully logged in to Duolingo, but no new words found in {retrieve_result.language_string} language.")
    elif askUser(f"Add {len(retrieve_result.words_to_add)} notes from {retrieve_result.language_string} language?"):
        op = QueryOp(
            # the active window (main window in this case)
            parent=mw,
            # the operation is passed the collection for convenience; you can
            # ignore it if you wish
            op=lambda col: add_vocab(retrieve_result),
            # op=lambda col: my_background_op("foo"),
            # this function will be called if op completes successfully,
            # and it is given the return value of the op
            success=on_add_success,
        )

        # if with_progress() is not called, no progress window will be shown.
        # note: QueryOp.with_progress() was broken until Anki 2.1.50
        op.with_progress(label=ADD_STATUS_TEMPLATE.format(0, len(retrieve_result.words_to_add))).run_in_background()
        return 1


def new_sync_duolingo():
    # gids_to_notes = init(mw)
    #
    # try:
    #     username, password = duolingo_display_login_dialog(mw)
    # except TypeError:
    #     return
    #
    # if not username or not password:
    #     return

    username = "foo"
    password = "bar"

    op = QueryOp(
        # the active window (main window in this case)
        parent=mw,
        # the operation is passed the collection for convenience; you can
        # ignore it if you wish
        op=lambda col: login_and_retrieve_vocab(username, password),
        # this function will be called if op completes successfully,
        # and it is given the return value of the op
        success=lambda *args, **kwargs: on_success,
    )

    # if with_progress() is not called, no progress window will be shown.
    # note: QueryOp.with_progress() was broken until Anki 2.1.50
    op.with_progress(label="Logging in...").run_in_background()

def sync_duolingo():
    model = get_duolingo_model(mw)

    if not model:
        showWarning("Could not find or create Duolingo Sync note type.")
        return

    note_ids = mw.col.findNotes('tag:duolingo_sync')
    notes = mw.col.db.list("select flds from notes where id in {}".format(ids2str(note_ids)))
    gids_to_notes = {splitFields(note)[0]: note for note in notes}
    try:
        username, password = duolingo_display_login_dialog(mw)
    except TypeError:
        return

    if username and password:
        try:
            mw.progress.start(immediate=True, label="Logging in...")

            login_thread = DuolingoThread(target=Duolingo, args=(username, password))
            login_thread.start()
            while login_thread.is_alive():
                time.sleep(.02)
                mw.progress.update()
            lingo = login_thread.join()

            vocabulary_thread = DuolingoThread(target=lingo.get_vocabulary)
            vocabulary_thread.start()
            mw.progress.update(label="Retrieving vocabulary...")
            while vocabulary_thread.is_alive():
                time.sleep(.02)
                mw.progress.update()
            vocabulary_response = vocabulary_thread.join()

        except LoginFailedException:
            showWarning(
                """
                <p>Logging in to Duolingo failed. Please check your Duolingo credentials.</p>

                <p>Having trouble logging in? You must use your <i>Duolingo</i> username and password.
                You <i>can't</i> use your Google or Facebook credentials, even if that's what you use to
                sign in to Duolingo.</p>

                <p>You can find your Duolingo username at
                <a href="https://www.duolingo.com/settings">https://www.duolingo.com/settings</a> and you
                can create or set your Duolingo password at
                <a href="https://www.duolingo.com/settings/password">https://www.duolingo.com/settings/password</a>.</p>
                """
            )
            return
        except requests.exceptions.ConnectionError:
            showWarning("Could not connect to Duolingo. Please check your internet connection.")
            return
        finally:
            mw.progress.finish()

        language_string = vocabulary_response['language_string']
        vocabs = vocabulary_response['vocab_overview']

        did = mw.col.decks.id("Default")
        mw.col.decks.select(did)

        deck = mw.col.decks.get(did)
        deck['mid'] = model['id']
        mw.col.decks.save(deck)

        words_to_add = [vocab for vocab in vocabs if vocab['id'] not in gids_to_notes]

        if not words_to_add:
            showInfo("Successfully logged in to Duolingo, but no new words found in {} language.".format(language_string))
        elif askUser("Add {} notes from {} language?".format(len(words_to_add), language_string)):

            word_chunks = [words_to_add[x:x + 50] for x in range(0, len(words_to_add), 50)]

            mw.progress.start(immediate=True, label="Importing from Duolingo...", max=len(words_to_add))
            notes_added = 0
            problem_vocabs = []
            for word_chunk in word_chunks:
                lexeme_ids = {vocab['word_string']: vocab['id'] for vocab in word_chunk}
                translations = lingo.get_translations([vocab['word_string'] for vocab in word_chunk])

                # The `get_translations` endpoint might not always return a translation. In this case, try
                # a couple of fallback methods
                for word_string, translation in translations.items():
                    if not translation:
                        fallback_translation = "Translation not found for '{}'. Edit this card to add it.".format(word_string)
                        try:
                            new_translation = lingo.get_word_definition_by_id(lexeme_ids[word_string])['translations']
                        except Exception:
                            new_translation = fallback_translation

                        translations[word_string] = [new_translation if new_translation else fallback_translation]

                for vocab in word_chunk:

                    n = mw.col.newNote()

                    # Update the underlying dictionary to accept more arguments for more customisable cards
                    n._fmap = defaultdict(str, n._fmap)

                    n['Gid'] = vocab['id']
                    n['Gender'] = vocab['gender'] if vocab['gender'] else ''
                    n['Source'] = '; '.join(translations[vocab['word_string']])
                    n['Target'] = vocab['word_string']
                    n['Pronunciation'] = vocab['normalized_string'].strip()
                    n['Target Language'] = language_string
                    n.addTag(language_string)
                    n.addTag('duolingo_sync')

                    if vocab['pos']:
                        n.addTag(vocab['pos'])

                    if vocab['skill']:
                        n.addTag(vocab['skill'].replace(" ", "-"))

                    num_cards = mw.col.addNote(n)

                    if num_cards:
                        notes_added += 1
                    else:
                        problem_vocabs.append(vocab['word_string'])

                    mw.progress.update(value=notes_added)

            message = "{} notes added.".format(notes_added)

            if problem_vocabs:
                message += " Failed to add: " + ", ".join(problem_vocabs)

            mw.progress.finish()

            showInfo(message)

            mw.moveToState("deckBrowser")


# action = QAction("Pull from Duolingo", mw)
# qconnect(action.triggered, new_sync_duolingo)
# mw.form.menuTools.addAction(action)


import aqt
from anki.collection import Collection
from aqt.operations import QueryOp
from aqt.utils import showInfo
from aqt import mw
# import the main window object (mw) from aqt
from aqt import mw
# import the "show info" tool from utils.py
from aqt.utils import showInfo, qconnect
# import all of the Qt GUI library
from aqt.qt import *

import time

# def my_background_op(col: Collection, note_ids: list[int]) -> int:
def my_background_op(s: str):

        for i in range(10):
            aqt.mw.taskman.run_on_main(
                lambda: aqt.mw.progress.update(
                    label=f"Remaining {s}:",
                    value=i,
                    max=10,
                )
            )
            time.sleep(.5)

        return True

# def my_ui_action(note_ids: list[int]):
def my_ui_action():
    try:
        username, password = duolingo_display_login_dialog(mw)
    except TypeError:
        return

    op = QueryOp(
        # the active window (main window in this case)
        parent=mw,
        # the operation is passed the collection for convenience; you can
        # ignore it if you wish
        op=lambda col: login_and_retrieve_vocab(username, password),
        # op=lambda col: my_background_op("foo"),
        # this function will be called if op completes successfully,
        # and it is given the return value of the op
        success=on_retrieve_success,
    )

    # if with_progress() is not called, no progress window will be shown.
    # note: QueryOp.with_progress() was broken until Anki 2.1.50
    op.with_progress(label="Logging in...").run_in_background()

action = QAction("Pull from Duolingo", mw)
qconnect(action.triggered, my_ui_action)
# action.triggered.connect(my_ui_action)
mw.form.menuTools.addAction(action)
