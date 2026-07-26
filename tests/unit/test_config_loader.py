"""Tests unitaires de `config/loader.py`.

`load_config()` est appelé au tout début de `create_app()` : ce qu'il rend
détermine la base de données sur laquelle l'app va travailler. Deux points
critiques :

  * `DATABASE_URL` **remplace** le bloc `database` du YAML (écrasement, pas
    fusion) — c'est ce qui permet à un déploiement de n'avoir aucun fichier de
    config, et ce qui fait qu'un `.env` traînant redirige toute l'app ;
  * une config invalide appelle `sys.exit(1)` : l'app refuse de démarrer plutôt
    que de tourner sur des défauts silencieux.

⚠️ Le socle purge `DATABASE_URL` de l'environnement pour tout le run
(tests/conftest.py) : les tests qui en ont besoin le posent avec
`monkeypatch.setenv`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from loguru import logger

from config.loader import AppConfig, DatabaseConfig, NtfyConfig, load_config

DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/appart"


@pytest.fixture
def log_messages():
    messages: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda msg: messages.append((msg.record["level"].name, msg.record["message"])), level="DEBUG"
    )
    yield messages
    logger.remove(sink_id)


def write_config(path: Path, payload) -> Path:
    """Écrit un config.yaml. `payload` peut être un dict ou du YAML brut."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else yaml.safe_dump(payload)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Défauts
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_the_defaults_are_the_ones_the_app_depends_on(self):
        """Ces valeurs sont le contrat implicite d'un déploiement sans config :
        les figer évite qu'un changement de défaut redirige silencieusement les
        notifications ou la base."""
        config = AppConfig()

        assert config.ntfy == NtfyConfig(server="https://ntfy.sh", priority="default")
        assert config.database == DatabaseConfig(database_url=DEFAULT_DB_URL)
        assert config.log_level == "INFO"

    def test_a_missing_file_warns_and_falls_back_to_the_defaults(self, tmp_path, log_messages):
        """Ne PAS échouer sur config absente est délibéré : en production, tout
        arrive de l'environnement. Mais le warning doit exister, sinon un chemin
        mal orthographié passe inaperçu."""
        config = load_config(tmp_path / "jamais_créé.yaml")

        assert config.database.database_url == DEFAULT_DB_URL
        assert config.log_level == "INFO"
        assert any(level == "WARNING" and "Config introuvable" in msg for level, msg in log_messages)

    @pytest.mark.parametrize(
        ("content", "case"),
        [
            ("", "fichier vide"),
            ("# uniquement un commentaire\n", "commentaires seuls"),
            ("---\n", "document YAML vide"),
            ("null\n", "document valant null"),
        ],
        ids=["empty", "comment_only", "empty_document", "null_document"],
    )
    def test_an_empty_yaml_falls_back_to_the_defaults(self, tmp_path, content, case):
        """`yaml.safe_load` rend `None` sur ces quatre formes : le `or {}` est ce
        qui évite un `AppConfig(**None)`."""
        path = write_config(tmp_path / "config.yaml", content)

        config = load_config(path)

        assert config.database.database_url == DEFAULT_DB_URL, case
        assert config.ntfy.server == "https://ntfy.sh", case

    def test_a_partial_config_only_overrides_what_it_declares(self, tmp_path):
        """Fusion au niveau des MODÈLES (pydantic) : déclarer `ntfy.priority`
        seul garde le serveur par défaut."""
        path = write_config(tmp_path / "config.yaml", {"ntfy": {"priority": "high"}})

        config = load_config(path)

        assert config.ntfy.priority == "high"
        assert config.ntfy.server == "https://ntfy.sh"
        assert config.database.database_url == DEFAULT_DB_URL


# ---------------------------------------------------------------------------
# Lecture d'un fichier valide
# ---------------------------------------------------------------------------

class TestValidFile:
    def test_every_field_is_read_from_the_yaml(self, tmp_path, log_messages):
        path = write_config(
            tmp_path / "config.yaml",
            {
                "ntfy": {"server": "https://ntfy.example.org", "priority": "urgent"},
                "database": {"database_url": "postgresql://u:p@db.example:6543/appart"},
                "log_level": "DEBUG",
            },
        )

        config = load_config(path)

        assert config.ntfy.server == "https://ntfy.example.org"
        assert config.ntfy.priority == "urgent"
        assert config.database.database_url == "postgresql://u:p@db.example:6543/appart"
        assert config.log_level == "DEBUG"
        assert any("Configuration chargée depuis" in msg for _, msg in log_messages)

    @pytest.mark.parametrize("as_string", [True, False], ids=["str_path", "path_object"])
    def test_both_str_and_path_are_accepted(self, tmp_path, as_string):
        """La signature annonce `str | Path` : `create_app` passe une chaîne,
        les tests passent des `Path`."""
        path = write_config(tmp_path / "config.yaml", {"log_level": "WARNING"})

        config = load_config(str(path) if as_string else path)

        assert config.log_level == "WARNING"

    def test_unknown_keys_are_ignored_rather_than_rejected(self, tmp_path):
        """Pydantic ignore les extras par défaut : une clé obsolète laissée dans
        le YAML (ou une faute de frappe) ne bloque pas le démarrage — mais ne
        prévient pas non plus. À savoir en cas de réglage « sans effet »."""
        path = write_config(
            tmp_path / "config.yaml",
            {"log_level": "DEBUG", "obsolete_section": {"foo": 1}, "loglevel": "ERROR"},
        )

        config = load_config(path)

        assert config.log_level == "DEBUG"

    def test_the_log_level_is_not_validated(self, tmp_path):
        """`log_level` est un `str` libre : une valeur absurde n'est refusée
        qu'au moment du `logger.add()` de `create_app`, donc plus tard et avec
        un message moins clair. Comportement actuel figé ici."""
        path = write_config(tmp_path / "config.yaml", {"log_level": "PAS_UN_NIVEAU"})

        assert load_config(path).log_level == "PAS_UN_NIVEAU"


# ---------------------------------------------------------------------------
# DATABASE_URL
# ---------------------------------------------------------------------------

class TestDatabaseUrlOverride:
    def test_the_environment_wins_over_the_file(self, tmp_path, monkeypatch, log_messages):
        path = write_config(
            tmp_path / "config.yaml", {"database": {"database_url": "postgresql://fichier/local"}}
        )
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/prod")

        config = load_config(path)

        assert config.database.database_url == "postgresql://env/prod"
        assert any("DATABASE_URL chargé depuis les variables" in msg for _, msg in log_messages)

    def test_the_whole_database_block_is_replaced_not_merged(self, tmp_path, monkeypatch):
        """`raw["database"] = {"database_url": env}` : l'affectation ÉCRASE le
        bloc entier. Démonstration : un bloc `database` par ailleurs invalide
        (une chaîne au lieu d'un mapping), qui ferait normalement échouer la
        validation, devient inoffensif dès que `DATABASE_URL` est posé.

        Conséquence pratique : tout futur réglage ajouté sous `database:`
        (taille de pool, timeout) serait silencieusement perdu en production,
        puisque `DATABASE_URL` y est toujours défini.
        """
        path = write_config(tmp_path / "config.yaml", "database: postgresql://pas-un-mapping\n")

        with pytest.raises(SystemExit):
            load_config(path)

        monkeypatch.setenv("DATABASE_URL", "postgresql://env/prod")
        config = load_config(path)

        assert config.database.database_url == "postgresql://env/prod"

    @pytest.mark.parametrize(
        ("value", "case"),
        [("", "chaîne vide"), (" ", "espace")],
        ids=["empty", "space"],
    )
    def test_an_empty_environment_variable_is_treated_differently_than_a_blank_one(
        self, tmp_path, monkeypatch, value, case
    ):
        """`if env_db_url:` — une variable VIDE est ignorée (le fichier fait
        alors autorité), mais une variable contenant un espace est prise au
        sérieux et produit une URL inutilisable, détectée seulement à la
        première connexion.
        """
        path = write_config(
            tmp_path / "config.yaml", {"database": {"database_url": "postgresql://fichier/local"}}
        )
        monkeypatch.setenv("DATABASE_URL", value)

        config = load_config(path)

        expected = "postgresql://fichier/local" if value == "" else value
        assert config.database.database_url == expected, case

    def test_the_environment_also_wins_when_there_is_no_file_at_all(self, tmp_path, monkeypatch):
        """Le cas du déploiement : ni config.yaml, ni bloc `database`."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://env/prod")

        config = load_config(tmp_path / "absent.yaml")

        assert config.database.database_url == "postgresql://env/prod"


# ---------------------------------------------------------------------------
# Configuration invalide
# ---------------------------------------------------------------------------

class TestInvalidConfig:
    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            ("ntfy: pas-un-mapping\n", "section ntfy scalaire"),
            ("database: 42\n", "section database numérique"),
            ("ntfy:\n  server: 123\n", "server non-chaîne (pydantic ne coerce pas int -> str)"),
            ("log_level:\n  - DEBUG\n", "log_level en liste"),
            ("ntfy:\n  - un\n  - deux\n", "section ntfy en liste"),
        ],
        ids=["ntfy_scalar", "database_number", "server_int", "log_level_list", "ntfy_list"],
    )
    def test_an_invalid_config_exits_with_code_one(self, tmp_path, payload, case, log_messages):
        """`sys.exit(1)` plutôt qu'une exception : le processus s'arrête avant
        d'avoir touché la base. Un démarrage sur des défauts serait pire — il
        pointerait le localhost du conteneur.
        """
        path = write_config(tmp_path / "config.yaml", payload)

        with pytest.raises(SystemExit) as excinfo:
            load_config(path)

        assert excinfo.value.code == 1, case
        assert any(
            level == "ERROR" and "Erreur de configuration" in msg for level, msg in log_messages
        ), case

    def test_a_top_level_list_fails_on_the_keyword_expansion(self, tmp_path):
        """`AppConfig(**raw)` avec une liste lève un `TypeError`, PAS un
        `ValidationError` — et le `except Exception` l'attrape quand même, donc
        le comportement observable reste `SystemExit(1)`."""
        path = write_config(tmp_path / "config.yaml", "- un\n- deux\n")

        with pytest.raises(SystemExit) as excinfo:
            load_config(path)

        assert excinfo.value.code == 1

    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            ("ntfy:\n  server: [unclosed\n", "liste non fermée"),
            ("a:\n\tb: 1\n", "tabulation d'indentation"),
            ("clé: 'guillemet non fermé\n", "chaîne non terminée"),
        ],
        ids=["unclosed_list", "tab_indent", "unterminated_string"],
    )
    def test_malformed_yaml_escapes_the_friendly_error_path(self, tmp_path, payload, case):
        """# BUG (borné) : la syntaxe YAML n'est pas gérée comme les autres erreurs.

        `yaml.safe_load` est appelé HORS du `try` (config/loader.py:43), qui ne
        couvre que la validation pydantic. Une erreur de syntaxe remonte donc en
        `yaml.YAMLError` brute : trace complète au démarrage, sans le
        `logger.error("Erreur de configuration : ...")` prévu juste à côté, et
        sans le code de sortie 1 attendu par un superviseur.
        """
        path = write_config(tmp_path / "config.yaml", payload)

        with pytest.raises(yaml.YAMLError):
            load_config(path)


# ---------------------------------------------------------------------------
# Le chemin par défaut
# ---------------------------------------------------------------------------

class TestDefaultPath:
    def test_the_default_path_is_relative_to_the_current_directory(self, tmp_path, monkeypatch):
        """Fragilité documentée : le défaut est `"config/config.yaml"`, un chemin
        RELATIF. La config trouvée dépend donc du répertoire de travail du
        process — lancer l'app depuis un autre dossier (`cd /`, un service systemd
        sans `WorkingDirectory`, un cron) charge silencieusement les défauts.

        Un chemin dérivé de `__file__`, comme le fait `scrape_logs.storage` pour
        `logs/`, supprimerait cette dépendance.
        """
        write_config(tmp_path / "config" / "config.yaml", {"log_level": "TRACE"})
        monkeypatch.chdir(tmp_path)

        assert load_config().log_level == "TRACE"

    def test_a_different_working_directory_silently_yields_the_defaults(self, tmp_path, monkeypatch):
        """Le pendant du test précédent : depuis un répertoire sans
        `config/config.yaml`, aucun échec — juste un warning et les défauts."""
        empty = tmp_path / "ailleurs"
        empty.mkdir()
        monkeypatch.chdir(empty)

        config = load_config()

        assert config.log_level == "INFO"
        assert config.database.database_url == DEFAULT_DB_URL

    def test_the_repository_config_file_is_valid(self):
        """Le vrai `config/config.yaml` du dépôt doit se charger : c'est celui
        que lit un développeur au démarrage, et une faute de frappe dedans se
        traduirait par un `sys.exit(1)` au lancement."""
        config = load_config(Path(__file__).resolve().parents[2] / "config" / "config.yaml")

        assert config.ntfy.server == "https://ntfy.sh"
        assert config.log_level == "INFO"
        assert config.database.database_url.startswith("postgresql://")
