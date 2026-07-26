"""Tests unitaires de `scripts/migrate.py`.

Ce script applique du DDL. Sa cible est lue dans `DATABASE_URL`, que son propre
`load_dotenv()` peut peupler depuis le `.env` du dépôt — donc, sur un poste de
développeur, la base de PRODUCTION. Tous les garde-fous qu'il contient (affichage
de la cible, refus d'un hôte distant sans confirmation) sont testés ici, ainsi
que ce qu'ils ne couvrent pas.

`Storage.run_migrations` est systématiquement doublé : aucun test n'exécute de
DDL (le socle coupe de toute façon psycopg2).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import dotenv
import pytest

# `scripts/migrate.py` appelle `load_dotenv()` au niveau MODULE : l'importer
# suffirait à injecter le DATABASE_URL de production dans os.environ pour tout
# le reste du run (le garde-fou de tests/conftest.py ne repurge qu'après la
# collecte). On neutralise donc `dotenv.load_dotenv` le temps de l'import — le
# `from dotenv import load_dotenv` du script lie alors ce remplaçant.
_real_load_dotenv = dotenv.load_dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
try:
    from scripts import migrate
finally:
    dotenv.load_dotenv = _real_load_dotenv

REPO_ROOT = Path(migrate.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------

@pytest.fixture
def storage_double(monkeypatch):
    """`Storage` doublé, avec la vraie signature de `run_migrations`."""
    from storage import Storage

    double = MagicMock(spec=Storage)
    monkeypatch.setattr(migrate, "Storage", double)
    return double


@pytest.fixture
def stdin(monkeypatch):
    """Contrôle `sys.stdin.isatty()` et les réponses de `input()`."""

    class Stdin:
        def __init__(self):
            self.answers: list[str] = []
            self.prompts: list[str] = []
            self._tty = False

        def isatty(self):
            return self._tty

        def as_tty(self, *answers):
            self._tty = True
            self.answers = list(answers)
            monkeypatch.setattr("builtins.input", self._input)
            return self

        def as_pipe(self):
            self._tty = False
            monkeypatch.setattr("builtins.input", self._refuse)
            return self

        def _input(self, prompt=""):
            self.prompts.append(prompt)
            return self.answers.pop(0)

        def _refuse(self, prompt=""):
            raise AssertionError(
                "input() appelé alors que stdin n'est pas un terminal : "
                "le script doit refuser sans jamais demander"
            )

    stub = Stdin().as_pipe()
    monkeypatch.setattr(sys, "stdin", stub)
    return stub


LOCAL_URL = "postgresql://appart:secret@localhost:5432/appart"
REMOTE_URL = "postgresql://appart:secret@db.prod.example.com:5432/appart"


# ---------------------------------------------------------------------------
# DATABASE_URL manquant
# ---------------------------------------------------------------------------

class TestMissingDatabaseUrl:
    def test_it_refuses_to_run_and_says_where_to_look(self, monkeypatch, capsys, storage_double):
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(SystemExit) as excinfo:
            migrate.main([])

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "DATABASE_URL manquant (ni variable d'env, ni .env)" in err
        storage_double.run_migrations.assert_not_called()

    @pytest.mark.parametrize(
        ("argv", "case"),
        [([], "sans argument"), (["--yes"], "avec --yes"), (["-y"], "avec -y")],
        ids=["no_args", "yes_flag", "y_flag"],
    )
    def test_no_flag_bypasses_the_missing_url_check(
        self, monkeypatch, capsys, storage_double, argv, case
    ):
        """`--yes` court-circuite la CONFIRMATION, pas la présence de l'URL."""
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with pytest.raises(SystemExit) as excinfo:
            migrate.main(argv)

        assert excinfo.value.code == 1, case
        storage_double.run_migrations.assert_not_called()

    def test_an_empty_database_url_counts_as_missing(self, monkeypatch, storage_double):
        monkeypatch.setenv("DATABASE_URL", "")

        with pytest.raises(SystemExit) as excinfo:
            migrate.main([])

        assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# Hôte local : exécution directe
# ---------------------------------------------------------------------------

class TestLocalTarget:
    @pytest.mark.parametrize(
        ("url", "expected_target"),
        [
            (LOCAL_URL, "appart@localhost:5432/appart"),
            ("postgresql://appart@127.0.0.1:5432/appart", "appart@127.0.0.1:5432/appart"),
            ("postgresql://appart@[::1]:5432/appart", "appart@::1:5432/appart"),
            ("postgresql://appart@postgres:5432/appart", "appart@postgres:5432/appart"),
            ("postgresql://appart@db:5432/appart", "appart@db:5432/appart"),
            ("postgresql:///appart", "?@:5432/appart"),
            ("postgresql://appart@localhost/appart", "appart@localhost:5432/appart"),
        ],
        ids=["localhost", "ipv4", "ipv6", "docker_postgres", "docker_db", "unix_socket", "default_port"],
    )
    def test_every_local_host_runs_without_confirmation(
        self, monkeypatch, capsys, storage_double, stdin, url, expected_target
    ):
        """Les hôtes de `_LOCAL_HOSTS` (dont les noms de service Docker et le
        socket Unix) sont considérés sans risque : pas de confirmation, même sans
        terminal. `stdin` est en mode « pipe » et fait échouer tout `input()`."""
        monkeypatch.setenv("DATABASE_URL", url)

        migrate.main([])

        storage_double.run_migrations.assert_called_once_with(url)
        out = capsys.readouterr().out
        assert f"Cible des migrations : {expected_target}" in out
        assert "Migrations appliquées avec succès." in out

    def test_the_target_is_printed_before_any_write(self, monkeypatch, capsys, storage_double):
        """L'affichage précède l'exécution : si le DDL plante, l'opérateur sait
        quelle base a été touchée."""
        monkeypatch.setenv("DATABASE_URL", LOCAL_URL)
        printed: list[str] = []
        storage_double.run_migrations.side_effect = lambda url: printed.append(capsys.readouterr().out)

        migrate.main([])

        assert "Cible des migrations : appart@localhost:5432/appart" in printed[0]

    def test_the_password_is_never_printed(self, monkeypatch, capsys, storage_double):
        """La cible affichée est reconstruite champ par champ, sans le mot de
        passe : la sortie du script atterrit dans des logs de déploiement."""
        monkeypatch.setenv("DATABASE_URL", LOCAL_URL)

        migrate.main([])

        assert "secret" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "host",
        ["LOCALHOST", "LocalHost", "POSTGRES"],
        ids=["upper", "mixed", "upper_docker"],
    )
    def test_the_host_comparison_is_case_insensitive(
        self, monkeypatch, storage_double, stdin, host
    ):
        monkeypatch.setenv("DATABASE_URL", f"postgresql://appart@{host}:5432/appart")

        migrate.main([])

        storage_double.run_migrations.assert_called_once()

    def test_a_ddl_failure_propagates_instead_of_being_reported_as_success(
        self, monkeypatch, capsys, storage_double
    ):
        """Aucun `try` autour de `run_migrations` : l'exception remonte, le
        message de succès n'est pas affiché, et le code de sortie est non nul.
        C'est le comportement voulu pour un script de déploiement."""
        monkeypatch.setenv("DATABASE_URL", LOCAL_URL)
        storage_double.run_migrations.side_effect = RuntimeError("relation déjà verrouillée")

        with pytest.raises(RuntimeError, match="relation déjà verrouillée"):
            migrate.main([])

        assert "Migrations appliquées" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Hôte distant
# ---------------------------------------------------------------------------

class TestRemoteTargetWithoutTty:
    def test_it_refuses_a_remote_host_when_nothing_can_confirm(
        self, monkeypatch, capsys, storage_double, stdin
    ):
        """Le cas d'un CI ou d'un `| bash` : pas de terminal, donc pas de
        confirmation possible. Refuser est le seul choix sûr — c'est ce qui
        empêche un script d'appliquer du DDL en production par accident."""
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)

        with pytest.raises(SystemExit) as excinfo:
            migrate.main([])

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Cible des migrations : appart@db.prod.example.com:5432/appart" in captured.out
        assert "Cible distante (db.prod.example.com) et pas de terminal pour confirmer" in captured.err
        assert "Relancer avec --yes si c'est intentionnel." in captured.err
        storage_double.run_migrations.assert_not_called()

    @pytest.mark.parametrize("flag", ["--yes", "-y"], ids=["long", "short"])
    def test_an_explicit_flag_authorizes_a_remote_run(
        self, monkeypatch, capsys, storage_double, stdin, flag
    ):
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)

        migrate.main([flag])

        storage_double.run_migrations.assert_called_once_with(REMOTE_URL)
        assert "Migrations appliquées avec succès." in capsys.readouterr().out

    def test_the_flag_is_recognized_anywhere_in_the_arguments(
        self, monkeypatch, storage_double, stdin
    ):
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)

        migrate.main(["--verbose", "quelque-chose", "--yes"])

        storage_double.run_migrations.assert_called_once_with(REMOTE_URL)

    @pytest.mark.parametrize(
        "argv",
        [["--y"], ["yes"], ["--YES"], ["-Y"], ["--yes=1"]],
        ids=["truncated", "no_dashes", "uppercase", "uppercase_short", "with_value"],
    )
    def test_a_near_miss_flag_does_not_authorize_anything(
        self, monkeypatch, storage_double, stdin, argv
    ):
        """Le test est un `in` exact sur la liste : aucune tolérance. Mieux vaut
        un refus qu'une autorisation approximative."""
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)

        with pytest.raises(SystemExit) as excinfo:
            migrate.main(argv)

        assert excinfo.value.code == 1
        storage_double.run_migrations.assert_not_called()

    def test_argv_defaults_to_the_process_arguments(self, monkeypatch, storage_double, stdin):
        """`main()` sans argument lit `sys.argv[1:]` : c'est le chemin réel de
        `python -m scripts.migrate --yes`."""
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)
        monkeypatch.setattr(sys, "argv", ["scripts/migrate.py", "--yes"])

        migrate.main()

        storage_double.run_migrations.assert_called_once_with(REMOTE_URL)


class TestRemoteTargetWithTty:
    @pytest.mark.parametrize(
        "answer",
        ["oui", "o", "yes", "y", "OUI", " Oui ", "Y"],
        ids=["oui", "o", "yes", "y", "upper", "padded", "upper_short"],
    )
    def test_an_affirmative_answer_runs_the_migrations(
        self, monkeypatch, capsys, storage_double, stdin, answer
    ):
        """`.strip().lower()` : la casse et les espaces sont tolérés sur la
        réponse — contrairement au drapeau."""
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)
        stdin.as_tty(answer)

        migrate.main([])

        storage_double.run_migrations.assert_called_once_with(REMOTE_URL)
        assert stdin.prompts == ["Appliquer les migrations sur db.prod.example.com ? [oui/non] "]
        assert "Migrations appliquées avec succès." in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("answer", "case"),
        [
            ("non", "refus explicite"),
            ("n", "refus abrégé"),
            ("", "entrée vide (défaut = refus)"),
            ("ok", "réponse ambiguë"),
            ("si", "réponse dans une autre langue"),
            ("1", "réponse numérique"),
        ],
        ids=["non", "n", "empty", "ok", "si", "one"],
    )
    def test_anything_but_an_explicit_yes_cancels(
        self, monkeypatch, capsys, storage_double, stdin, answer, case
    ):
        """Liste blanche : tout ce qui n'est pas un accord franc annule. Une
        entrée vide (l'utilisateur tape Entrée) annule donc aussi."""
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)
        stdin.as_tty(answer)

        with pytest.raises(SystemExit) as excinfo:
            migrate.main([])

        assert excinfo.value.code == 1, case
        assert "Annulé." in capsys.readouterr().err
        storage_double.run_migrations.assert_not_called()

    def test_the_flag_skips_the_prompt_even_on_a_terminal(
        self, monkeypatch, storage_double, stdin
    ):
        monkeypatch.setenv("DATABASE_URL", REMOTE_URL)
        stdin.as_tty()  # aucune réponse préparée : un input() lèverait IndexError

        migrate.main(["--yes"])

        storage_double.run_migrations.assert_called_once_with(REMOTE_URL)
        assert stdin.prompts == []

    def test_a_local_target_is_never_confirmed_even_on_a_terminal(
        self, monkeypatch, storage_double, stdin
    ):
        monkeypatch.setenv("DATABASE_URL", LOCAL_URL)
        stdin.as_tty()

        migrate.main([])

        assert stdin.prompts == []
        storage_double.run_migrations.assert_called_once_with(LOCAL_URL)


# ---------------------------------------------------------------------------
# Limites du garde-fou
# ---------------------------------------------------------------------------

class TestGuardrailLimits:
    @pytest.mark.parametrize(
        ("url", "case"),
        [
            ("postgresql://appart@127.0.0.2:5432/appart", "autre adresse de loopback"),
            ("postgresql://appart@LOCALHOST.prod.example.com:5432/appart", "sous-domaine trompeur"),
        ],
        ids=["loopback_alias", "misleading_subdomain"],
    )
    def test_the_local_list_is_an_exact_match_not_a_heuristic(
        self, monkeypatch, storage_double, stdin, url, case
    ):
        """`_LOCAL_HOSTS` est un ensemble de noms exacts : `127.0.0.2` (pourtant
        du loopback) et un hôte dont le nom COMMENCE par « localhost » sont
        traités comme distants, donc protégés. Le garde-fou est conservateur
        dans le bon sens.
        """
        monkeypatch.setenv("DATABASE_URL", url)

        with pytest.raises(SystemExit) as excinfo:
            migrate.main([])

        assert excinfo.value.code == 1, case
        storage_double.run_migrations.assert_not_called()

    def test_a_malformed_url_is_passed_through_to_the_driver(
        self, monkeypatch, capsys, storage_double, stdin
    ):
        """`urlparse` ne valide rien : une URL sans schéma n'a pas d'hôte, tombe
        donc dans `_LOCAL_HOSTS` (chaîne vide) et part directement au driver, qui
        seul refusera. Le garde-fou ne protège que ce qu'il sait analyser."""
        monkeypatch.setenv("DATABASE_URL", "pas-une-url-du-tout")

        migrate.main([])

        storage_double.run_migrations.assert_called_once_with("pas-une-url-du-tout")
        assert "Cible des migrations : ?@:5432" in capsys.readouterr().out

    def test_importing_this_module_did_not_leak_the_production_url(self):
        """Contrôle du contournement d'import en tête de ce fichier.

        Vérifié : sans le remplacement de `dotenv.load_dotenv`, un simple
        `from scripts import migrate` pose bien `DATABASE_URL` dans
        `os.environ` (le `.env` du dépôt en contient un). Si quelqu'un retire
        ce contournement, ce test devient rouge — avant qu'un autre test ne
        parte joindre la production.
        """
        import os

        assert "DATABASE_URL" not in os.environ, (
            "DATABASE_URL présent dans l'environnement du test : l'import de "
            "scripts.migrate a chargé .env, le contournement en tête de fichier "
            "ne fonctionne plus"
        )

    def test_the_script_loads_dotenv_at_import_time(self):
        """Vérification statique du contraire de main.py : ici le
        `load_dotenv()` au niveau module est ASSUMÉ (point d'entrée CLI, jamais
        importé par l'app — cf. `per-file-ignores` E402 dans pyproject.toml).

        C'est aussi ce qui rend l'affichage de la cible indispensable : la base
        visée peut venir d'un `.env` que l'opérateur a oublié, et c'est
        exactement le scénario de l'incident de fuite de la base de production.
        Ce test existe pour que quiconque déplace cet appel voie la contrepartie.
        """
        tree = ast.parse((REPO_ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8"))
        module_level_calls = [
            node.value.func.id
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ]

        assert "load_dotenv" in module_level_calls
