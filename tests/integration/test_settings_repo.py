"""SettingsRepository contre un vrai Postgres.

Le repo tient en trente lignes, mais son écriture est un `INSERT ... ON
CONFLICT (key) DO UPDATE` : c'est le moteur, et lui seul, qui décide si la
deuxième écriture d'une clé remplace la première ou fait remonter une
violation de clé primaire. Un double de connexion ne prouverait que la chaîne
SQL envoyée.

`app_settings` fait partie des tables vidées par `clean_db` : chaque test part
donc d'un magasin de réglages vide.
"""

from __future__ import annotations

import psycopg2
import pytest


class TestGetSetting:
    def test_an_absent_key_returns_the_default(self, storage):
        assert storage.settings.get_setting("jamais_ecrite") == ""
        assert storage.settings.get_setting("jamais_ecrite", "repli") == "repli"

    def test_a_written_key_ignores_the_default(self, storage):
        storage.settings.set_setting("notify_mode", "instant")

        assert storage.settings.get_setting("notify_mode", "repli") == "instant"

    def test_the_default_is_returned_as_is_without_being_stored(self, storage, sql):
        """`get_setting` est un pur lecteur : lire une clé absente ne doit pas
        la matérialiser avec sa valeur par défaut, sinon le prochain changement
        de défaut dans le code n'aurait plus d'effet sur les bases déjà
        déployées."""
        assert storage.settings.get_setting("absente", "defaut") == "defaut"

        assert sql.one("SELECT COUNT(*) FROM app_settings") == 0


class TestSetSetting:
    def test_a_new_key_is_inserted_and_readable(self, storage, sql):
        assert storage.settings.set_setting("retention_days", "30") is True

        assert sql.row("SELECT key, value FROM app_settings") == ("retention_days", "30")

    def test_writing_the_same_key_twice_updates_instead_of_raising(self, storage, sql):
        """🔒 Le cœur du test : `key` est PRIMARY KEY. Sans le `ON CONFLICT
        (key) DO UPDATE`, la seconde écriture lèverait une `UniqueViolation` —
        c'est-à-dire que tout changement de réglage depuis l'admin planterait
        dès la deuxième fois."""
        storage.settings.set_setting("retention_days", "30")

        assert storage.settings.set_setting("retention_days", "7") is True

        assert storage.settings.get_setting("retention_days") == "7"
        assert sql.one("SELECT COUNT(*) FROM app_settings WHERE key = 'retention_days'") == 1

    def test_many_rewrites_never_accumulate_rows(self, storage, sql):
        for value in ("a", "b", "c", "d", "e"):
            storage.settings.set_setting("cle", value)

        assert sql.one("SELECT COUNT(*) FROM app_settings") == 1
        assert storage.settings.get_setting("cle") == "e"

    def test_distinct_keys_coexist(self, storage):
        storage.settings.set_setting("un", "1")
        storage.settings.set_setting("deux", "2")

        assert storage.settings.get_setting("un") == "1"
        assert storage.settings.get_setting("deux") == "2"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            pytest.param("simple", "valeur", id="ascii"),
            pytest.param("clé.avec.points", "valeur accentuée éà", id="accents-et-points"),
            pytest.param("cle", "", id="valeur-vide"),
            pytest.param("cle", "  espaces conservés  ", id="espaces-non-rognes"),
            pytest.param("cle", "ligne1\nligne2", id="saut-de-ligne"),
            pytest.param("cle", '{"json": "brut"}', id="json-stocke-en-texte"),
            # Aucune interpolation : la valeur est liée en paramètre.
            pytest.param("'; DROP TABLE app_settings; --", "'; DELETE FROM users; --", id="charge-hostile"),
        ],
    )
    def test_keys_and_values_round_trip_verbatim(self, storage, key, value):
        storage.settings.set_setting(key, value)

        assert storage.settings.get_setting(key, "PAS-LU") == value

    def test_the_value_column_is_not_nullable(self, storage):
        """`value TEXT NOT NULL` : passer `None` n'écrit pas « pas de valeur »,
        ça lève. Comportement ACTUEL figé — `set_setting` ne valide rien en
        amont et laisse remonter l'erreur du moteur, contrairement à
        `log_admin_action` qui l'avale."""
        with pytest.raises(psycopg2.errors.NotNullViolation):
            storage.settings.set_setting("cle", None)

    def test_the_connection_is_reusable_after_that_failure(self, storage):
        """Le repo ne rollback pas lui-même : c'est `release_to_pool` qui
        rattrape la transaction avortée au retour au pool. Sans lui, la
        connexion empoisonnerait la requête suivante."""
        with pytest.raises(psycopg2.errors.NotNullViolation):
            storage.settings.set_setting("cle", None)

        assert storage.settings.set_setting("cle", "ça repart") is True
        assert storage.settings.get_setting("cle") == "ça repart"
