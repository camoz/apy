"""An Anki collection wrapper class."""
import os
import re
import tempfile
from pathlib import Path
import sqlite3
import pickle

import click
from bs4 import BeautifulSoup

from apy.config import cfg
from apy.convert import (
    html_to_screen,
    markdown_file_to_notes,
    markdown_to_html,
    plain_to_html,
)
from apy.note import Note
from apy.utilities import cd, choose, editor


class Anki:
    """My Anki collection wrapper class."""

    def __init__(
        self, base_path=None, collection_db_path=None, profile_name=None, **_kwargs
    ):
        self.modified = False

        self._meta = None
        self._collection_db_path = ""
        self._profile_name = profile_name
        self._profile = None

        self._init_load_profile(base_path, collection_db_path)
        self._init_load_collection()
        self._init_load_config()

        self.model_name_to_id = {m["name"]: m["id"] for m in self.col.models.all()}
        self.model_names = self.model_name_to_id.keys()

        self.deck_name_to_id = {d["name"]: d["id"] for d in self.col.decks.all()}
        self.deck_names = self.deck_name_to_id.keys()
        self.n_decks = len(self.deck_names)

    def _init_load_profile(self, base_path, collection_db_path):
        """Load the Anki profile from database"""
        if base_path is None:
            if collection_db_path:
                self._collection_db_path = str(Path(collection_db_path).absolute())
                return
            else:
                click.echo("Base path is not properly set!")
                raise click.Abort()

        base_path = Path(base_path)
        db_path = base_path / "prefs21.db"

        if not db_path.exists():
            click.echo("Invalid base path!")
            click.echo(f"path = {base_path.absolute()}")
            raise click.Abort()

        # Load metadata and profiles from database
        conn = sqlite3.connect(db_path)
        try:
            res = conn.execute(
                "select cast(data as blob) from profiles where name = '_global'"
            )
            self._meta = pickle.loads(res.fetchone()[0])

            profiles = conn.execute(
                "select name, cast(data as blob) from profiles where name != '_global'"
            ).fetchall()
        finally:
            conn.close()

        profiles_dict = {name: pickle.loads(data) for name, data in profiles}

        if self._profile_name is None:
            self._profile_name = self._meta.get(
                "last_loaded_profile_name", profiles[0][0]
            )

        self._collection_db_path = str(
            base_path / self._profile_name / "collection.anki2"
        )
        self._profile = profiles_dict[self._profile_name]

    def _init_load_collection(self):
        """Load the Anki collection"""
        from anki.collection import Collection
        from anki.errors import DBError

        # Save CWD (because Anki changes it)
        save_cwd = os.getcwd()

        try:
            self.col = Collection(self._collection_db_path)
        except AssertionError as error:
            click.echo("Path to database is not valid!")
            click.echo(f"path = {self._collection_db_path}")
            raise click.Abort() from error
        except DBError as error:
            click.echo("Database is NA/locked!")
            raise click.Abort() from error

        # Restore CWD (because Anki changes it)
        os.chdir(save_cwd)

    @staticmethod
    def _init_load_config():
        """Load custom configuration"""
        from anki import latex

        # Update LaTeX commands
        # * Idea based on Anki addon #1546037973 ("Edit LaTeX build process")
        if "pngCommands" in cfg:
            latex.pngCommands = cfg["pngCommands"]
        if "svgCommands" in cfg:
            latex.svgCommands = cfg["svgCommands"]

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        if self.modified:
            click.echo("Database was modified.")
            if self._profile is not None and self._profile["syncKey"]:
                click.secho("Remember to sync!", fg="blue")
            self.col.close()
        elif self.col.db:
            self.col.close(False)

    def sync(self):
        """Sync collection to AnkiWeb"""
        from anki.sync import SyncAuth

        if self._profile is None:
            return

        hkey = self._profile.get("syncKey")
        if not hkey:
            return

        auth = SyncAuth(
            hkey=hkey,
            endpoint=self._profile.get("currentSyncUrl")
            or self._profile.get("customSyncUrl")
            or None,
            io_timeout_secs=self._profile.get("networkTimeout") or 30,
        )

        if auth is None:
            return

        # Make sure database is saved first
        self.col.save(trx=False)

        # Perform main sync
        try:
            debug_output = "anki::sync=debug" in os.environ.get("RUST_LOG", "")

            if debug_output:
                click.secho("Syncing deck:", fg="blue")
            else:
                click.echo("Syncing deck ... ", nl=False)

            self.col.sync_collection(auth)

            if not debug_output:
                click.echo("done!")
            else:
                click.echo("")
        except Exception as e:
            click.secho("Error during sync!", fg="red")
            click.echo(e)
            raise click.Abort()

        # Perform media sync
        try:
            debug_output = "media=debug" in os.environ.get("RUST_LOG", "")

            with cd(self.col.media.dir()):
                if debug_output:
                    click.secho("Syncing media:", fg="blue")
                else:
                    click.echo("Syncing media ... ", nl=False)
                self.col.sync_media(auth)
                if not debug_output:
                    click.echo("done!")
        except Exception as e:
            if "sync cancelled" in str(e):
                return
            raise

    def check_media(self):
        """Check media (will rebuild missing LaTeX files)"""
        with cd(self.col.media.dir()):
            click.echo("Checking media DB ... ", nl=False)
            output = self.col.media.check()
            click.echo("done!")

            if len(output.missing) + len(output.unused) == 0:
                click.secho("No unused or missing files found.", fg="white")
                return

            for file in output.missing:
                click.secho(f"Missing: {file}", fg="red")

            if len(output.missing) > 0 and click.confirm("Render missing LaTeX?"):
                out = self.col.media.render_all_latex()
                if out is not None:
                    nid, _ = out
                    click.secho(f"Error processing node: {nid}", fg="red")

                    if click.confirm("Review note?"):
                        note = Note(self, self.col.get_note(nid))
                        note.review()

            for file in output.unused:
                click.secho(f"Unused: {file}", fg="red")

            if len(output.unused) > 0 and click.confirm("Delete unused media?"):
                for file in output.unused:
                    if os.path.isfile(file):
                        os.remove(file)

    def find_cards(self, query):
        """Find card ids in Collection that match query"""
        return self.col.find_cards(query)

    def find_notes(self, query):
        """Find notes in Collection and return Note objects"""
        return (
            Note(self, self.col.get_note(i)) for i in set(self.col.find_notes(query))
        )

    def delete_notes(self, ids):
        """Delete notes by note ids"""
        if not isinstance(ids, list):
            ids = [ids]

        self.col.remove_notes(ids)
        self.modified = True

    def get_model(self, model_name):
        """Get model from model name"""
        return self.col.models.get(self.model_name_to_id.get(model_name))

    def set_model(self, model_name):
        """Set current model based on model name"""
        current = self.col.models.current(for_deck=False)
        if current["name"] == model_name:
            return current

        model = self.get_model(model_name)
        if model is None:
            click.secho(f'Model "{model_name}" was not recognized!')
            raise click.Abort()

        self.col.models.set_current(model)
        return model

    def rename_model(self, old_model_name, new_model_name):
        """Rename a model"""
        if old_model_name not in self.model_names:
            click.echo("Can" "t rename model!")
            click.echo(f"No such model: {old_model_name}")
            raise click.Abort()

        # Change the name
        model = self.get_model(old_model_name)
        model["name"] = new_model_name

        # Update local storage
        self.model_name_to_id = {m["name"]: m["id"] for m in self.col.models.all()}
        self.model_names = self.model_name_to_id.keys()

        # Save changes
        self.col.models.update_dict(model)
        self.modified = True

    def list_tags(self):
        """List all tags"""
        tags = [(t, len(self.col.find_notes(f"tag:{t}"))) for t in self.col.tags.all()]
        width = len(max(tags, key=lambda x: len(x[0]))[0]) + 2
        filler = " " * (cfg["width"] - 2 * width - 8)

        for (t1, n1), (t2, n2) in zip(
            sorted(tags, key=lambda x: x[0]), sorted(tags, key=lambda x: x[1])
        ):
            click.echo(f"{t1:{width}s}{n1:4d}{filler}{t2:{width}s}{n2:4d}")

    def change_tags(self, query, tags, add=True):
        """Add/Remove tags from notes that match query"""
        self.col.tags.bulkAdd(self.col.find_notes(query), tags, add)
        self.modified = True

    def edit_model_css(self, model_name):
        """Edit the CSS part of a given model."""
        model = self.get_model(model_name)

        with tempfile.NamedTemporaryFile(
            mode="w+", prefix="_apy_edit_", suffix=".css", delete=False
        ) as tf:
            tf.write(model["css"])
            tf.flush()

            retcode = editor(tf.name)
            if retcode != 0:
                click.echo(f"Editor return with exit code {retcode}!")
                return

            with open(tf.name, "r", encoding="utf8") as f:
                new_content = f.read()

        if model["css"] != new_content:
            model["css"] = new_content
            self.col.models.save(model, templates=True)
            self.modified = True

    def list_notes(self, query, verbose=False):
        """List notes that match a query"""
        for note in self.find_notes(query):
            first_field = html_to_screen(note.n.values()[0])
            first_field = first_field.replace("\n", " ")
            first_field = re.sub(r"\s\s\s+", " ", first_field)
            first_field = first_field[: cfg["width"] - 14] + click.style("", reset=True)

            first = "Q: "
            if note.suspended:
                first = click.style(first, fg="red")
            elif "marked" in note.n.tags:
                first = click.style(first, fg="yellow")

            click.echo(f"{first}{first_field}")
            if verbose:
                click.echo(f"model: {note.model_name}\n")

    def list_cards(self, query, verbose=False):
        """List cards that match a query"""
        for cid in self.find_cards(query):
            c = self.col.get_card(cid)
            question = BeautifulSoup(html_to_screen(c.question()), features="html5lib")
            question = re.sub(
                r"\s\s+", " ", question.get_text().replace("\n", " ").strip()
            )
            answer = BeautifulSoup(html_to_screen(c.answer()), features="html5lib")
            answer = re.sub(r"\s\s+", " ", answer.get_text().replace("\n", " ").strip())

            def _styled(key, value):
                """Simple convenience printer."""
                return click.style(key + ": ", fg="yellow") + str(value)

            cardtype = int(c.type)
            card_type = ["new", "learning", "review", "relearning"][cardtype]

            click.echo(_styled("Q", question[: cfg["width"]]))
            if verbose:
                click.echo(_styled("A", answer[: cfg["width"]]))

                click.echo(
                    f"{_styled('model', c.note_type()['name'])} "
                    f"{_styled('type', card_type)} "
                    f"{_styled('ease', c.factor/10)}% "
                    f"{_styled('lapses', c.lapses)}\n"
                    f"{_styled('cid', cid)} "
                    f"{_styled('due', c.due)}\n"
                )

    def add_notes_with_editor(
        self, tags="", model_name=None, deck_name=None, template=None
    ):
        """Add new notes to collection with editor"""
        if isinstance(template, Note):
            input_string = template.get_template()
        else:
            if model_name is None or model_name.lower() == "ask":
                model_name = choose(sorted(self.model_names), "Choose model:")

            model = self.set_model(model_name)

            if deck_name is None:
                deck_name = self.col.decks.current()["name"]
            elif deck_name.lower() == "ask":
                deck_name = choose(sorted(self.deck_names), "Choose deck:")

            input_string = [f"model: {model_name}"]

            if self.n_decks > 1:
                input_string += [f"deck: {deck_name}"]

            input_string += [f"tags: {tags}"]

            if model_name not in cfg["markdown_models"]:
                input_string += ["markdown: false"]

            input_string += ["\n# Note\n"]

            input_string += [
                x
                for y in [[f'## {field["name"]}', ""] for field in model["flds"]]
                for x in y
            ]

            input_string = "\n".join(input_string) + "\n"

        with tempfile.NamedTemporaryFile(
            mode="w+", prefix="apy_note_", suffix=".md", delete=False
        ) as tf:
            tf.write(input_string)
            tf.flush()
            retcode = editor(tf.name)

            if retcode != 0:
                click.echo(f"Editor return with exit code {retcode}!")
                return []

            return self.add_notes_from_file(tf.name)

    def add_notes_from_file(self, filename, tags=""):
        """Add new notes to collection from Markdown file"""
        return self.add_notes_from_list(markdown_file_to_notes(filename), tags)

    def add_notes_from_list(self, parsed_notes, tags=""):
        """Add new notes to collection from note list (from parsed file)"""
        notes = []
        for note in parsed_notes:
            model_name = note["model"]
            model = self.set_model(model_name)
            model_field_names = [field["name"] for field in model["flds"]]

            field_names = note["fields"].keys()
            field_values = note["fields"].values()

            if len(field_names) != len(model_field_names):
                click.echo(f"Error: Not enough fields for model {model_name}!")
                self.modified = False
                raise click.Abort()

            for x, y in zip(model_field_names, field_names):
                if x != y:
                    click.echo("Warning: Inconsistent field names " f"({x} != {y})")

            notes.append(
                self._add_note(
                    field_values,
                    f"{tags} {note['tags']}",
                    note["markdown"],
                    note.get("deck"),
                )
            )

        return notes

    def add_notes_single(self, fields, tags="", model=None, deck=None):
        """Add new note to collection from args"""
        if model is not None:
            self.set_model(model)

        self._add_note(fields, tags, False, deck)

    def _add_note(self, fields, tags, markdown=True, deck=None):
        """Add new note to collection"""
        notetype = self.col.models.current(for_deck=False)
        note = self.col.new_note(notetype)

        if deck is not None:
            note.note_type()["did"] = self.deck_name_to_id[deck]

        if markdown:
            note.fields = [markdown_to_html(x) for x in fields]
        else:
            note.fields = [plain_to_html(x) for x in fields]

        tags = tags.strip().split()
        for tag in tags:
            note.add_tag(tag)

        if not note.dupeOrEmpty():
            self.col.addNote(note)
            self.modified = True
        else:
            click.secho("Dupe detected, note was not added!", fg="red")
            click.echo("Question:")
            click.echo(list(fields)[0])

        return Note(self, note)