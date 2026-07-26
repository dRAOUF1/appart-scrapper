"""SelogerGeoRepository contre un vrai Postgres.

Ce cache tient une distinction à trois états que seule une vraie table peut
matérialiser :

    pas de ligne                -> jamais tenté          -> résoudre
    ligne, place_id renseigné   -> résolu                -> réutiliser
    ligne, place_id à NULL      -> échec MÉMORISÉ        -> ne pas réessayer
                                                            avant 7 jours

Un dict Python confondrait les deux derniers cas (`{"k": None}` vs `"k"
absent`) au premier `.get()` distrait, et un double de connexion ne prouverait
rien du tout : c'est `SELECT ... WHERE area_key = %s` qui rend `None` pour
« absent » et une ligne à `place_id IS NULL` pour « échec ». La classe
`TestCooldownDependsOnTheThreeStates` va jusqu'au consommateur réel,
`services.seloger_geocode.resolve_place_id`, avec un vrai repo derrière.

Deuxième sujet : `area_key` est une PRIMARY KEY qui mélange quatre niveaux de
périmètre dans un même espace de noms (voir
`services.seloger_geocode.area_cache_key`). Le département 75 et la région 75
existent tous les deux, et le niveau commune garde le code INSEE nu : les
préfixes sont la seule chose qui empêche une collision.

`seloger_place_ids` n'est pas dans `clean_db` mais dans la fixture autouse
dédiée `clean_geo_cache` — chaque test part donc d'un cache vide.
"""

from __future__ import annotations

import pytest

from services.seloger_geocode import area_cache_key, resolve_place_id
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Les trois états
# ---------------------------------------------------------------------------

class TestThreeStates:
    def test_a_key_never_attempted_has_no_row_at_all(self, storage, sql):
        assert storage.seloger_geo.get_cached("75113") is None

        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids") == 0

    def test_a_resolved_key_round_trips_with_its_timestamp(self, storage):
        storage.seloger_geo.set_cached("75113", "AD09FR40")

        row = storage.seloger_geo.get_cached("75113")

        assert row["area_key"] == "75113"
        assert row["place_id"] == "AD09FR40"
        assert row["resolved_at"] is not None

    def test_a_failure_writes_a_row_with_a_null_place_id(self, storage, sql):
        """🔒 La ligne à NULL est ce qui distingue « tenté et raté » de « jamais
        tenté ». Sans elle, chaque cycle de scrape re-tenterait la résolution
        d'un périmètre introuvable et martèlerait le site de SeLoger."""
        storage.seloger_geo.set_cached("99999", None)

        row = storage.seloger_geo.get_cached("99999")

        assert row is not None, "un échec doit laisser une trace, pas rien"
        assert row["place_id"] is None
        assert row["resolved_at"] is not None
        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids WHERE place_id IS NULL") == 1

    def test_absent_and_failed_are_two_different_return_values(self, storage):
        """Les deux cas sont falsy côté appelant : la seule façon de les
        distinguer est `is None` sur la ligne, pas sur `place_id`."""
        storage.seloger_geo.set_cached("echec", None)

        absent = storage.seloger_geo.get_cached("jamais-tentee")
        failed = storage.seloger_geo.get_cached("echec")

        assert absent is None
        assert failed is not None
        assert failed["place_id"] is None


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_writing_the_same_key_twice_updates_instead_of_duplicating(self, storage, sql):
        """`area_key` est PRIMARY KEY : sans le `ON CONFLICT DO UPDATE`, la
        seconde résolution d'un même périmètre lèverait une `UniqueViolation`
        au milieu d'un scrape."""
        storage.seloger_geo.set_cached("75113", "AD09FR40")

        storage.seloger_geo.set_cached("75113", "AD09FR41")

        assert storage.seloger_geo.get_cached("75113")["place_id"] == "AD09FR41"
        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids") == 1

    def test_a_failure_can_be_upgraded_to_a_success(self, storage, sql):
        """Le chemin normal après le cooldown : la ligne à NULL est écrasée par
        le placeId enfin trouvé, elle n'est pas doublée."""
        storage.seloger_geo.set_cached("75113", None)

        storage.seloger_geo.set_cached("75113", "AD09FR40")

        assert storage.seloger_geo.get_cached("75113")["place_id"] == "AD09FR40"
        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids") == 1

    def test_a_success_can_be_downgraded_to_a_failure(self, storage):
        """# Comportement ACTUEL, contre-intuitif : `set_cached(key, None)`
        efface un placeId valide déjà en cache. `resolve_place_id` n'écrit que
        sur le chemin où le cache n'a rien donné, donc ce n'est pas déclenché
        aujourd'hui — mais rien dans le repo ne protège la valeur acquise.
        """
        storage.seloger_geo.set_cached("75113", "AD09FR40")

        storage.seloger_geo.set_cached("75113", None)

        assert storage.seloger_geo.get_cached("75113")["place_id"] is None

    def test_resolved_at_moves_forward_on_every_write(self, storage, sql):
        """Le `resolved_at = CURRENT_TIMESTAMP` de la branche `DO UPDATE` est ce
        qui fait repartir le cooldown : sans lui, une ligne d'échec resterait
        éternellement « vieille de 8 jours » et serait re-tentée à chaque
        cycle."""
        storage.seloger_geo.set_cached("75113", None)
        sql.exec("UPDATE seloger_place_ids SET resolved_at = NOW() - INTERVAL '30 days'")
        old = storage.seloger_geo.get_cached("75113")["resolved_at"]

        storage.seloger_geo.set_cached("75113", None)

        assert storage.seloger_geo.get_cached("75113")["resolved_at"] > old


# ---------------------------------------------------------------------------
# Cohabitation des niveaux dans une clé primaire unique
# ---------------------------------------------------------------------------

class TestAreaKeyNamespace:
    def test_department_75_and_region_75_do_not_collide(self, storage):
        """Le code 75 désigne le département de Paris ET la région
        Nouvelle-Aquitaine. Sans le préfixe de niveau dans la clé, résoudre l'un
        écraserait l'autre — et un scrape « toute la Nouvelle-Aquitaine »
        renverrait des annonces parisiennes."""
        storage.seloger_geo.set_cached("dept:75", "AD06FR75")
        storage.seloger_geo.set_cached("region:75", "AD04FR75")

        assert storage.seloger_geo.get_cached("dept:75")["place_id"] == "AD06FR75"
        assert storage.seloger_geo.get_cached("region:75")["place_id"] == "AD04FR75"

    def test_the_four_levels_coexist_including_the_bare_insee_commune_key(self, storage, sql):
        """Le niveau commune garde le code INSEE NU (`75113`), sans préfixe :
        c'est la convention d'avant les périmètres larges, conservée pour ne pas
        invalider le cache déjà constitué en production. Elle doit donc cohabiter
        avec les clés préfixées sans jamais les recouper."""
        entries = {
            "75113": "AD09FR40",       # commune (code INSEE nu)
            "city:86194": "AD08FR31",  # ville entière
            "dept:33": "AD06FR34",     # département
            "region:75": "AD04FR5",    # région
        }
        for key, place_id in entries.items():
            storage.seloger_geo.set_cached(key, place_id)

        assert {
            key: storage.seloger_geo.get_cached(key)["place_id"] for key in entries
        } == entries
        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids") == 4

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            pytest.param(make_city_location(insee="75113"), "75113", id="commune-insee-nu"),
            pytest.param(make_whole_city_location(insee="86194"), "city:86194", id="ville-entiere"),
            pytest.param(make_department_location(code="33"), "dept:33", id="departement"),
            pytest.param(make_region_location(code="75"), "region:75", id="region"),
        ],
    )
    def test_the_key_produced_by_area_cache_key_is_the_one_stored(self, storage, sql, location, expected_key):
        """Épingle le couplage entre le producteur de clés
        (`services.seloger_geocode.area_cache_key`) et la colonne réelle : c'est
        la seule chose qui garantit qu'une relecture retombe sur la même ligne."""
        key = area_cache_key(location)
        assert key == expected_key

        storage.seloger_geo.set_cached(key, "AD00FR0")

        assert sql.one("SELECT area_key FROM seloger_place_ids") == expected_key

    def test_an_insee_code_that_looks_like_a_prefixed_key_is_stored_verbatim(self, storage):
        """Aucune interprétation côté SQL : la clé est une chaîne opaque. Ce test
        écarte l'idée d'un jour parser `area_key` en base plutôt que de la
        traiter comme un identifiant."""
        storage.seloger_geo.set_cached("dept:dept:33", "AD06FR34")

        assert storage.seloger_geo.get_cached("dept:33") is None
        assert storage.seloger_geo.get_cached("dept:dept:33")["place_id"] == "AD06FR34"


# ---------------------------------------------------------------------------
# Le consommateur : le cooldown de 7 jours
# ---------------------------------------------------------------------------

@pytest.fixture
def resolver_calls(monkeypatch):
    """Compte les tentatives de résolution réseau, sans en faire aucune.

    `_resolve_uncached` est le seul point de sortie vers seloger.com ; la
    fixture autouse `no_network` couperait de toute façon le transport, mais on
    veut *compter* les tentatives, pas seulement les empêcher.
    """
    calls: list[dict] = []
    result: dict = {"place_id": None}

    def _fake(location):
        calls.append(location)
        return result["place_id"]

    monkeypatch.setattr("services.seloger_geocode._resolve_uncached", _fake)
    return calls, result


class TestCooldownDependsOnTheThreeStates:
    def test_a_cached_place_id_short_circuits_the_network(self, storage, resolver_calls):
        calls, _ = resolver_calls
        storage.seloger_geo.set_cached("75113", "AD09FR40")

        assert resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo) == "AD09FR40"

        assert calls == [], "un placeId en cache ne doit jamais redéclencher de résolution"

    def test_no_row_triggers_a_resolution_and_banks_the_result(self, storage, resolver_calls):
        calls, result = resolver_calls
        result["place_id"] = "AD09FR40"

        assert resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo) == "AD09FR40"

        assert len(calls) == 1
        assert storage.seloger_geo.get_cached("75113")["place_id"] == "AD09FR40"

    def test_a_failed_resolution_writes_the_null_row_that_arms_the_cooldown(self, storage, resolver_calls):
        calls, _ = resolver_calls

        assert resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo) is None

        assert len(calls) == 1
        row = storage.seloger_geo.get_cached("75113")
        assert row is not None and row["place_id"] is None

    def test_a_recent_failure_is_not_retried(self, storage, resolver_calls):
        """🔒 Le cooldown en action : la ligne à NULL fraîche coupe la seconde
        tentative. C'est ce qui empêche un périmètre introuvable de générer une
        requête vers seloger.com à chaque cycle de scrape (toutes les 30 s)."""
        calls, _ = resolver_calls
        resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo)
        assert len(calls) == 1

        assert resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo) is None

        assert len(calls) == 1, "l'échec mémorisé doit court-circuiter la 2e tentative"

    def test_a_failure_older_than_the_cooldown_is_retried(self, storage, sql, resolver_calls):
        """Vieillissement fait en SQL brut : `resolved_at` est rempli par
        `CURRENT_TIMESTAMP` côté serveur, donc c'est l'horloge du serveur qui
        décide, et il n'y a aucun moyen de fabriquer une vieille ligne en passant
        par le repo."""
        calls, result = resolver_calls
        resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo)
        sql.exec("UPDATE seloger_place_ids SET resolved_at = NOW() - INTERVAL '8 days'")
        result["place_id"] = "AD09FR40"

        assert resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo) == "AD09FR40"

        assert len(calls) == 2
        assert storage.seloger_geo.get_cached("75113")["place_id"] == "AD09FR40"

    @pytest.mark.parametrize(
        ("age_days", "expect_retry"),
        [
            pytest.param(1, False, id="1-jour"),
            pytest.param(6, False, id="6-jours"),
            pytest.param(8, True, id="8-jours"),
            pytest.param(30, True, id="30-jours"),
        ],
    )
    def test_the_cooldown_boundary_is_seven_days(self, storage, sql, resolver_calls, age_days, expect_retry):
        calls, _ = resolver_calls
        storage.seloger_geo.set_cached("75113", None)
        sql.exec(
            "UPDATE seloger_place_ids SET resolved_at = NOW() - make_interval(days => %s)",
            (age_days,),
        )

        resolve_place_id(make_city_location(insee="75113"), storage.seloger_geo)

        assert (len(calls) == 1) is expect_retry

    def test_a_location_without_an_identifiable_key_never_touches_the_table(self, storage, sql, resolver_calls):
        """Une commune sans code INSEE ne produit aucune clé : rien n'est tenté
        ni mémorisé, sinon des lignes d'échec s'accumuleraient sous une clé
        vide et se recouvriraient entre périmètres."""
        calls, _ = resolver_calls

        assert resolve_place_id({"kind": "city", "city": "Nulle-Part"}, storage.seloger_geo) is None

        assert calls == []
        assert sql.one("SELECT COUNT(*) FROM seloger_place_ids") == 0
