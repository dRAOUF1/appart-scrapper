"""Ops admin (#21) contre un vrai Postgres : purges de caches géo et
réglages, prouvés sur les vraies tables — pas sur des doubles.

Ce que ces tests prouvent et que le reste de la suite ne peut pas :

1. **purge réelle vérifiée par compteur** — critère d'acceptation #21 :
   insérer N entrées (dont des échecs mémorisés) via les repos géo, lire les
   compteurs, purger UNE source, recompter — la source purgée tombe à 0,
   les autres caches restent intacts ;
2. **le registre ne dérive pas du schéma** — chaque table de CACHES_GEO est
   une vraie table interrogeable : si une source ajoute son cache sans le
   déclarer ici (ou l'inverse), ce module devient rouge ;
3. **round-trip des réglages** — set_setting → get_setting sur les clés
   canoniques #21, et lecture à l'usage (`lire_jours`) sur le repo réel ;
4. **statistiques_pool** — le pool réel du Storage expose ses compteurs.

Marqués `integration` automatiquement (conftest des tests d'intégration) ;
sautés sans TEST_DATABASE_URL.
"""

from __future__ import annotations

import pytest

from core.reglages import (
    CLE_PURGE_LOGS_AUDIT,
    CLE_RETENTION_ANNONCES,
    DEFAUT_RETENTION_ANNONCES,
    lire_jours,
)
from repositories.admin_repo import CACHES_GEO
from repositories.base import statistiques_pool

# ---------------------------------------------------------------------------
# Compteurs + purge réelle des caches géo
# ---------------------------------------------------------------------------

class TestCompteursCachesGeo:
    def test_entrees_et_manquees_compte_la_realite(self, storage):
        """Un échec mémorisé (identifiant NULL) compte comme « manquée » :
        c'est exactement la définition du taux demandé par l'issue."""
        storage.seloger_geo.set_cached("93066", "AD04")
        storage.seloger_geo.set_cached("dept:99", None)
        storage.pap_geo.set_cached("city:93000", "37782")

        stats = {s["table"]: s for s in storage.admin.get_geo_cache_stats()}

        assert stats["seloger_place_ids"]["entrees"] == 2
        assert stats["seloger_place_ids"]["manquees"] == 1
        assert stats["seloger_place_ids"]["suivi_echecs"] is True
        assert stats["pap_geo_ids"]["entrees"] == 1
        assert stats["pap_geo_ids"]["manquees"] == 0

    def test_le_cache_sans_echec_memorise_ne_declare_jamais_de_manquee(self, storage):
        # Clé « postal:<cp> » — voir services.geocode_commune.area_cache_key.
        storage.commune_geo.set_cached("postal:75013", 48.853, 2.35)

        communes = next(
            s for s in storage.admin.get_geo_cache_stats() if s["table"] == "commune_centres"
        )

        assert communes["entrees"] == 1
        assert communes["manquees"] == 0
        assert communes["suivi_echecs"] is False


class TestPurgeReelleCacheGeo:
    def test_une_purge_source_scopee_vide_sa_table_et_seule_celle_ci(self, storage):
        """Le cœur du critère d'acceptation : insert N → purge → compteur 0,
        avec les AUTRES caches intacts (une purge ne fuit pas sur sa voisine)."""
        for key in ("93066", "dept:75", "region:11"):
            storage.seloger_geo.set_cached(key, "AD04")
        storage.bienici_geo.set_cached("dept:33", ["33"])

        avant = {s["table"]: s["entrees"] for s in storage.admin.get_geo_cache_stats()}
        assert avant["seloger_place_ids"] == 3

        supprimees = storage.admin.purge_geo_cache("seloger_place_ids")

        apres = {s["table"]: s["entrees"] for s in storage.admin.get_geo_cache_stats()}
        assert supprimees == 3
        assert apres["seloger_place_ids"] == 0
        assert apres["bienici_zone_ids"] == 1, "la purge ne doit toucher qu'une table"

    def test_purger_un_table_vide_renvoie_zero(self, storage):
        assert storage.admin.purge_geo_cache("orpi_geo_ids") == 0

    def test_une_table_hors_registre_leve_avant_toute_base(self, storage):
        with pytest.raises(ValueError, match="Cache géo inconnu"):
            storage.admin.purge_geo_cache("listings")


class TestRegistreVsSchema:
    @pytest.mark.parametrize("cache", list(CACHES_GEO), ids=lambda c: c["table"])
    def test_chaque_table_du_registre_existe_vraiment(self, sql, cache):
        """Garde-fou anti-dérive : une entrée ajoutée au registre sans table
        en base (ou un renommage de table oublié ici) rend ce test rouge."""
        assert sql.one(f"SELECT COUNT(*) FROM {cache['table']}") == 0

    @pytest.mark.parametrize(
        "cache",
        [c for c in CACHES_GEO if c["colonne"]],
        ids=lambda c: c["table"],
    )
    def test_la_colonne_identifiante_est_bien_nullable(self, sql, cache):
        """« Manquée » = identifiant NULL : si la colonne devenait NOT NULL,
        le taux de résolutions manquées serait mensongèrement à zéro."""
        nullable = sql.one(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = %s AND column_name = %s",
            (cache["table"], cache["colonne"]),
        )
        assert nullable == "YES"


# ---------------------------------------------------------------------------
# Réglages : round-trip réel
# ---------------------------------------------------------------------------

class TestReglagesRoundTrip:
    def test_un_reglage_ecrit_est_relu_telle_quel(self, storage):
        storage.settings.set_setting(CLE_RETENTION_ANNONCES, "9")
        storage.settings.set_setting(CLE_PURGE_LOGS_AUDIT, "45")

        assert storage.settings.get_setting(CLE_RETENTION_ANNONCES, "") == "9"
        assert storage.settings.get_setting(CLE_PURGE_LOGS_AUDIT, "") == "45"

    def test_l_usage_lit_le_repo_reel_pas_une_constant(self, storage):
        """Le même chemin qu'une routine de purge : settings_repo réel →
        lire_jours. Un réglage persisté change donc le prochain tour."""
        assert lire_jours(storage.settings.get_setting, CLE_RETENTION_ANNONCES,
                          DEFAUT_RETENTION_ANNONCES) == DEFAUT_RETENTION_ANNONCES

        storage.settings.set_setting(CLE_RETENTION_ANNONCES, "9")

        assert lire_jours(storage.settings.get_setting, CLE_RETENTION_ANNONCES,
                          DEFAUT_RETENTION_ANNONCES) == 9


# ---------------------------------------------------------------------------
# Pool de connexions réel
# ---------------------------------------------------------------------------

class TestStatistiquesPoolReel:
    def test_le_pool_actif_expose_ses_compteurs(self, storage, pg_url):
        stats = statistiques_pool(pg_url)

        assert stats is not None, "Storage a déjà emprunté des connexions : le pool existe"
        assert stats["min"] >= 1
        assert stats["max"] >= stats["min"]
        assert stats["utilisees"] >= 0
        assert stats["libres"] >= 0
